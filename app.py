import os
import time
import threading
import statistics
import requests

from flask import Flask, jsonify, redirect, request
from datetime import datetime, time as dt_time
from pytz import timezone
from kiteconnect import KiteConnect

app = Flask(__name__)

# ========= CONFIG =========

API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
access_token = os.environ.get("access_token")

kite = KiteConnect(api_key=API_KEY)

if access_token:
    kite.set_access_token(access_token)

IST = timezone("Asia/Kolkata")

SCRIPTS = ["HDFCBANK", "INFY", "RELIANCE", "BHARTIARTL", "TCS"]
EXCHANGE = "NSE"

MAX_TOTAL_MARGIN = 5000
MARGIN_PER_STOCK = MAX_TOTAL_MARGIN // len(SCRIPTS)

# ========= GLOBAL STATE =========

running = False

status = {
    "msg": "Idle",
    "last": "-",
    "pnl": 0,
    "funds": 0
}

portfolio = {s: {"prices": [], "entry": None} for s in SCRIPTS}

# ========= AI / ML & ADVANCED STATS =========

def get_z_score(prices):
    """Calculates Z-Score to identify overextended sentiment"""
    if len(prices) < 30:
        return 0

    mean = statistics.mean(prices[-30:])
    stdev = statistics.stdev(prices[-30:])

    return (prices[-1] - mean) / stdev if stdev > 0 else 0


def ml_logic_gate(prices):
    """Simulated decision tree using momentum + volatility"""
    if len(prices) < 20:
        return 0

    roc = ((prices[-1] - prices[-5]) / prices[-5]) * 100
    vol = statistics.stdev(prices[-10:])
    ema = statistics.mean(prices[-20:])

    if prices[-1] > ema and roc > 0.12 and vol > 0.03:
        return 1  # Buy
    elif prices[-1] < ema and roc < -0.12 and vol > 0.03:
        return -1  # Short

    return 0


# ========= TRADING BOT CORE =========

def bot_loop():
    global running

    while running:
        try:
            now = datetime.now(IST)

            # Trade only during market hours
            if not (dt_time(9, 15) <= now.time() <= dt_time(15, 20)):
                status["msg"] = "Market Closed"
                time.sleep(30)
                continue

            # Get funds
            margins = kite.margins()
            status["funds"] = margins.get("equity", {}) \
                                     .get("available", {}) \
                                     .get("live_balance", 0)

            # Fetch prices
            query = [f"{EXCHANGE}:{s}" for s in SCRIPTS]
            quotes = kite.ltp(query)

            total_pnl = 0

            for symbol in SCRIPTS:

                full_sym = f"{EXCHANGE}:{symbol}"
                if full_sym not in quotes:
                    continue

                price = quotes[full_sym]["last_price"]
                data = portfolio[symbol]

                data["prices"].append(price)
                if len(data["prices"]) > 60:
                    data["prices"].pop(0)

                # ===== Manage active trade =====
                if data["entry"]:
                    trade = data["entry"]

                    if trade["side"] == kite.TRANSACTION_TYPE_BUY:
                        pnl = (price - trade["price"]) * trade["qty"]
                        trigger = price <= trade["sl"] or price >= trade["tp"]
                        exit_side = kite.TRANSACTION_TYPE_SELL
                    else:
                        pnl = (trade["price"] - price) * trade["qty"]
                        trigger = price >= trade["sl"] or price <= trade["tp"]
                        exit_side = kite.TRANSACTION_TYPE_BUY

                    total_pnl += pnl

                    if trigger:
                        kite.place_order(
                            variety=kite.VARIETY_REGULAR,
                            exchange=EXCHANGE,
                            tradingsymbol=symbol,
                            transaction_type=exit_side,
                            quantity=trade["qty"],
                            product=kite.PRODUCT_MIS,
                            order_type=kite.ORDER_TYPE_MARKET
                        )
                        data["entry"] = None

                # ===== Entry Logic =====
                elif len(data["prices"]) >= 30:

                    z = get_z_score(data["prices"])
                    signal = ml_logic_gate(data["prices"])

                    side = None
                    sl = tp = 0

                    if signal == 1 and -1 < z < 1.6:
                        side = kite.TRANSACTION_TYPE_BUY
                        sl = price * 0.996
                        tp = price * 1.012

                    elif signal == -1 and -1.6 < z < 1:
                        side = kite.TRANSACTION_TYPE_SELL
                        sl = price * 1.004
                        tp = price * 0.988

                    if side:
                        qty = int(MARGIN_PER_STOCK / price)

                        if qty >= 1:
                            kite.place_order(
                                variety=kite.VARIETY_REGULAR,
                                exchange=EXCHANGE,
                                tradingsymbol=symbol,
                                transaction_type=side,
                                quantity=qty,
                                product=kite.PRODUCT_MIS,
                                order_type=kite.ORDER_TYPE_MARKET
                            )

                            data["entry"] = {
                                "qty": qty,
                                "price": price,
                                "sl": sl,
                                "tp": tp,
                                "side": side
                            }

            status.update({
                "pnl": round(total_pnl, 2),
                "msg": "AI Active",
                "last": now.strftime("%H:%M:%S")
            })

        except Exception as e:
            status["msg"] = f"Err: {str(e)[:40]}"

        time.sleep(10)


# ========= FLASK ROUTES =========

@app.route("/")
def home():
    return "AI Trading Engine Operational"


@app.route("/ping")
def ping():
    return "pong"


@app.route("/status")
def stat():
    return jsonify({
        "status": status,
        "auth_active": bool(access_token)
    })


@app.route("/login")
def login():
    return redirect(kite.login_url())


@app.route("/callback")
def callback():
    global access_token

    token = request.args.get("request_token")

    try:
        session = kite.generate_session(token, api_secret=API_SECRET)
        access_token = session["access_token"]
        kite.set_access_token(access_token)

        return "Authentication Successful"

    except:
        return "Auth Failed"


@app.route("/start", methods=["POST"])
def start():
    global running

    if access_token and not running:
        running = True
        threading.Thread(target=bot_loop, daemon=True).start()
        return jsonify({"status": "AI Logic Started"})

    return jsonify({"status": "Auth required or already running"})


# ========= KEEP ALIVE =========

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


# ========= MAIN =========

if __name__ == "__main__":
    threading.Thread(target=self_keepalive, daemon=True).start()

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )