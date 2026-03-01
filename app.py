import os
import time
import threading
import requests
import numpy as np
import pandas as pd
from datetime import datetime, time as dtime

from flask import Flask, render_template, jsonify, redirect, request
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ================= CONFIG =================

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
BUDGET = float(os.getenv("BUDGET", 10000))

BASE_URL = "https://smc-trading.onrender.com"

kite = KiteConnect(api_key=API_KEY)

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)

running = False
status = {"state": "Stopped", "error": "", "last": ""}

SYMBOLS = ["TATAMOTORS", "ADANIENT"]
instrument_tokens = {}

# ================= KEEP ALIVE =================

def self_keepalive():
    while True:
        try:
            requests.get("https://smc-trading.onrender.com")
        except:
            pass
        time.sleep(240)

threading.Thread(target=self_keepalive, daemon=True).start()

# ================= LOGIN =================

@app.route("/login")
def login():
    return redirect(kite.login_url())

@app.route("/callback")
def callback():
    global ACCESS_TOKEN

    req_token = request.args.get("request_token")
    data = kite.generate_session(req_token, api_secret=API_SECRET)

    ACCESS_TOKEN = data["access_token"]
    os.environ["ACCESS_TOKEN"] = ACCESS_TOKEN
    kite.set_access_token(ACCESS_TOKEN)

    return "Login successful. Token stored for session."

# ================= MAP INSTRUMENTS =================

def map_instruments():
    global instrument_tokens
    instruments = kite.instruments("NSE")

    for ins in instruments:
        if ins["tradingsymbol"] in SYMBOLS:
            instrument_tokens[ins["tradingsymbol"]] = ins["instrument_token"]

# ================= DATA =================

def get_data(symbol):
    token = instrument_tokens[symbol]

    data = kite.historical_data(
        token,
        datetime.now().replace(hour=9, minute=15),
        datetime.now(),
        "5minute"
    )

    return pd.DataFrame(data)

# ================= INSTITUTIONAL AI =================

def compute_signal(df):

    if len(df) < 10:
        return None

    price = df["close"].iloc[-1]

    vwap = (df["volume"] *
           (df["high"] + df["low"] + df["close"]) / 3).cumsum() \
           / df["volume"].cumsum()

    vwap = vwap.iloc[-1]

    vol_ratio = df["volume"].iloc[-1] / \
                df["volume"].rolling(5).mean().iloc[-1]

    slope = df["close"].iloc[-1] - df["close"].iloc[-4]

    score = 0.4*slope + 0.4*vol_ratio + 0.2*(price - vwap)

    prob = 1 / (1 + np.exp(-score))
    prob = 0.75 * prob + 0.125

    if prob > 0.68:
        return "BUY"

    if prob < 0.32:
        return "SELL"

    return None

# ================= POSITION SIZE =================

def calc_qty(price):
    risk = BUDGET * 0.006
    stop = price * 0.005
    qty = int(risk / stop)
    return max(qty, 1)

# ================= ORDER =================

def place_order(symbol, side, price):

    qty = calc_qty(price)

    if os.getenv("ALLOW_ORDER_EXEC") != "1":
        return

    kite.place_order(
        exchange="NSE",
        tradingsymbol=symbol,
        transaction_type=side,
        quantity=qty,
        order_type="MARKET",
        product="MIS",
        variety="regular"
    )

# ================= SQUARE OFF =================

def square_off_all():

    positions = kite.positions()["net"]

    for p in positions:

        if p["quantity"] != 0:

            side = "SELL" if p["quantity"] > 0 else "BUY"

            kite.place_order(
                exchange="NSE",
                tradingsymbol=p["tradingsymbol"],
                transaction_type=side,
                quantity=abs(p["quantity"]),
                order_type="MARKET",
                product="MIS"
            )

# ================= MAIN LOOP =================

def bot_loop():

    global running, status

    map_instruments()

    while running:

        now = datetime.now().time()

        try:

            if dtime(9,25) <= now <= dtime(14,50):

                for sym in SYMBOLS:

                    df = get_data(sym)
                    signal = compute_signal(df)

                    if signal:
                        price = df["close"].iloc[-1]
                        place_order(sym, signal, price)

            if now >= dtime(15,10):
                square_off_all()

        except Exception as e:
            status["error"] = str(e)

        status["last"] = datetime.now().strftime("%H:%M:%S")
        time.sleep(60)

# ================= CONTROL =================

@app.route("/start")
def start():
    global running, status

    if not running:
        running = True
        status["state"] = "Running"
        threading.Thread(target=bot_loop, daemon=True).start()

    return "Started"

@app.route("/stop")
def stop():
    global running, status
    running = False
    status["state"] = "Stopped"
    return "Stopped"

@app.route("/status")
def get_status():

    portfolio = {"INR": 0}

    try:
        margins = kite.margins()["equity"]
        portfolio["INR"] = margins["available"]["cash"]
    except:
        pass

    return jsonify({
        "status": status["state"],
        "last": status["last"],
        "error": status["error"],
        "portfolio": portfolio
    })

# ================= UI =================

@app.route("/")
def index():
    return render_template("index.html")

# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)