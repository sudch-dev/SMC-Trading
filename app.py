from flask import Flask, redirect, request, render_template, jsonify
from kiteconnect import KiteConnect
import os
from dotenv import load_dotenv
from smc_logic import run_smc_scan

# --- Keepalive deps ---
import threading
import time
import requests
from datetime import datetime
import pytz

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")

# Render keep-alive config
# Set KEEPALIVE_URL to something like: https://your-app.onrender.com/ping
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "").strip()
KEEPALIVE_INTERVAL_SEC = int(os.getenv("KEEPALIVE_INTERVAL_SEC", "240"))  # 20–30s works fine
ENABLE_KEEPALIVE = bool(KEEPALIVE_URL)

IST = pytz.timezone("Asia/Kolkata")

kite = KiteConnect(api_key=API_KEY)

access_token = None
smc_status = {}

# ------------------ Routes ------------------

@app.route('/')
def home():
    return redirect('/login')

@app.route('/login')
def login():
    login_url = kite.login_url()
    return redirect(login_url)

@app.route('/callback')
def callback():
    global access_token
    request_token = request.args.get('request_token')
    if not request_token:
        return "Missing request_token", 400
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]
    kite.set_access_token(access_token)
    return redirect('/dashboard')

@app.route('/dashboard')
def dashboard():
    return render_template('index.html')

@app.route('/api/smc-status')
def api_smc_status():
    """
    Returns the latest SMC scan.
    If not logged in yet, returns an empty dict (frontend can show 'Login first').
    """
    global smc_status
    if access_token:
        kite.set_access_token(access_token)
        try:
            smc_status = run_smc_scan(kite) or {}
        except Exception as e:
            smc_status = {"error": str(e)}
    else:
        smc_status = {"error": "Not logged in. Please complete Kite login."}
    return jsonify(smc_status)

@app.route('/ping')
def ping():
    """Simple keep-alive endpoint; also handy for health checks."""
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    return jsonify({
        "status": "ok",
        "time": now_ist,
        "keepalive_enabled": ENABLE_KEEPALIVE,
        "keepalive_url": KEEPALIVE_URL
    })

# ---------------- Keep-alive worker ----------------

_keepalive_thread_started = False

def _keepalive_loop():
    """Periodically pings KEEPALIVE_URL to keep the Render dyno awake."""
    session = requests.Session()
    while True:
        try:
            # Use GET so Render shows it in logs; keep timeout small.
            r = session.get(KEEPALIVE_URL, timeout=60)
            # Optional: log minimal info to stdout
            print(f"[KEEPALIVE] {datetime.now(IST).strftime('%H:%M:%S')} → {r.status_code}")
        except Exception as e:
            print(f"[KEEPALIVE] error: {e}")
        time.sleep(KEEPALIVE_INTERVAL_SEC)

@app.before_request
def _start_keepalive_if_needed():
    """
    Start the keepalive thread once per process when the first request arrives.
    (Render's free tier does not allow true background workers; this pattern is safe.)
    """
    global _keepalive_thread_started
    if ENABLE_KEEPALIVE and not _keepalive_thread_started:
        _keepalive_thread_started = True
        t = threading.Thread(target=_keepalive_loop, daemon=True)
        t.start()
        print(f"[KEEPALIVE] started thread, url={KEEPALIVE_URL}, interval={KEEPALIVE_INTERVAL_SEC}s")

# ---------------- Main ----------------

if __name__ == '__main__':
    # Local dev note: set KEEPALIVE_URL to http://127.0.0.1:5000/ping if you want to see it run
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
