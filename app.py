import os
import time
import threading
import requests
import numpy as np
from datetime import datetime
from flask import Flask, redirect, request, jsonify, render_template
from kiteconnect import KiteConnect
from sklearn.linear_model import LogisticRegression

# ================= CONFIG =================
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
RENDER_URL = os.environ.get("RENDER_URL", "https://your-service-name.onrender.com")
CAPITAL = 5000
SCAN_INTERVAL = 300
AUTO_TRADE = True

app = Flask(__name__)
kite = KiteConnect(api_key=API_KEY)
auth_active = False
current_token = os.environ.get("access_token")
trailing_tracker = {}
last_error = None

if current_token:
    kite.set_access_token(current_token)
    auth_active = True

# ================= KEEP ALIVE =================
def self_keepalive():
    """Pings /ping every 4 mins to prevent Render sleep"""
    time.sleep(60)
    while True:
        try:
            requests.get(f"{RENDER_URL}/ping", timeout=10)
        except Exception as e:
            print(f"Keep-alive skipped: {e}")
        time.sleep(240)

threading.Thread(target=self_keepalive, daemon=True).start()

# ================= ML MODEL =================
model = LogisticRegression(solver='lbfgs')

def train_dummy_model():
    # 0=Sell, 1=Wait, 2=Buy
    X, y = [], []
    for _ in range(300):
        r1, r5 = np.random.normal(0, 0.002), np.random.normal(0, 0.005)
        vr, tr = np.random.uniform(0.5, 2), np.random.normal(0, 1)
        score = r1 + r5 + (vr - 1) * 0.3 + tr * 0.01
        X.append([r1, r5, vr, tr])
        y.append(2 if score > 0.01 else (0 if score < -0.01 else 1))
    model.fit(np.array(X), np.array(y))

train_dummy_model()

# ================= TRADING UTILITIES =================
def ai_signal(symbol):
    try:
        ltp_data = kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]
        data = kite.historical_data(ltp_data["instrument_token"], 
                                    datetime.now().replace(hour=9, minute=15), 
                                    datetime.now(), "5minute")
        if len(data) < 15: return None
        closes = np.array([c["close"] for c in data])
        volumes = np.array([c["volume"] for c in data])
        feat = [(closes[-1]-closes[-2])/closes[-2], (closes[-1]-closes[-6])/closes[-6], 
                volumes[-1]/np.mean(volumes[-10:]), closes[-1]-np.mean(closes[-10:])]
        probs = model.predict_proba([feat])[0]
        if probs[2] > 0.65: return "BUY"
        if probs[0] > 0.65: return "SELL"
    except: return None
    return None

def square_off_all():
    global last_error
    try:
        positions = kite.positions()["net"]
        for p in positions:
            if p["quantity"] != 0:
                side = kite.TRANSACTION_TYPE_SELL if p["quantity"] > 0 else kite.TRANSACTION_TYPE_BUY
                kite.place_order(variety=kite.VARIETY_REGULAR, exchange=p["exchange"], 
                                 tradingsymbol=p["tradingsymbol"], transaction_type=side, 
                                 quantity=abs(p["quantity"]), order_type=kite.ORDER_TYPE_MARKET, 
                                 product=kite.PRODUCT_MIS)
        trailing_tracker.clear()
        print("ALL POSITIONS SQUARED OFF")
    except Exception as e:
        last_error = f"Square-off failure: {str(e)}"

# ================= TRADING ENGINE =================
def trading_engine():
    global auth_active, last_error
    while True:
        if not (AUTO_TRADE and auth_active):
            time.sleep(30); continue
        
        now = datetime.now().time()
        # Market Close Auto-Exit
        if now > datetime.strptime("15:20", "%H:%M").time():
            square_off_all(); time.sleep(60); continue
        if now < datetime.strptime("09:20", "%H:%M").time():
            time.sleep(60); continue

        # TP/SL and Entry Scanning
        try:
            # Entry Scanning
            symbols = ["RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK"]
            for symbol in symbols:
                signal = ai_signal(symbol)
                if signal:
                    price = kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["last_price"]
                    kite.place_order(variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NSE, 
                                     tradingsymbol=symbol, transaction_type=kite.TRANSACTION_TYPE_BUY if signal == "BUY" else kite.TRANSACTION_TYPE_SELL, 
                                     quantity=max(1, int(CAPITAL / price)), order_type=kite.ORDER_TYPE_MARKET, 
                                     product=kite.PRODUCT_MIS)
                    print(f"ENTERED {signal}: {symbol}")
        except Exception as e:
            last_error = f"Engine Error: {str(e)}"
        
        time.sleep(SCAN_INTERVAL)

threading.Thread(target=trading_engine, daemon=True).start()

# ================= ROUTES =================
@app.route("/")
def home():
    return render_template("index.html", auth=auth_active)

@app.route("/ping")
def ping(): return "pong"

@app.route("/login")
def login(): return redirect(kite.login_url())

@app.route("/callback")
def callback():
    global auth_active
    request_token = request.args.get("request_token")
    try:
        session = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(session["access_token"])
        auth_active = True
        return redirect("/")
    except Exception as e:
        return f"Auth Failed: {e}"

@app.route("/dashboard_data")
def dashboard_data():
    if not auth_active: return jsonify({"error": "Auth Required"}), 401
    try:
        margins = kite.margins()
        equity_funds = margins.get("equity", {}).get("available", {}).get("cash", 0)
        
        orders = kite.orders()
        completed = [o for o in orders if o['status'] == 'COMPLETE']
        
        positions = kite.positions().get("net", [])
        
        return jsonify({
            "engine_running": AUTO_TRADE,
            "funds": equity_funds,
            "orders_sent": len(orders),
            "orders_traded": len(completed),
            "positions": positions,
            "last_error": last_error,
            "server_time": datetime.now().strftime("%H:%M:%S")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/orders")
def get_orders():
    return jsonify(kite.orders()) if auth_active else ({"error": "Unauthorized"}, 401)

@app.route("/squareoff")
def manual_squareoff():
    square_off_all()
    return "Manual Square-off Executed"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
