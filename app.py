```python
import os, time, threading, statistics, requests
from flask import Flask, render_template, jsonify, redirect, request
from datetime import datetime, time as dt_time
from pytz import timezone
from kiteconnect import KiteConnect
app = Flask(name) 
========= CONFIG =========
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET") 
Using small letters for env variable as requested
access_token = os.environ.get("access_token") 
kite = KiteConnect(api_key=API_KEY)
if access_token:
kite.set_access_token(access_token)
IST = timezone("Asia/Kolkata") 
SCRIPTS = ["HDFCBANK", "INFY", "RELIANCE", "BHARTIARTL", "TCS"]
EXCHANGE = "NSE"
MAX_TOTAL_MARGIN = 5000
MARGIN_PER_STOCK = MAX_TOTAL_MARGIN // len(SCRIPTS)
Global State
running = False
status = {"msg": "Idle", "last": "-", "pnl": 0, "funds": 0}
portfolio = {s: {"prices": [], "entry": None} for s in SCRIPTS}
========= AI/ML & ADVANCED STATS =========
def get_z_score(prices):
"""Calculates Z-Score to identify overextended market sentiment."""
if len(prices) < 30: return 0
mean = statistics.mean(prices[-30:])
stdev = statistics.stdev(prices[-30:])
return (prices[-1] - mean) / stdev if stdev > 0 else 0
def ml_logic_gate(prices):
"""Simulates Decision Tree logic checking Momentum, Volatility, and Trend."""
if len(prices) < 20: return 0
roc = ((prices[-1] - prices[-5]) / prices[-5]) * 100
vol = statistics.stdev(prices[-10:])
ema = statistics.mean(prices[-20:])
if prices[-1] > ema and roc > 0.12 and vol > 0.03:
return 1 # AI Buy Signal
elif prices[-1] < ema and roc < -0.12 and vol > 0.03:
return -1 # AI Short Signal
return 0
========= TRADING BOT CORE =========
def bot_loop():
global running
while running:
try:
now = datetime.now(IST)
# Protocol: Exit before 15:20 to avoid auto-squareoff charges
if not (dt_time(9, 15) <= now.time() <= dt_time(15, 20)):
status["msg"] = "Market Closed"
time.sleep(30); continue
# Sync Margins from Zerodha
margins = kite.margins()
status["funds"] = margins.get("equity", {}).get("available", {}).get("live_balance", 0)
# Fetch LTP
query = [f"{EXCHANGE}:{s}" for s in SCRIPTS]
quotes = kite.ltp(query)
total_pnl = 0
for symbol in SCRIPTS:
full_sym = f"{EXCHANGE}:{symbol}"
if full_sym not in quotes: continue
price = quotes[full_sym]["last_price"]
data = portfolio[symbol]
data["prices"].append(price)
if len(data["prices"]) > 60: data["prices"].pop(0)
# 1. Manage Active Position (Dynamic Exit)
if data["entry"]:
trade = data["entry"]
if trade["side"] == kite.TRANSACTION_TYPE_BUY:
pnl = (price - trade["price"]) * trade["qty"]
trigger = (price <= trade["sl"] or price >= trade["tp"])
exit_side = kite.TRANSACTION_TYPE_SELL
else: # Short logic
pnl = (trade["price"] - price) * trade["qty"]
trigger = (price >= trade["sl"] or price <= trade["tp"])
exit_side = kite.TRANSACTION_TYPE_BUY
total_pnl += pnl
if trigger:
kite.place_order(variety=kite.VARIETY_REGULAR, exchange=EXCHANGE, tradingsymbol=symbol,
transaction_type=exit_side, quantity=trade["qty"],
product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET)
data["entry"] = None
# 2. Entry Triggers (AI + Z-Score)
elif len(data["prices"]) >= 30:
z = get_z_score(data["prices"])
signal = ml_logic_gate(data["prices"])
side, sl, tp = None, 0, 0
# Sentiment Filtering (Avoid buying at peak or shorting at bottom)
if signal == 1 and -1 < z < 1.6:
side = kite.TRANSACTION_TYPE_BUY
sl, tp = price * 0.996, price * 1.012
elif signal == -1 and -1.6 < z < 1:
side = kite.TRANSACTION_TYPE_SELL
sl, tp = price * 1.004, price * 0.988
if side:
qty = int(MARGIN_PER_STOCK / price)
if qty >= 1:
kite.place_order(variety=kite.VARIETY_REGULAR, exchange=EXCHANGE, tradingsymbol=symbol,
transaction_type=side, quantity=qty,
product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET)
data["entry"] = {"qty": qty, "price": price, "sl": sl, "tp": tp, "side": side}
status.update({"pnl": round(total_pnl, 2), "msg": "AI Active", "last": now.strftime("%H:%M:%S")})
except Exception as e: status["msg"] = f"Err: {str(e)[:25]}"
time.sleep(10) 
========= FLASK ROUTES =========
@app.route("/")
def home(): return "AI Trading Engine Operational" 
@app.route("/ping")
def ping(): return "pong"
@app.route("/status")
def stat():
return jsonify({"status": status, "auth_active": bool(access_token)}) 
@app.route("/login")
def login(): return redirect(kite.login_url()) 
@app.route("/callback")
def callback():
global access_token
token = request.args.get("request_token")
try:
session = kite.generate_session(token, api_secret=API_SECRET)
access_token = session["access_token"]
kite.set_access_token(access_token)
return "Authentication Successful. Token updated in memory."
except: return "Auth Failed" 
@app.route("/start", methods=["POST"])
def start():
global running
if access_token and not running:
running = True
threading.Thread(target=bot_loop, daemon=True).start()
return jsonify({"status": "AI Logic Started"})
========= KEEP ALIVE =========
def self_keepalive():
while True:
try:
# Ping itself to prevent Render from sleeping
requests.get("smc-trading.onrender.com", timeout=10)
except: pass
time.sleep(240)
if name == "main":
threading.Thread(target=self_keepalive, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))) 