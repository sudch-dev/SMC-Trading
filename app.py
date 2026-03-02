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


# ================= KEEP ALIVE =================

@app.route("/ping")
def ping():
    return "pong"


def self_keepalive():
    while True:
        try:
            requests.get(
                "https://smc-trading.onrender.com/ping",
                timeout=10
            )
        except:
            pass
        time.sleep(240)


threading.Thread(target=self_keepalive, daemon=True).start()


# ================= NIFTY 50 (sample liquid set) =================

NIFTY50 = [
    "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK",
    "SBIN","LT","ITC","KOTAKBANK","AXISBANK",
    "BAJFINANCE","MARUTI","TITAN","SUNPHARMA"
]


# ================= ML MODEL =================

model = LogisticRegression()

def extract_features(symbol):
    """Create ML features from price + volume"""

    try:
        data = kite.historical_data(
            instrument_token=kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["instrument_token"],
            from_date=datetime.now().replace(hour=9, minute=15),
            to_date=datetime.now(),
            interval="5minute"
        )

        if len(data) < 15:
            return None

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
    """Synthetic training so model exists (online learning)"""

    X = []
    y = []

    for _ in range(200):
        r1 = np.random.normal(0, 0.002)
        r5 = np.random.normal(0, 0.005)
        vr = np.random.uniform(0.5, 2)
        tr = np.random.normal(0, 1)

        score = r1 + r5 + (vr - 1) * 0.3 + tr * 0.01

        X.append([r1, r5, vr, tr])
        y.append(1 if score > 0 else 0)

    model.fit(X, y)


train_dummy_model()


# ================= SIGNAL =================

def ai_signal(symbol):

    features = extract_features(symbol)
    if features is None:
        return False

    prob = model.predict_proba([features])[0][1]

    # confidence threshold
    return prob > 0.65


# ================= ORDER ENGINE =================

def get_ltp(symbol):
    try:
        return kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["last_price"]
    except:
        return None


def place_trade(symbol):

    price = get_ltp(symbol)
    if not price:
        return

    qty = max(1, int(CAPITAL / price))

    try:
        kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_MIS
        )

        print("TRADE:", symbol, qty)

    except Exception as e:
        print("Order error:", e)


def has_position():
    try:
        return any(p["quantity"] != 0 for p in kite.positions()["net"])
    except:
        return False


def square_off_all():
    try:
        for p in kite.positions()["net"]:
            if p["quantity"] != 0:
                kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=p["exchange"],
                    tradingsymbol=p["tradingsymbol"],
                    transaction_type=kite.TRANSACTION_TYPE_SELL
                    if p["quantity"] > 0 else kite.TRANSACTION_TYPE_BUY,
                    quantity=abs(p["quantity"]),
                    order_type=kite.ORDER_TYPE_MARKET,
                    product=kite.PRODUCT_MIS
                )
    except:
        pass


# ================= AUTO TRADING LOOP =================

def trading_engine():

    while True:

        if not AUTO_TRADE or not auth_active:
            time.sleep(30)
            continue

        now = datetime.now().time()

        if now < datetime.strptime("09:20", "%H:%M").time() \
           or now > datetime.strptime("15:20", "%H:%M").time():

            if now > datetime.strptime("15:20", "%H:%M").time():
                square_off_all()

            time.sleep(60)
            continue

        if has_position():
            time.sleep(SCAN_INTERVAL)
            continue

        print("AI scanning market...")

        for symbol in NIFTY50:

            if ai_signal(symbol):
                place_trade(symbol)
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

    session = kite.generate_session(
        request_token,
        api_secret=API_SECRET
    )

    access_token = session["access_token"]
    kite.set_access_token(access_token)

    auth_active = True

    print("ACCESS TOKEN:", access_token)

    return redirect("/")


@app.route("/positions")
def positions():
    if not auth_active:
        return jsonify({"error": "Not authenticated"})
    return jsonify(kite.positions())


@app.route("/orders")
def orders():
    if not auth_active:
        return jsonify({"error": "Not authenticated"})
    return jsonify(kite.orders())


@app.route("/squareoff")
def manual_squareoff():
    square_off_all()
    return "Squared off"


# ================= SERVER =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)