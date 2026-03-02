import os, time, threading, statistics, requests
from flask import Flask, render_template, jsonify, redirect, request
from datetime import datetime, time as dt_time
from pytz import timezone
from kiteconnect import KiteConnect

app = Flask(__name__)

# ========= CONFIG =========
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
ACCESS_TOKEN = None 

kite = KiteConnect(api_key=API_KEY)
IST = timezone("Asia/Kolkata")

SCRIPTS = ["HDFCBANK", "INFY", "RELIANCE", "BHARTIARTL", "TCS"]
EXCHANGE = "NSE"
MAX_TOTAL_MARGIN = 5000 
MARGIN_PER_STOCK = MAX_TOTAL_MARGIN // len(SCRIPTS)

# Global State
running = False
status = {"msg": "Idle", "last": "-", "pnl": 0, "funds": 0}
portfolio = {s: {"prices": [], "entry": None, "trades": 0} for s in SCRIPTS}

# ========= AUTH =========
@app.route("/login")
def login(): return redirect(kite.login_url())

@app.route("/callback")
def callback():
    global ACCESS_TOKEN
    token = request.args.get("request_token")
    try:
        data = kite.generate_session(token, api_secret=API_SECRET)
        ACCESS_TOKEN = data["access_token"]
        kite.set_access_token(ACCESS_TOKEN)
        return redirect("/")
    except: return "Auth Failed"

# ========= TRADING LOGIC =========

def bot_loop():
    global running
    while running:
        try:
            now = datetime.now(IST)
            # Market check
            if not (dt_time(9, 15) <= now.time() <= dt_time(15, 30)):
                status["msg"] = "Market Closed"
                time.sleep(30); continue

            # 1. Update Global Funds (Safely navigation nested dict)
            margins = kite.margins()
            status["funds"] = margins.get("equity", {}).get("available", {}).get("live_balance", 0)
            
            # 2. Update LTP and Prices
            query = [f"{EXCHANGE}:{s}" for s in SCRIPTS]
            quotes = kite.ltp(query)
            total_pnl = 0

            for symbol in SCRIPTS:
                full_sym = f"{EXCHANGE}:{symbol}"
                
                # Check if symbol exists in response to avoid fetch errors
                if full_sym not in quotes: continue
                
                price = quotes[full_sym]["last_price"]
                data = portfolio[symbol]
                data["prices"].append(price)
                if len(data["prices"]) > 50: data["prices"].pop(0)

                # 3. Manage Position
                if data["entry"]:
                    trade = data["entry"]
                    pnl = (price - trade["price"]) * trade["qty"]
                    total_pnl += pnl

                    if price <= trade["sl"] or price >= trade["tp"]:
                        kite.place_order(variety=kite.VARIETY_REGULAR, exchange=EXCHANGE, tradingsymbol=symbol,
                                         transaction_type=kite.TRANSACTION_TYPE_SELL, quantity=trade["qty"],
                                         product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET)
                        data["entry"] = None
                
                # 4. Entry Logic (EMA Pullback)
                elif len(data["prices"]) >= 20:
                    ema = statistics.mean(data["prices"][-20:])
                    if price > ema and price <= ema * 1.001:
                        qty = int(MARGIN_PER_STOCK / price)
                        if qty < 1: continue
                        
                        order_id = kite.place_order(variety=kite.VARIETY_REGULAR, exchange=EXCHANGE, tradingsymbol=symbol,
                                                    transaction_type=kite.TRANSACTION_TYPE_BUY, quantity=qty,
                                                    product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET)
                        
                        order_history = kite.order_history(order_id)
                        exchange_id = order_history[-1].get("exchange_order_id", "PENDING")
                        
                        data["entry"] = {
                            "ex_id": exchange_id, "qty": qty, "price": price,
                            "sl": price * 0.995, "tp": price * 1.01
                        }

            status["pnl"] = round(total_pnl, 2)
            status["msg"] = "Active"
            status["last"] = now.strftime("%H:%M:%S")

        except Exception as e: status["msg"] = f"Err: {str(e)[:30]}"
        time.sleep(10)

# ========= API ROUTES =========
@app.route("/")
def home(): return render_template("index.html")

@app.route("/ping")
def ping(): return "pong"

@app.route("/status")
def stat():
    return jsonify({
        "status": status,
        "authenticated": bool(ACCESS_TOKEN),
        "scripts": {s: portfolio[s]["entry"] if portfolio[s]["entry"] else "Watching" for s in SCRIPTS}
    })

@app.route("/start", methods=["POST"])
def start():
    global running
    if ACCESS_TOKEN and not running:
        running = True
        threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"status": "started"})

# ========= KEEP ALIVE =========
def self_keepalive():
    """Prevents Render from sleeping."""
    while True:
        try:
            # Pings the /ping route specifically
            requests.get("https://coin-4k37.onrender.com", timeout=10)
        except: pass
        time.sleep(240)

if __name__ == "__main__":
    # Start keepalive before running the app
    threading.Thread(target=self_keepalive, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
