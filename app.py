from flask import Flask, redirect, request, render_template, jsonify
from kiteconnect import KiteConnect
import os
from dotenv import load_dotenv
from smc_logic import run_smc_scan

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")

ALLOW_ORDER_EXEC = os.getenv("ALLOW_ORDER_EXEC", "0").lower() in ("1", "true", "yes")
PRODUCT_DEFAULT = os.getenv("PRODUCT", "NRML")  # NRML or MIS

# Max % away from LTP we will go when MARKET isn't allowed (stock options)
# Use small value (e.g., 0.005 = 0.5%)
ORDER_SLIPPAGE_PCT = float(os.getenv("ORDER_SLIPPAGE_PCT", "0.005"))

# Fraction of the bid-ask spread we’ll cross toward the opposite side
# e.g., 0.6 means: BUY at mid + 0.6*spread, SELL at mid - 0.6*spread
ORDER_SPREAD_FRACTION = float(os.getenv("ORDER_SPREAD_FRACTION", "0.6"))

INDEX_PREFIXES = tuple(s.strip().upper() for s in os.getenv(
    "INDEX_PREFIXES", "NIFTY,BANKNIFTY,FINNIFTY,MIDCPNIFTY"
).split(",") if s.strip())

kite = KiteConnect(api_key=API_KEY)

access_token = None
smc_status = {}

def _tick_round(x, tick=0.05):
    if x is None: return None
    return round(round(float(x)/tick)*tick, 2)

def _is_index_tradingsymbol(tsym: str) -> bool:
    ts = (tsym or "").upper()
    return any(ts.startswith(pref) for pref in INDEX_PREFIXES)

@app.route('/')
def home():
    return redirect('/login')

@app.route('/login')
def login():
    return redirect(kite.login_url())

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
    Entry + optional TP/SL.
    - Index options → honor MARKET/LIMIT (MARKET allowed).
    - Stock options → FORCE LIMIT using bid/ask spread with small cap ±ORDER_SLIPPAGE_PCT of LTP.
    """
    if not ALLOW_ORDER_EXEC:
        return jsonify({"status": "error", "error": "Order execution disabled. Set ALLOW_ORDER_EXEC=1"}), 403
    if not access_token:
        return jsonify({"status": "error", "error": "Not logged in."}), 401

    kite.set_access_token(access_token)
    try:
        p = request.get_json(force=True) or {}
        symbol_full = p.get("symbol", "")
        if not symbol_full:
            return jsonify({"status": "error", "error": "symbol required"}), 400

        if symbol_full.startswith("NFO:"):
            tradingsymbol = symbol_full.split(":", 1)[1]
            qkey = symbol_full
        else:
            tradingsymbol = symbol_full
            qkey = f"NFO:{tradingsymbol}"

        qty = int(p.get("quantity", 0))
        if qty <= 0:
            return jsonify({"status": "error", "error": "quantity must be > 0"}), 400

        action = (p.get("action") or "").upper()
        if action not in ("BUY", "SELL"):
            return jsonify({"status": "error", "error": "action must be BUY or SELL"}), 400

        req_order_type = (p.get("order_type") or "MARKET").upper()
        product = p.get("product", PRODUCT_DEFAULT)

        is_index = _is_index_tradingsymbol(tradingsymbol)

        # Decide entry order/price
        if is_index:
            if req_order_type == "LIMIT":
                entry_type = KiteConnect.ORDER_TYPE_LIMIT
                entry_price = _tick_round(float(p.get("price")))
                if entry_price is None:
                    return jsonify({"status": "error", "error": "price required for LIMIT"}), 400
            else:
                entry_type = KiteConnect.ORDER_TYPE_MARKET
                entry_price = None
        else:
            # Stock option → force LIMIT near LTP using spread
            depth = {}
            ltp = 0.0
            try:
                q = kite.quote([qkey]) or {}
                qv = q.get(qkey) or {}
                ltp = float(qv.get("last_price") or 0.0)
                depth = qv.get("depth") or {}
            except Exception:
                pass
            if ltp <= 0.0:
                return jsonify({"status": "error", "error": "Could not fetch LTP"}), 502

            best_bid = (depth.get("buy") or [{}])[0].get("price")
            best_ask = (depth.get("sell") or [{}])[0].get("price")
            if best_bid and best_ask:
                spread = float(best_ask) - float(best_bid)
                mid = float(best_bid) + spread/2.0
                k = max(0.0, min(1.0, ORDER_SPREAD_FRACTION))
                raw = (mid + k*spread) if action == "BUY" else (mid - k*spread)
                # Cap by ± ORDER_SLIPPAGE_PCT around LTP
                cap_hi = ltp * (1.0 + ORDER_SLIPPAGE_PCT)
                cap_lo = ltp * (1.0 - ORDER_SLIPPAGE_PCT)
                raw = min(max(raw, cap_lo), cap_hi)
                entry_price = _tick_round(raw)
            else:
                # Fallback: LTP ± slippage cap
                raw = ltp * (1.0 + ORDER_SLIPPAGE_PCT) if action == "BUY" else ltp * (1.0 - ORDER_SLIPPAGE_PCT)
                entry_price = _tick_round(raw)
            entry_type = KiteConnect.ORDER_TYPE_LIMIT

        # Place ENTRY
        entry_kwargs = dict(
            variety=KiteConnect.VARIETY_REGULAR,
            exchange=KiteConnect.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=action,
            quantity=qty,
            product=product,
            order_type=entry_type,
            validity=KiteConnect.VALIDITY_DAY,
        )
        if entry_type == KiteConnect.ORDER_TYPE_LIMIT:
            entry_kwargs["price"] = float(entry_price)

        entry_id = kite.place_order(**entry_kwargs)

        resp = {
            "status": "ok",
            "entry_order_id": entry_id,
            "entry_order_type": "LIMIT" if entry_type == KiteConnect.ORDER_TYPE_LIMIT else "MARKET",
            "entry_price": entry_kwargs.get("price"),
            "tp_order_id": None,
            "sl_order_id": None
        }

        # Optional TP/SL
        if p.get("with_tp_sl"):
            tp = p.get("tp", None)
            sl = p.get("sl", None)

            if tp is not None:
                try:
                    tp_price = _tick_round(float(tp))
                    tp_kwargs = dict(
                        variety=KiteConnect.VARIETY_REGULAR,
                        exchange=KiteConnect.EXCHANGE_NFO,
                        tradingsymbol=tradingsymbol,
                        transaction_type=("SELL" if action == "BUY" else "BUY"),
                        quantity=qty,
                        product=product,
                        order_type=KiteConnect.ORDER_TYPE_LIMIT,
                        price=tp_price,
                        validity=KiteConnect.VALIDITY_DAY,
                    )
                    resp["tp_order_id"] = kite.place_order(**tp_kwargs)
                except Exception as e:
                    resp["tp_error"] = str(e)

            if sl is not None:
                try:
                    sl_trig = _tick_round(float(sl))
                    sl_kwargs = dict(
                        variety=KiteConnect.VARIETY_REGULAR,
                        exchange=KiteConnect.EXCHANGE_NFO,
                        tradingsymbol=tradingsymbol,
                        transaction_type=("SELL" if action == "BUY" else "BUY"),
                        quantity=qty,
                        product=product,
                        order_type=KiteConnect.ORDER_TYPE_SLM,
                        trigger_price=sl_trig,
                        validity=KiteConnect.VALIDITY_DAY,
                    )
                    resp["sl_order_id"] = kite.place_order(**sl_kwargs)
                except Exception as e:
                    resp["sl_error"] = str(e)

        return jsonify(resp)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
