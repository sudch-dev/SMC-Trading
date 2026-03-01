import os, time, threading, requests, json, statistics
from flask import Flask, render_template, jsonify, request
from datetime import datetime
from pytz import timezone
from kiteconnect import KiteConnect

app = Flask(__name__)

# ========= CONFIG =========
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
# The access_token is generated via the /login/callback route daily
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

kite = KiteConnect(api_key=API_KEY)
if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)

TRADING_SYMBOL = "TPMV" # Example Symbol
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
    """Step 1: Get the Login URL"""
    return f"Login here: {kite.login_url()}"

@app.route("/login/callback")
def callback():
    """Step 2: Handle redirect and generate daily Access Token"""
    global ACCESS_TOKEN
    request_token = request.args.get("request_token")
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        ACCESS_TOKEN = data["access_token"]
        # In a production Render env, you'd save this to a DB or Redis
        kite.set_access_token(ACCESS_TOKEN)
        return jsonify({"status": "Authenticated", "access_token": ACCESS_TOKEN})
    except Exception as e:
        return str(e)

# ========= MARKET UTILS =========

def get_price():
    try:
        quote = kite.quote(f"{EXCHANGE}:{TRADING_SYMBOL}")
        return quote[f"{EXCHANGE}:{TRADING_SYMBOL}"]["last_price"]
    except Exception as e:
        return None

def get_margins():
    try:
        margin = kite.margins()
        return margin["equity"]["net"]
    except:
        return 0

# ========= SIGNAL ENGINE =========

def trade_signal(price):
    if len(prices) < 30: return None
    
    short_ma = statistics.mean(prices[-5:])
    long_ma = statistics.mean(prices[-20:])
    
    # Simple Momentum Crossover
    if short_ma > long_ma and price > max(prices[-10:-1]):
        return "BUY"
    if short_ma < long_ma and price < min(prices[-10:-1]):
        return "SELL"
    return None

# ========= ENGINE =========

def bot_loop():
    global running, error_message, entry

    while running:
        try:
            now_ist = datetime.now(IST)
            current_time = now_ist.strftime("%H:%M")
            
            # Market Hours check (09:15 - 15:30)
            if not (time(9, 15) <= now_ist.time() <= time(15, 30)):
                status["msg"] = "Market Closed"
                time.sleep(60)
                continue

            price = get_price()
            if not price:
                time.sleep(2)
                continue

            prices.append(price)
            if len(prices) > 100: prices.pop(0)

            # TRADE LOGIC STARTING AT 09:25 AM
            if current_time >= "09:25" and not entry:
                signal = trade_signal(price)
                if signal:
                    # Place Market Order
                    order_id = kite.place_order(
                        variety=kite.VARIETY_REGULAR,
                        exchange=kite.EXCHANGE_NSE,
                        tradingsymbol=TRADING_SYMBOL,
                        transaction_type=kite.TRANSACTION_TYPE_BUY if signal == "BUY" else kite.TRANSACTION_TYPE_SELL,
                        quantity=1,
                        product=kite.PRODUCT_MIS,
                        order_type=kite.ORDER_TYPE_MARKET
                    )
                    if order_id:
                        entry = {"side": signal, "price": price}

            status["msg"] = "Running" if not entry else f"In Trade {TRADING_SYMBOL}"
            status["last"] = now_ist.strftime("%H:%M:%S")

        except Exception as e:
            error_message = f"Loop Error: {str(e)}"
        
        time.sleep(5)

# ========= SYSTEM ROUTES =========

@app.route("/")
def home():
    return "SMC Trading Bot Active"

@app.route("/start", methods=["POST"])
def start():
    global running
    if not running and ACCESS_TOKEN:
        running = True
        threading.Thread(target=bot_loop, daemon=True).start()
        return jsonify({"status": "started"})
    return jsonify({"status": "failed", "reason": "No Access Token or already running"})

@app.route("/ping")
def ping():
    return "pong"

def self_keepalive():
    while True:
        try:
            # Updated to your Render URL
            requests.get("https://smc-trading.onrender.com", timeout=10)
        except:
            pass
        time.sleep(240)

threading.Thread(target=self_keepalive, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
