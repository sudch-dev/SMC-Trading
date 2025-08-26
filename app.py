import os
from flask import Flask, redirect, request, render_template, jsonify
from kiteconnect import KiteConnect
from dotenv import load_dotenv
from smc_logic import run_smc_scan  # your scanner

# extras for recording + rounding
import json
from pathlib import Path
from datetime import datetime

load_dotenv()

app = Flask(__name__)

# ------------ Config ------------
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")

# allow or block live order execution (safety switch)
ALLOW_ORDER_EXEC = os.getenv("ALLOW_ORDER_EXEC", "0") in ("1", "true", "True")

# default product for entries/exits (NRML recommended for AMO)
PRODUCT_DEFAULT = os.getenv("PRODUCT", "NRML")  # NRML or MIS

# option tick size (₹) for rounding prices
OPT_TICK = float(os.getenv("OPT_TICK", "0.05"))

# AMO fallback controls
ALLOW_AMO = os.getenv("ALLOW_AMO", "1") in ("1", "true", "True")
AMO_FALLBACK_PATTERNS = (
    "after market order",
    "amo",
    "market is closed",
    "outside market hours",
    "could not be converted",
    "not allowed during market closed",
)

kite = KiteConnect(api_key=API_KEY)

access_token = None
smc_status = {}


# ------------ Helpers ------------
def _tick_round(x, tick=0.05):
    if x is None:
        return None
    return round(round(float(x) / tick) * tick, 2)


def _compute_limit_from_quote(tradingsymbol, action):
    """Choose a LIMIT price from best bid/ask (LTP fallback), tick-rounded."""
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

    price = _tick_round(ref, OPT_TICK)
    snap = {"ltp": ltp, "best_buy": best_buy, "best_sell": best_sell}
    return price, snap


def _record_entry(symbol, action, qty, chosen_price, quote_snapshot, extra=None):
    rec = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "action": action,
        "qty": qty,
        "chosen_price": chosen_price,
        "quote": quote_snapshot or {},
    }
    if extra:
        rec.update(extra)
    path = Path("entry_records.json")
    try:
        data = json.loads(path.read_text()) if path.exists() else []
        data.append(rec)
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ------------ Routes ------------
@app.route("/")
def home():
    return redirect("/login")


@app.route("/login")
def login():
    login_url = kite.login_url()
    return redirect(login_url)


@app.route("/callback")
def callback():
    global access_token
    request_token = request.args.get("request_token")
    if not request_token:
        return "Missing request_token", 400
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]
    kite.set_access_token(access_token)
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    return render_template("index.html")


@app.route("/api/health")
def api_health():
    return {"ok": True, "logged_in": bool(access_token)}


@app.route("/api/smc-status")
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
        smc_status = {
            "status": "error",
            "error": "Not logged in. Please complete Kite login.",
        }
    return jsonify(smc_status)


@app.route("/api/execute", methods=["POST"])
def api_execute():
    """
    Entry is always placed as LIMIT (no MARKET).
    TP = LIMIT. SL = stop-loss LIMIT (SL).
    Action is derived from trade_type if provided (LONG->BUY, SHORT->SELL).

    If market is closed (or broker refuses regular placement) and ALLOW_AMO=1,
    we fallback to VARIETY_AMO for the ENTRY ONLY and skip TP/SL legs.
    """
    if not ALLOW_ORDER_EXEC:
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "Order execution disabled. Set ALLOW_ORDER_EXEC=1",
                }
            ),
            403,
        )
    if not access_token:
        return jsonify({"status": "error", "error": "Not logged in."}), 401

    kite.set_access_token(access_token)
    payload = request.get_json(force=True) or {}

    try:
        symbol_full = payload.get("symbol", "")
        tradingsymbol = (
            symbol_full.split(":", 1)[1]
            if symbol_full.startswith("NFO:")
            else symbol_full
        )

        qty = int(payload.get("quantity", 0))
        action = (payload.get("action", "")).upper()  # legacy fallback
        trade_type = (payload.get("trade_type") or "").upper()  # LONG / SHORT
        opt_side = (payload.get("type") or "").upper()  # CE / PE (for logging)

        # derive action from intent to avoid CE/PE drift
        if trade_type in ("LONG", "SHORT"):
            action = "BUY" if trade_type == "LONG" else "SELL"

        order_type_req = (payload.get("order_type", "LIMIT")).upper()
        product = payload.get("product", PRODUCT_DEFAULT)
        price_req = payload.get("price")  # only used if client sends explicit limit

        if action not in ("BUY", "SELL"):
            return jsonify({"status": "error", "error": "Invalid action"}), 400
        if qty <= 0:
            return jsonify({"status": "error", "error": "Quantity must be > 0"}), 400

        # Force LIMIT entry — either client price or derived from quote
        if order_type_req == "LIMIT" and price_req is not None:
            chosen_price = _tick_round(price_req, OPT_TICK)
            quote_snap = {"from": "client_price"}
        else:
            chosen_price, quote_snap = _compute_limit_from_quote(tradingsymbol, action)
        if chosen_price is None:
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "Unable to derive limit price from quote",
                    }
                ),
                502,
            )

        # ---------- ENTRY: try REGULAR first, then AMO fallback ----------
        entry_kwargs = dict(
            variety=KiteConnect.VARIETY_REGULAR,
            exchange=KiteConnect.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=action,
            quantity=qty,
            product=product,
            order_type=KiteConnect.ORDER_TYPE_LIMIT,  # always LIMIT
            validity=KiteConnect.VALIDITY_DAY,
            price=float(chosen_price),
        )

        entry_id = None
        queued_as_amo = False
        amo_note = None

        try:
            entry_id = kite.place_order(**entry_kwargs)
        except Exception as ex:
            msg = str(ex).lower()
            # market-closed / AMO-eligible style failures → fallback if allowed
            if ALLOW_AMO and any(pat in msg for pat in AMO_FALLBACK_PATTERNS):
                try:
                    amo_kwargs = dict(entry_kwargs)
                    amo_kwargs["variety"] = KiteConnect.VARIETY_AMO
                    entry_id = kite.place_order(**amo_kwargs)
                    queued_as_amo = True
                    amo_note = (
                        "Entry queued as AMO (market closed). TP/SL legs were skipped."
                    )
                except Exception as ex2:
                    # Could not even place AMO — return that message
                    return (
                        jsonify(
                            {"status": "error", "error": f"AMO failed: {str(ex2)}"}
                        ),
                        400,
                    )
            else:
                # Different error (margin/product/rejections etc.)
                return jsonify({"status": "error", "error": str(ex)}), 400

        # audit trail
        _record_entry(
            tradingsymbol,
            action,
            qty,
            chosen_price,
            quote_snap,
            extra={
                "entry_order_id": entry_id,
                "trade_type": trade_type,
                "type": opt_side,
                "queued_as_amo": queued_as_amo,
            },
        )

        resp = {
            "status": "ok",
            "entry_order_id": entry_id,
            "tp_order_id": None,
            "sl_order_id": None,
            "used_limit_price": chosen_price,
        }
        if amo_note:
            resp["note"] = amo_note

        # ---------- Optional exits ----------
        # Skip when AMO: exchanges won't accept linked exits now.
        if payload.get("with_tp_sl") and not queued_as_amo:
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

            # SL → stop-loss LIMIT (SL) with one-tick offset
            if sl is not None:
                sl_trig = _tick_round(sl, OPT_TICK)
                sl_price = _tick_round(
                    (sl_trig - OPT_TICK) if action == "BUY" else (sl_trig + OPT_TICK),
                    OPT_TICK,
                )
                exit_sl_kwargs = dict(
                    variety=KiteConnect.VARIETY_REGULAR,
                    exchange=KiteConnect.EXCHANGE_NFO,
                    tradingsymbol=tradingsymbol,
                    transaction_type=("SELL" if action == "BUY" else "BUY"),
                    quantity=qty,
                    product=product,
                    order_type=KiteConnect.ORDER_TYPE_SL,  # stop-loss LIMIT
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

        # If AMO, explicitly mention exits were skipped
        if queued_as_amo:
            resp["tp_error"] = resp.get("tp_error") or "Skipped because entry is AMO"
            resp["sl_error"] = resp.get("sl_error") or "Skipped because entry is AMO"

        return jsonify(resp)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
