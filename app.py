import os, time, threading, requests
from datetime import datetime, timedelta
from flask import Flask, redirect, request, jsonify, render_template
from kiteconnect import KiteConnect

# ================= CONFIG =================
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
# Vital for Render Binding
PORT = int(os.environ.get("PORT", 10000))
# URL of your hosted app to ping itself
RENDER_URL = os.environ.get("RENDER_URL", f"http://0.0.0.0:{PORT}")

trade_config = {
    "target_pct": 20.0,
    "sl_pct": 10.0,
    "active_side": "NONE",
    "quantity": 50,
    "is_running": False
}

app = Flask(__name__)
kite = KiteConnect(api_key=API_KEY)
auth_active = False
last_error = None

# ================= KEEP ALIVE PROTOCOL =================
def self_keepalive():
    """Pings the /ping route every 4 mins to prevent Render sleep"""
    time.sleep(30) # Initial boot delay
    while True:
        try:
            requests.get(f"{RENDER_URL}/ping", timeout=10)
        except Exception as e:
            print(f"Keep-alive skip: {e}")
        time.sleep(240)

threading.Thread(target=self_keepalive, daemon=True).start()

# ================= KITE UTILS =================
def get_atm_strike():
    ltp = kite.ltp("NSE:NIFTY 50")["NSE:NIFTY 50"]["last_price"]
    return int(round(ltp / 50) * 50)

def get_expiry_str():
    today = datetime.now()
    # Nifty Weekly Expiry (Tuesday)
    days_until = (1 - today.weekday() + 7) % 7
    expiry = today + timedelta(days=days_until)
    y = str(expiry.year)[2:]
    m = str(expiry.month)
    if m == "10": m = "O"
    elif m == "11": m = "N"
    elif m == "12": m = "D"
    return f"{y}{m}{expiry.day:02d}"

# ================= TRADING ENGINE =================
def trade_monitor():
    global auth_active, last_error
    while True:
        if not (auth_active and trade_config["is_running"]):
            time.sleep(5); continue

        try:
            positions = kite.positions()["net"]
            active = [p for p in positions if p["quantity"] != 0]

            if not active and trade_config["active_side"] != "NONE":
                symbol = f"NIFTY{get_expiry_str()}{get_atm_strike()}{trade_config['active_side']}"
                kite.place_order(variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NFO,
                                 tradingsymbol=symbol, transaction_type=kite.TRANSACTION_TYPE_BUY,
                                 quantity=trade_config["quantity"], order_type=kite.ORDER_TYPE_MARKET,
                                 product=kite.PRODUCT_MIS)
            
            elif active:
                for p in active:
                    ltp = kite.ltp(f"NFO:{p['tradingsymbol']}")[f"NFO:{p['tradingsymbol']}"]["last_price"]
                    pnl_pct = ((ltp - p["average_price"]) / p["average_price"]) * 100
                    if pnl_pct >= trade_config["target_pct"] or pnl_pct <= -trade_config["sl_pct"]:
                        square_off_all()
                        trade_config["is_running"] = False
        except Exception as e: last_error = str(e)
        time.sleep(2)

threading.Thread(target=trade_monitor, daemon=True).start()

# ================= ROUTES =================
@app.route("/")
def home(): return render_template("index.html", auth=auth_active, config=trade_config)

@app.route("/ping")
def ping(): return "pong"

@app.route("/login")
def login(): return redirect(kite.login_url())

@app.route("/callback")
def callback():
    global auth_active
    try:
        session = kite.generate_session(request.args.get("request_token"), api_secret=API_SECRET)
        kite.set_access_token(session["access_token"])
        auth_active = True
        return redirect("/")
    except: return "Auth Failed"

@app.route("/update_trade", methods=["POST"])
def update_trade():
    data = request.json
    trade_config.update({
        "target_pct": float(data.get("tp", 20)),
        "sl_pct": float(data.get("sl", 10)),
        "active_side": data.get("side", "NONE"),
        "is_running": data.get("run", False)
    })
    return jsonify(trade_config)

@app.route("/status")
def status():
    pos = kite.positions()["net"] if auth_active else []
    return jsonify({"positions": pos, "error": last_error, "config": trade_config})

def square_off_all():
    for p in kite.positions()["net"]:
        if p["quantity"] != 0:
            side = kite.TRANSACTION_TYPE_SELL if p["quantity"] > 0 else kite.TRANSACTION_TYPE_BUY
            kite.place_order(variety=kite.VARIETY_REGULAR, exchange=p["exchange"], tradingsymbol=p["tradingsymbol"],
                             transaction_type=side, quantity=abs(p["quantity"]), order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS)

if __name__ == "__main__":
    # Required for Render Port Binding
    app.run(host="0.0.0.0", port=PORT)
