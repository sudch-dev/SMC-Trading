import os, time, threading, requests, statistics
from flask import Flask, render_template, jsonify, redirect, request
from datetime import datetime, time as dt_time
from pytz import timezone
from kiteconnect import KiteConnect

app = Flask(__name__)

# ========= CONFIG =========
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
# Token is volatile; generated daily via login
ACCESS_TOKEN = None 

kite = KiteConnect(api_key=API_KEY)
TRADING_SYMBOL = "TPMV"
EXCHANGE = "NSE"
IST = timezone("Asia/Kolkata")

running = False
error_message = ""
status = {"msg": "Idle", "last": ""}
prices = []
entry = None

# ========= AUTH FLOW =========

@app.route("/login")
def login():
    """Step 1: Redirect user to Zerodha's secure login page"""
    return redirect(kite.login_url())

@app.route("/login/callback")
def callback():
    """Step 2: Catch the request_token and generate daily session"""
    global ACCESS_TOKEN, error_message
    request_token = request.args.get("request_token")
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        ACCESS_TOKEN = data["access_token"]
        kite.set_access_token(ACCESS_TOKEN)
        error_message = ""
        return redirect("/") # Go back to dashboard
    except Exception as e:
        error_message = f"Login Failed: {str(e)}"
        return redirect("/")

# ========= MARKET & ENGINE =========

def get_price():
    try:
        # Fetching LTP for the specific symbol
        return kite.ltp(f"{EXCHANGE}:{TRADING_SYMBOL}")[f"{EXCHANGE}:{TRADING_SYMBOL}"]["last_price"]
    except: return None

def bot_loop():
    global running, error_message, entry
    while running:
        try:
            now = datetime.now(IST)
            # Market check: 09:15 - 15:30
            if not (dt_time(9, 15) <= now.time() <= dt_time(15, 30)):
                status["msg"] = "Market Closed"
                time.sleep(60); continue

            price = get_price()
            if price:
                prices.append(price)
                if len(prices) > 100: prices.pop(0)

                # TRIGGER ENTRY AT 09:25 AM
                if now.strftime("%H:%M") >= "09:25" and not entry:
                    # Basic Strategy Example
                    if len(prices) > 30 and price > statistics.mean(prices[-20:]):
                        order_id = kite.place_order(
                            variety=kite.VARIETY_REGULAR,
                            exchange=kite.EXCHANGE_NSE,
                            tradingsymbol=TRADING_SYMBOL,
                            transaction_type=kite.TRANSACTION_TYPE_BUY,
                            quantity=1,
                            product=kite.PRODUCT_MIS,
                            order_type=kite.ORDER_TYPE_MARKET
                        )
                        entry = {"id": order_id, "price": price}

            status["msg"] = "Running" if not entry else "In Trade"
            status["last"] = now.strftime("%H:%M:%S")
        except Exception as e:
            error_message = f"Loop Error: {str(e)}"
        time.sleep(5)

# ========= SYSTEM ROUTES =========

@app.route("/")
def home():
    return render_template("index.html", authenticated=bool(ACCESS_TOKEN), status=status, error=error_message)

@app.route("/start", methods=["POST"])
def start():
    global running
    if not ACCESS_TOKEN: return jsonify({"status": "error", "reason": "Login Required"})
    if not running:
        running = True
        threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/stop", methods=["POST"])
def stop():
    global running
    running = False
    return jsonify({"status": "stopped"})

@app.route("/ping")
def ping(): return "pong"

def self_keepalive():
    while True:
        try: requests.get("https://smc-trading.onrender.com", timeout=10)
        except: pass
        time.sleep(600) # Ping every 10 mins to keep Render awake

threading.Thread(target=self_keepalive, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
