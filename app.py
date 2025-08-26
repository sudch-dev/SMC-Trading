from flask import Flask, redirect, request, render_template, jsonify
from kiteconnect import KiteConnect
import os
from dotenv import load_dotenv
from smc_logic import run_smc_scan

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
ALLOW_ORDER_EXEC = os.getenv("ALLOW_ORDER_EXEC", "0") in ("1", "true", "True")

PRODUCT_DEFAULT = os.getenv("PRODUCT", "NRML")  # NRML or MIS

kite = KiteConnect(api_key=API_KEY)

access_token = None
smc_status = {}

# ------------------ Routes ------------------

@app.route('/')
def home():
    return redirect('/login')

@app.route('/login')
def login():
    login_url = kite.login_url()
    return redirect(login_url)

@app.route('/callback')
def callback():
    global access_token
    request_token = request.args.get('request_token')
    if not request_token:
        return "Missing request_token", 400
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]
    kite.set_access_token(access_token)
    return redirect('/dashboard')

@app.route('/dashboard')
def dashboard():
    return render_template('index.html')

@app.route('/api/smc-status')
def api_smc_status():
    """Return latest scan with TP/SL suggestions."""
    global smc_status
    if access_token:
        kite.set_access_token(access_token)
        try:
            smc_status = run_smc_scan(kite) or {}
        except Exception as e:
            smc_status = {"status": "error", "error": str(e)}
    else:
        smc_status = {"status": "error", "error": "Not logged in. Please complete Kite login."}
    return jsonify(smc_status)

@app.route('/api/execute', methods=['POST'])
def api_execute():
    """Place entry order; optionally place TP/SL exit orders (regular variety)."""
    if not ALLOW_ORDER_EXEC:
        return jsonify({"status": "error", "error": "Order execution disabled. Set ALLOW_ORDER_EXEC=1"}), 403
    if not access_token:
        return jsonify({"status": "error", "error": "Not logged in."}), 401

    kite.set_access_token(access_token)
    payload = request.get_json(force=True) or {}

    # Expected: symbol ("NFO:..."), action ("BUY"/"SELL"), quantity (int),
    # with_tp_sl (bool), tp (float), sl (float), product ("NRML"/"MIS"), order_type ("MARKET"/"LIMIT"), price (float)
    try:
        symbol_full = payload.get("symbol", "")
        if symbol_full.startswith("NFO:"):
            tradingsymbol = symbol_full.split(":", 1)[1]
        else:
            tradingsymbol = symbol_full  # accept raw too

        qty = int(payload.get("quantity", 0))
        action = payload.get("action", "").upper()
        order_type = payload.get("order_type", "MARKET").upper()
        price = payload.get("price", None)
        product = payload.get("product", PRODUCT_DEFAULT)

        if action not in ("BUY", "SELL"):
            return jsonify({"status": "error", "error": "Invalid action"}), 400
        if qty <= 0:
            return jsonify({"status": "error", "error": "Quantity must be > 0"}), 400

        entry_kwargs = dict(
            variety=KiteConnect.VARIETY_REGULAR,
            exchange=KiteConnect.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=action,
            quantity=qty,
            product=product,
            order_type=KiteConnect.ORDER_TYPE_MARKET if order_type == "MARKET" else KiteConnect.ORDER_TYPE_LIMIT,
            validity=KiteConnect.VALIDITY_DAY,
        )
        if order_type == "LIMIT":
            if price is None:
                return jsonify({"status": "error", "error": "price required for LIMIT"}), 400
            entry_kwargs["price"] = float(price)

        entry_id = kite.place_order(**entry_kwargs)

        resp = {"status": "ok", "entry_order_id": entry_id, "tp_order_id": None, "sl_order_id": None}

        # Optional exits
        if payload.get("with_tp_sl"):
            tp = payload.get("tp", None)
            sl = payload.get("sl", None)
            if tp is not None:
                exit_tp_kwargs = dict(
                    variety=KiteConnect.VARIETY_REGULAR,
                    exchange=KiteConnect.EXCHANGE_NFO,
                    tradingsymbol=tradingsymbol,
                    transaction_type=("SELL" if action == "BUY" else "BUY"),
                    quantity=qty,
                    product=product,
                    order_type=KiteConnect.ORDER_TYPE_LIMIT,
                    price=float(tp),
                    validity=KiteConnect.VALIDITY_DAY,
                )
                try:
                    resp["tp_order_id"] = kite.place_order(**exit_tp_kwargs)
                except Exception as e:
                    resp["tp_error"] = str(e)

            if sl is not None:
                # Stop-loss MARKET (SLM) with trigger_price = sl
                exit_sl_kwargs = dict(
                    variety=KiteConnect.VARIETY_REGULAR,
                    exchange=KiteConnect.EXCHANGE_NFO,
                    tradingsymbol=tradingsymbol,
                    transaction_type=("SELL" if action == "BUY" else "BUY"),
                    quantity=qty,
                    product=product,
                    order_type=KiteConnect.ORDER_TYPE_SLM,
                    trigger_price=float(sl),
                    validity=KiteConnect.VALIDITY_DAY,
                )
                try:
                    resp["sl_order_id"] = kite.place_order(**exit_sl_kwargs)
                except Exception as e:
                    resp["sl_error"] = str(e)

        return jsonify(resp)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
