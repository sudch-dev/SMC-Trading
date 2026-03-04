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
ACCESS_TOKEN = os.environ.get("access_token")

CAPITAL = 5000
SCAN_INTERVAL = 300 
AUTO_TRADE = True

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

auth_active = False
if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    auth_active = True

# Global tracker for trailing SL (stores highest price for Long, lowest for Short)
trailing_tracker = {}

# ================= KEEP ALIVE =================

@app.route("/ping")
def ping():
    return "pong"

def self_keepalive():
    while True:
        try:
            requests.get("https://smc-trading.onrender.com", timeout=10)
        except:
            pass
        time.sleep(240)

threading.Thread(target=self_keepalive, daemon=True).start()

# ================= NIFTY 50 SET =================

NIFTY50 = [
    "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK",
    "SBIN","LT","ITC","KOTAKBANK","AXISBANK",
    "BAJFINANCE","MARUTI","TITAN","SUNPHARMA"
]

# ================= ML MODEL =================

model = LogisticRegression()

def extract_features(symbol):
    try:
        inst_token = kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["instrument_token"]
        data = kite.historical_data(
            instrument_token=inst_token,
            from_date=datetime.now().replace(hour=9, minute=15),
            to_date=datetime.now(),
            interval="5minute"
        )
        if len(data) < 15: return None

        closes = np.array([c["close"] for c in data])
        volumes = np.array([c["volume"] for c in data])

        ret1 = (closes[-1] - closes[-2]) / closes[-2]
        ret5 = (closes[-1] - closes[-6]) / closes[-6]
        vol_ratio = volumes[-1] / np.mean(volumes[-10:])
        trend = closes[-1] - np.mean(closes[-10:])

        return [ret1, ret5, vol_ratio, trend]
    except:
        return None

def train_dummy_model():
    # Training for 3 classes: 0=Sell, 1=No Trade, 2=Buy
    X, y = [], []
    for _ in range(300):
        r1, r5 = np.random.normal(0, 0.002), np.random.normal(0, 0.005)
        vr, tr = np.random.uniform(0.5, 2), np.random.normal(0, 1)
        score = r1 + r5 + (vr - 1) * 0.3 + tr * 0.01
        X.append([r1, r5, vr, tr])
        if score > 0.01: y.append(2)
        elif score < -0.01: y.append(0)
        else: y.append(1)
    model.fit(X, y)

train_dummy_model()

# ================= SIGNAL & ORDERS =================

def ai_signal(symbol):
    features = extract_features(symbol)
    if features is None: return None
    
    probs = model.predict_proba([features])[0]
    # probs index: 0=SELL, 1=WAIT, 2=BUY
    if probs[2] > 0.65: return "BUY"
    if probs[0] > 0.65: return "SELL"
    return None

def get_ltp(symbol):
    try:
        return kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["last_price"]
    except:
        return None

def place_trade(symbol, side):
    price = get_ltp(symbol)
    if not price: return
    qty = max(1, int(CAPITAL / price))
    
    t_type = kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL
    
    try:
        kite.place_order(
            variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NSE,
            tradingsymbol=symbol, transaction_type=t_type,
            quantity=qty, order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS
        )
        trailing_tracker[symbol] = price 
        print(f"ENTRY {side}: {symbol} at {price}")
    except Exception as e:
        print("Order error:", e)

def square_off_all():
    try:
        for p in kite.positions()["net"]:
            if p["quantity"] != 0:
                side = kite.TRANSACTION_TYPE_SELL if p["quantity"] > 0 else kite.TRANSACTION_TYPE_BUY
                kite.place_order(
                    variety=kite.VARIETY_REGULAR, exchange=p["exchange"],
                    tradingsymbol=p["tradingsymbol"], transaction_type=side,
                    quantity=abs(p["quantity"]), order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS
                )
        trailing_tracker.clear()
    except:
        pass

# ================= TRADING ENGINE =================

def trading_engine():
    global trailing_tracker
    while True:
        if not AUTO_TRADE or not auth_active:
            time.sleep(30); continue

        now = datetime.now().time()
        if now > datetime.strptime("15:20", "%H:%M").time():
            square_off_all(); time.sleep(60); continue
        if now < datetime.strptime("09:20", "%H:%M").time():
            time.sleep(60); continue

        try:
            positions = kite.positions()["net"]
            active_pos = [p for p in positions if p["quantity"] != 0]

            if active_pos:
                for pos in active_pos:
                    sym = pos["tradingsymbol"]
                    ltp = get_ltp(sym)
                    if not ltp: continue

                    entry_price = float(pos["average_price"])
                    qty = pos["quantity"]
                    
                    if sym not in trailing_tracker: trailing_tracker[sym] = ltp

                    if qty > 0: # LONG
                        if ltp > trailing_tracker[sym]: trailing_tracker[sym] = ltp
                        tp_hit = ltp >= (entry_price * 1.002)
                        sl_hit = ltp <= (trailing_tracker[sym] * 0.995)
                    else: # SHORT
                        if ltp < trailing_tracker[sym]: trailing_tracker[sym] = ltp
                        tp_hit = ltp <= (entry_price * 0.998)
                        sl_hit = ltp >= (trailing_tracker[sym] * 1.005)

                    if tp_hit or sl_hit:
                        print(f"EXIT {'TP' if tp_hit else 'TSL'}: {sym}")
                        square_off_all()
                
                time.sleep(SCAN_INTERVAL); continue
        except Exception as e:
            print(f"Engine Error: {e}")

        print("Scanning for signals...")
        for symbol in NIFTY50:
            signal = ai_signal(symbol)
            if signal:
                place_trade(symbol, signal)
                break

        time.sleep(SCAN_INTERVAL)

threading.Thread(target=trading_engine, daemon=True).start()

# ================= ROUTES =================

@app.route("/")
def home():
    return render_template("index.html", auth=auth_active)

@app.route("/login")
def login():
    return redirect(kite.login_url())

@app.route("/callback")
def callback():
    global auth_active
    request_token = request.args.get("request_token")
    session = kite.generate_session(request_token, api_secret=API_SECRET)
    kite.set_access_token(session["access_token"])
    auth_active = True
    return redirect("/")

@app.route("/positions")
def positions():
    return jsonify(kite.positions()) if auth_active else jsonify({"error": "Auth failed"})

@app.route("/squareoff")
def manual_squareoff():
    square_off_all()
    return "Squared off"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
