import os, time, threading, requests, math
import numpy as np
from datetime import datetime, timedelta
from flask import Flask, redirect, request, jsonify, render_template
from kiteconnect import KiteConnect
from sklearn.linear_model import LogisticRegression

# ================= CONFIG =================
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
RENDER_URL = os.environ.get("RENDER_URL", "https://your-service-name.onrender.com")
INDEX_SYMBOL = "NSE:NIFTY 50"
INDEX_TOKEN = 256265  # Nifty 50 Token
STRIKE_GAP = 50
CAPITAL = 5000
SCAN_INTERVAL = 60
AUTO_TRADE = True

# Global TP/SL Managed via UI
risk_params = {"tp_pct": 20.0, "sl_pct": 10.0} 

app = Flask(__name__)
kite = KiteConnect(api_key=API_KEY)
auth_active = False
last_error = None

# ================= KEEP ALIVE =================
def self_keepalive():
    while True:
        try: requests.get(f"{RENDER_URL}/ping", timeout=10)
        except: pass
        time.sleep(240)

threading.Thread(target=self_keepalive, daemon=True).start()

# ================= ML MODEL (Logic from 2nd Program) =================
model = LogisticRegression(solver='lbfgs')

def train_model():
    X, y = [], []
    for _ in range(300):
        r1, r5 = np.random.normal(0, 0.002), np.random.normal(0, 0.005)
        vr = np.random.uniform(0.5, 2)
        score = r1 + r5 + (vr - 1) * 0.3
        X.append([r1, r5, vr])
        y.append(2 if score > 0.01 else (0 if score < -0.01 else 1))
    model.fit(np.array(X), np.array(y))

train_model()

# ================= OPTIONS LOGIC =================
def get_atm_strike():
    ltp = kite.ltp(INDEX_SYMBOL)[INDEX_SYMBOL]["last_price"]
    return int(round(ltp / STRIKE_GAP) * STRIKE_GAP)

def get_weekly_expiry_str():
    # Kite format for weekly: YYMDD (Oct=O, Nov=N, Dec=D)
    today = datetime.now()
    days_until_thu = (3 - today.weekday() + 7) % 7
    expiry = today + timedelta(days=days_until_thu)
    year = str(expiry.year)[2:]
    month = expiry.strftime('%m')
    if month == "10": month = "O"
    elif month == "11": month = "N"
    elif month == "12": month = "D"
    else: month = str(int(month))
    return f"{year}{month}{expiry.day:02d}"

# ================= TRADING ENGINE =================
def trading_engine():
    global last_error, auth_active
    while True:
        if not (AUTO_TRADE and auth_active):
            time.sleep(10); continue
        
        now = datetime.now().time()
        if now > datetime.strptime("15:20", "%H:%M").time():
            square_off_all(); time.sleep(60); continue

        try:
            positions = kite.positions()["net"]
            active_pos = [p for p in positions if p["quantity"] != 0]

            if not active_pos:
                # Entry Logic
                data = kite.historical_data(INDEX_TOKEN, datetime.now()-timedelta(days=1), datetime.now(), "5minute")
                closes = np.array([c["close"] for c in data])
                vols = np.array([c["volume"] for c in data])
                feat = [(closes[-1]-closes[-2])/closes[-2], (closes[-1]-closes[-6])/closes[-6], vols[-1]/np.mean(vols[-10:])]
                prob = model.predict_proba([feat])[0]
                
                signal = "CE" if prob[2] > 0.65 else ("PE" if prob[0] > 0.65 else None)
                if signal:
                    symbol = f"NIFTY{get_weekly_expiry_str()}{get_atm_strike()}{signal}"
                    kite.place_order(variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NFO, tradingsymbol=symbol, 
                                     transaction_type=kite.TRANSACTION_TYPE_BUY, quantity=50, 
                                     order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS)
            else:
                # TP/SL Exit Logic
                for p in active_pos:
                    ltp = kite.ltp(f"NFO:{p['tradingsymbol']}")[f"NFO:{p['tradingsymbol']}"]["last_price"]
                    pnl_pct = ((ltp - p["average_price"]) / p["average_price"]) * 100
                    if pnl_pct >= risk_params["tp_pct"] or pnl_pct <= -risk_params["sl_pct"]:
                        square_off_all()

        except Exception as e: last_error = str(e)
        time.sleep(SCAN_INTERVAL)

threading.Thread(target=trading_engine, daemon=True).start()

# ================= ROUTES =================
@app.route("/")
def home(): return render_template("index.html", auth=auth_active)

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
    except Exception as e: return str(e)

@app.route("/update_risk", methods=["POST"])
def update_risk():
    risk_params["tp_pct"] = float(request.json.get("tp"))
    risk_params["sl_pct"] = float(request.json.get("sl"))
    return jsonify(risk_params)

@app.route("/dashboard_data")
def dashboard_data():
    if not auth_active: return jsonify({"error": "Auth"}), 401
    return jsonify({"positions": kite.positions().get("net", []), "risk": risk_params, "error": last_error})

@app.route("/ping")
def ping(): return "pong"

def square_off_all():
    for p in kite.positions()["net"]:
        if p["quantity"] != 0:
            side = kite.TRANSACTION_TYPE_SELL if p["quantity"] > 0 else kite.TRANSACTION_TYPE_BUY
            kite.place_order(variety=kite.VARIETY_REGULAR, exchange=p["exchange"], tradingsymbol=p["tradingsymbol"], 
                             transaction_type=side, quantity=abs(p["quantity"]), order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS)

if __name__ == "__main__":
    # Use the PORT env var provided by Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

