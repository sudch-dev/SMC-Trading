import os
from flask import Flask, redirect, request, render_template, jsonify
from kiteconnect import KiteConnect
from dotenv import load_dotenv
from smc_logic import run_smc_scan  # uses the separate module

# --- NEW: for recording and rounding ---
import json
from pathlib import Path

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
ALLOW_ORDER_EXEC = os.getenv("ALLOW_ORDER_EXEC", "0") in ("1", "true", "True")
PRODUCT_DEFAULT = os.getenv("PRODUCT", "NRML")  # NRML or MIS
OPT_TICK = float(os.getenv("OPT_TICK", "0.05"))  # common option tick

kite = KiteConnect(api_key=API_KEY)

access_token = None
smc_status = {}

def _tick_round(x, tick=0.05):
    if x is None: return None
    return round(round(float(x)/tick)*tick, 2)

def _compute_limit_from_quote(tradingsymbol, action):
    """Pick a reasonable LIMIT based on best bid/ask (fallback LTP)."""
    q = {}
    try:
        q = kite.quote([f"NFO:{tradingsymbol}"]) or {}
        q = q.get(f"NFO:{tradingsymbol}", {})
    except Exception:
        q = {}
    ltp = q.get("last_price")
    depth = q.get("depth") or {}
    best_buy = (depth.get("buy") or [{}])[0].get("price")
    best_sell = (depth.get("sell") or [{}])[0].get("price")

    ref = (best_sell if action == "BUY" else best_buy) or ltp
    if ref is None:
        return None, {"ltp": None, "best_buy": None, "best_sell": None}

    price = _tick_round(ref, OPT_TICK)  # stay within tick & band
    snap = {"ltp": ltp, "best_buy": best_buy, "best_sell": best_sell}
    return price, snap

def _record_entry(symbol, action, qty, chosen_price, quote_snapshot, extra=None):
    rec = {
        "ts": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "action": action,
        "qty": qty,
        "chosen_price": chosen_price,
        "quote": quote_snapshot or {},
    }
    if extra: rec.update(extra)
    path = Path("entry_records.json")
    try:
        data = json.loads(path.read_text()) if path.exists() else []
        data.append(rec)
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

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
    """
    Entry is always placed as LIMIT (no MARKET). TP = LIMIT. SL = stop-loss LIMIT (SL).
    Also records observed quote/price to entry_records.json.
    """
    if not ALLOW_ORDER_EXEC:
        return jsonify({"status": "error", "error": "Order execution disabled. Set ALLOW_ORDER_EXEC=1"}), 403
    if not access_token:
        return jsonify({"status": "error", "error": "Not logged in."}), 401

    kite.set_access_token(access_token)
    payload = request.get_json(force=True) or {}

    try:
        symbol_full = payload.get("symbol", "")
        tradingsymbol = symbol_full.split(":", 1)[1] if symbol_full.startswith("NFO:") else symbol_full

        qty = int(payload.get("quantity", 0))
        action = (payload.get("action", "")).upper()
        order_type_req = (payload.get("order_type", "MARKET")).upper()
        product = payload.get("product", PRODUCT_DEFAULT)
        price_req = payload.get("price")  # only used if LIMIT explicitly supplied

        if action not in ("BUY", "SELL"):
            return jsonify({"status": "error", "error": "Invalid action"}), 400
        if qty <= 0:
            return jsonify({"status": "error", "error": "Quantity must be > 0"}), 400

        # Force LIMIT entry — either user-specified limit or derived from quote
        if order_type_req == "LIMIT" and price_req is not None:
            chosen_price = _tick_round(price_req, OPT_TICK)
            quote_snap = {"from": "client_price"}
        else:
            chosen_price, quote_snap = _compute_limit_from_quote(tradingsymbol, action)
        if chosen_price is None:
            return jsonify({"status": "error", "error": "Unable to derive limit price from quote"}), 502

        entry_kwargs = dict(
            variety=KiteConnect.VARIETY_REGULAR,
            exchange=KiteConnect.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=action,
            quantity=qty,
            product=product,
            order_type=KiteConnect.ORDER_TYPE_LIMIT,   # always LIMIT
            validity=KiteConnect.VALIDITY_DAY,
            price=float(chosen_price),
        )
        entry_id = kite.place_order(**entry_kwargs)

        # audit trail
        _record_entry(tradingsymbol, action, qty, chosen_price, quote_snap, extra={"entry_order_id": entry_id})

        resp = {"status": "ok", "entry_order_id": entry_id, "tp_order_id": None, "sl_order_id": None,
                "used_limit_price": chosen_price}

        # Optional exits (both LIMIT-type)
        if payload.get("with_tp_sl"):
            tp = payload.get("tp")
            sl = payload.get("sl")

            # TP → LIMIT
            if tp is not None:
                tp_price = _tick_round(tp, OPT_TICK)
                exit_tp_kwargs = dict(
                    variety=KiteConnect.VARIETY_REGULAR,
                    exchange=KiteConnect.EXCHANGE_NFO,
                    tradingsymbol=tradingsymbol,
                    transaction_type=("SELL" if action == "BUY" else "BUY"),
                    quantity=qty,
                    product=product,
                    order_type=KiteConnect.ORDER_TYPE_LIMIT,
                    price=float(tp_price),
                    validity=KiteConnect.VALIDITY_DAY,
                )
                try:
                    resp["tp_order_id"] = kite.place_order(**exit_tp_kwargs)
                    resp["tp_price"] = tp_price
                except Exception as e:
                    resp["tp_error"] = str(e)

            # SL → stop-loss LIMIT (SL) with 1 tick offset from trigger
            if sl is not None:
                sl_trig = _tick_round(sl, OPT_TICK)
                sl_price = _tick_round((sl_trig - OPT_TICK) if action == "BUY" else (sl_trig + OPT_TICK), OPT_TICK)

                exit_sl_kwargs = dict(
                    variety=KiteConnect.VARIETY_REGULAR,
                    exchange=KiteConnect.EXCHANGE_NFO,
                    tradingsymbol=tradingsymbol,
                    transaction_type=("SELL" if action == "BUY" else "BUY"),
                    quantity=qty,
                    product=product,
                    order_type=KiteConnect.ORDER_TYPE_SL,   # stop-loss LIMIT
                    price=float(sl_price),
                    trigger_price=float(sl_trig),
                    validity=KiteConnect.VALIDITY_DAY,
                )
                try:
                    resp["sl_order_id"] = kite.place_order(**exit_sl_kwargs)
                    resp["sl_price"] = sl_price
                    resp["sl_trigger"] = sl_trig
                except Exception as e:
                    resp["sl_error"] = str(e)

        return jsonify(resp)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
