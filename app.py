import os, time, threading, requests, statistics
from flask import Flask, render_template, jsonify, redirect, request
from datetime import datetime
from pytz import timezone
from kiteconnect import KiteConnect

app = Flask(__name__)

# ========= CONFIG (SMC/Kite Terminology) =========
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
# access_token is set via the /callback route daily
ACCESS_TOKEN = os.environ.get("access_token") 

kite = KiteConnect(api_key=API_KEY)
if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)

TRADING_SYMBOL = "TPMV"
EXCHANGE = "NSE"
IST = timezone("Asia/Kolkata")

running = False
error_message = ""
status = {"msg": "Idle", "last": ""}
prices = []
entry = None

# ========= AUTH & LOGIN FLOW =========

@app.route("/login")
def login():
    """Step 1: Redirect user to Zerodha's login page"""
    return redirect(kite.login_url())

@app.route("/callback")
def callback():
    """Step 2: Catch the request_token and generate daily access_token"""
    global ACCESS_TOKEN, error_message
    request_token = request.args.get("request_token")
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        ACCESS_TOKEN = data["access_token"]
        kite.set_access_token(ACCESS_TOKEN)
        status["msg"] = "Authenticated"
        error_message = ""
        return redirect("/") # Redirect back to home dashboard
    except Exception as e:
        error_message = f"Login Failed: {str(e)}"
        return redirect("/")

# ========= MARKET & ENGINE (09:25 Entry) =========

def get_price():
    try:
        res = kite.ltp(f"{EXCHANGE}:{TRADING_SYMBOL}")
        return res[f"{EXCHANGE}:{TRADING_SYMBOL}"]["last_price"]
    except: return None

def bot_loop():
    global running, error_message, entry
    while running:
        try:
            now = datetime.now(IST)
            current_time = now.strftime("%H:%M")

            # Market Hours check (09:15 - 15:30)
            if not ("09:15" <= current_time <= "15:30"):
                status["msg"] = "Market Closed"
                time.sleep(60); continue

            price = get_price()
            if price:
                prices.append(price)
                if len(prices) > 100: prices.pop(0)

                # TRIGGER ENTRY AT 09:25 AM
                if current_time >= "09:25" and not entry:
                    if len(prices) > 20 and price > statistics.mean(prices[-20:]):
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

# ========= ROUTES =========

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/status")
def stat():
    return jsonify({
        "status": status["msg"],
        "last": status["last"],
        "authenticated": bool(ACCESS_TOKEN),
        "error": error_message
    })

@app.route("/start", methods=["POST"])
def start():
    global running
    if not ACCESS_TOKEN: return jsonify({"status": "failed", "reason": "No access_token"})
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
    """Prevents Render from sleeping"""
    while True:
        try: requests.get("https://smc-trading.onrender.com", timeout=10)
        except: pass
        time.sleep(240)

threading.Thread(target=self_keepalive, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
