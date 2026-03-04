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
# Ensure this matches your Render Service name exactly
RENDER_URL = os.environ.get("RENDER_URL", "https://smc-trading.onrender.com")

CAPITAL = 5000
SCAN_INTERVAL = 300 
AUTO_TRADE = True

app = Flask(__name__)
kite = KiteConnect(api_key=API_KEY)

auth_active = False
current_token = os.environ.get("access_token")

if current_token:
    kite.set_access_token(current_token)
    auth_active = True

trailing_tracker = {}

# ================= STABILIZED KEEP ALIVE =================

@app.route("/ping")
def ping():
    return "pong"

def self_keepalive():
    """Pings the /ping route to prevent Render from sleeping"""
    time.sleep(60) 
    while True:
        try:
            # Ping the actual /ping route to keep the app active
            requests.get(f"{RENDER_URL}/ping", timeout=10)
        except Exception as e:
            print(f"Keep-alive skipped: {e}")
        time.sleep(240)

threading.Thread(target=self_keepalive, daemon=True).start()

# ================= ML MODEL =================

# Removed multi_class for compatibility with scikit-learn 1.4+
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

# ================= SIGNAL & ORDERS =================

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
        
        # Get class probabilities
        probs = model.predict_proba([feat])[0] 
        
        # Index 0: SELL, Index 1: WAIT, Index 2: BUY
        if probs[2] > 0.65: return "BUY"
        if probs[0] > 0.65: return "SELL"
    except:
        return None
    return None

def square_off_all():
    try:
        positions = kite.positions()["net"]
        for p in positions:
            if p["quantity"] != 0:
                side = kite.TRANSACTION_TYPE_SELL if p["quantity"] > 0 else kite.TRANSACTION_TYPE_BUY
                kite.place_order(variety=kite.VARIETY_REGULAR, exchange=p["exchange"], 
                                tradingsymbol=p["tradingsymbol"], transaction_type=side, 
                                quantity=abs(p["quantity"]), order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS)
        trailing_tracker.clear()
        print("ALL POSITIONS SQUARED OFF")
    except Exception as e: 
        print(f"Square-off error: {e}")

# ================= TRADING ENGINE =================

def trading_engine():
    global auth_active
    while True:
        if not (AUTO_TRADE and auth_active):
            time.sleep(30); continue

        # Verify Session
        try:
            kite.profile()
        except:
            print("Session Expired. Please Re-login via /login")
            auth_active = False
            continue

        now = datetime.now().time()
        if now > datetime.strptime("15:20", "%H:%M").time():
            square_off_all(); time.sleep(60); continue
        
        if now < datetime.strptime("09:20", "%H:%M").time():
            time.sleep(60); continue

        # TP/SL Monitor
        try:
            positions = kite.positions()["net"]
            active_pos = [p for p in positions if p["quantity"] != 0]
            if active_pos:
                for pos in active_pos:
                    sym = pos["tradingsymbol"]
                    ltp = kite.ltp(f"NSE:{sym}")[f"NSE:{sym}"]["last_price"]
                    entry, qty = float(pos["average_price"]), pos["quantity"]
                    
                    if sym not in trailing_tracker: trailing_tracker[sym] = ltp
                    
                    if qty > 0: # LONG
                        trailing_tracker[sym] = max(ltp, trailing_tracker[sym])
                        exit_now = (ltp >= entry * 1.002 or ltp <= trailing_tracker[sym] * 0.995)
                    else: # SHORT
                        trailing_tracker[sym] = min(ltp, trailing_tracker[sym])
                        exit_now = (ltp <= entry * 0.998 or ltp >= trailing_tracker[sym] * 1.005)

                    if exit_now: 
                        print(f"EXIT TRIGGERED FOR {sym}")
                        square_off_all()
                time.sleep(SCAN_INTERVAL); continue
        except: pass

        # Entry Scanning
        print("Scanning Nifty 50...")
        symbols = ["RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","SBIN","LT","ITC","KOTAKBANK","AXISBANK"]
        for symbol in symbols:
            signal = ai_signal(symbol)
            if signal:
                price = kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["last_price"]
                kite.place_order(variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NSE, tradingsymbol=symbol, 
                                transaction_type=kite.TRANSACTION_TYPE_BUY if signal == "BUY" else kite.TRANSACTION_TYPE_SELL,
                                quantity=max(1, int(CAPITAL / price)), order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS)
                trailing_tracker[symbol] = price
                print(f"ENTERED {signal}: {symbol} at {price}")
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
    global auth_active, current_token
    request_token = request.args.get("request_token")
    if not request_token:
        return "No request token found", 400
    try:
        session = kite.generate_session(request_token, api_secret=API_SECRET)
        current_token = session["access_token"]
        kite.set_access_token(current_token)
        auth_active = True
        print(f"Authenticated: {current_token[:5]}...")
        return redirect("/")
    except Exception as e:
        return f"Authentication Failed: {e}"

@app.route("/squareoff")
def manual_squareoff():
    square_off_all()
    return "Manual Square-off Executed"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
