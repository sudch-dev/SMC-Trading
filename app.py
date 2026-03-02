import os
import time
import threading
import requests
import numpy as np
import pandas as pd
import joblib
from flask import Flask, jsonify
from kiteconnect import KiteConnect
from datetime import datetime, timedelta

# ================= CONFIG =================

API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
access_token = os.environ.get("access_token")

CAPITAL = 5000
MAX_TRADES = 2
RISK_PER_DAY = 150
PROB_THRESHOLD = 0.72
INTERVAL = "5minute"

RENDER_URL = "https://smc-trading.onrender.com"

# ================= INIT =================

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(access_token)

model = joblib.load("model.pkl")

running = True
daily_pnl = 0
open_positions = {}

# ================= NIFTY 50 =================

NIFTY50 = [
"RELIANCE","TCS","HDFCBANK","ICICIBANK","INFY","HINDUNILVR",
"ITC","SBIN","BHARTIARTL","KOTAKBANK","LT","AXISBANK",
"ASIANPAINT","MARUTI","SUNPHARMA","ULTRACEMCO","NTPC",
"TITAN","POWERGRID","ONGC","BAJFINANCE","BAJAJFINSV",
"WIPRO","NESTLEIND","ADANIENT","ADANIPORTS","HCLTECH",
"TATASTEEL","JSWSTEEL","INDUSINDBK","COALINDIA","GRASIM",
"TECHM","M&M","DRREDDY","DIVISLAB","HEROMOTOCO",
"CIPLA","EICHERMOT","APOLLOHOSP","BRITANNIA","UPL",
"BPCL","TATAMOTORS","SBILIFE","HDFCLIFE","ICICIPRULI",
"SHREECEM","BAJAJ-AUTO"
]

# ================= FEATURE ENGINE =================

def build_features(df):
    df["ret1"] = df["close"].pct_change()
    df["ret3"] = df["close"].pct_change(3)
    df["ret6"] = df["close"].pct_change(6)

    df["ema9"] = df["close"].ewm(span=9).mean()
    df["ema21"] = df["close"].ewm(span=21).mean()
    df["ema_diff"] = df["ema9"] - df["ema21"]

    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()

    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            abs(df["high"] - df["close"].shift()),
            abs(df["low"] - df["close"].shift())
        )
    )
    df["atr"] = df["tr"].rolling(14).mean()

    latest = df.iloc[-1]

    features = [
        latest["ret1"],
        latest["ret3"],
        latest["ret6"],
        latest["ema_diff"],
        latest["vol_ratio"],
        latest["atr"]
    ]

    return np.nan_to_num(features), latest["atr"], latest["close"]

# ================= TRADE EXECUTION =================

def enter_trade(symbol, direction, price, atr):
    global open_positions

    capital_per_trade = CAPITAL / MAX_TRADES
    qty = int(capital_per_trade / price)

    if qty <= 0:
        return

    if direction == "BUY":
        sl = price - 0.6 * atr
        tp = price + 1.2 * atr
        transaction_type = kite.TRANSACTION_TYPE_BUY
    else:
        sl = price + 0.6 * atr
        tp = price - 1.2 * atr
        transaction_type = kite.TRANSACTION_TYPE_SELL

    kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NSE,
        tradingsymbol=symbol,
        transaction_type=transaction_type,
        quantity=qty,
        order_type=kite.ORDER_TYPE_MARKET,
        product=kite.PRODUCT_MIS
    )

    open_positions[symbol] = {
        "direction": direction,
        "entry": price,
        "qty": qty,
        "sl": sl,
        "tp": tp
    }

# ================= POSITION MONITOR =================

def monitor_positions():
    global daily_pnl

    while running:
        try:
            for symbol in list(open_positions.keys()):
                ltp = kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["last_price"]
                pos = open_positions[symbol]

                if pos["direction"] == "BUY":
                    exit_cond = ltp <= pos["sl"] or ltp >= pos["tp"]
                    pnl = (ltp - pos["entry"]) * pos["qty"]
                    exit_type = kite.TRANSACTION_TYPE_SELL
                else:
                    exit_cond = ltp >= pos["sl"] or ltp <= pos["tp"]
                    pnl = (pos["entry"] - ltp) * pos["qty"]
                    exit_type = kite.TRANSACTION_TYPE_BUY

                if exit_cond:
                    kite.place_order(
                        variety=kite.VARIETY_REGULAR,
                        exchange=kite.EXCHANGE_NSE,
                        tradingsymbol=symbol,
                        transaction_type=exit_type,
                        quantity=pos["qty"],
                        order_type=kite.ORDER_TYPE_MARKET,
                        product=kite.PRODUCT_MIS
                    )

                    daily_pnl += pnl
                    del open_positions[symbol]

        except:
            pass

        time.sleep(5)

# ================= SCREENER =================

def screener():
    global running

    while running:
        if daily_pnl <= -RISK_PER_DAY:
            break

        if len(open_positions) >= MAX_TRADES:
            time.sleep(60)
            continue

        try:
            for symbol in NIFTY50:
                if symbol in open_positions:
                    continue

                token = kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["instrument_token"]

                to_date = datetime.now()
                from_date = to_date - timedelta(days=5)

                data = kite.historical_data(
                    token,
                    from_date,
                    to_date,
                    INTERVAL
                )

                df = pd.DataFrame(data)
                if len(df) < 30:
                    continue

                features, atr, price = build_features(df)
                prob = model.predict_proba([features])[0][1]

                if prob > PROB_THRESHOLD:
                    enter_trade(symbol, "BUY", price, atr)

                elif (1 - prob) > PROB_THRESHOLD:
                    enter_trade(symbol, "SELL", price, atr)

                if len(open_positions) >= MAX_TRADES:
                    break

        except:
            pass

        time.sleep(300)

# ================= KEEP ALIVE =================

@app.route("/ping")
def ping():
    return "pong", 200

def self_keepalive():
    while True:
        try:
            requests.get(f"{RENDER_URL}/ping", timeout=10)
        except:
            pass
        time.sleep(240)

# ================= STATUS =================

@app.route("/status")
def status():
    return jsonify({
        "running": running,
        "daily_pnl": daily_pnl,
        "open_positions": open_positions
    })

# ================= START THREADS =================

threading.Thread(target=monitor_positions, daemon=True).start()
threading.Thread(target=screener, daemon=True).start()
threading.Thread(target=self_keepalive, daemon=True).start()

# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)