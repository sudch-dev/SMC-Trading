from flask import Flask, redirect, request, render_template, jsonify
from kiteconnect import KiteConnect
import os
from dotenv import load_dotenv
from smc_logic import run_smc_scan

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")

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
    Returns the latest scan. Frontend consumes:
      { status, ts, budget, picks[], errors[], diag{} }
    """
    global smc_status
    if access_token:
        kite.set_access_token(access_token)
        try:
            smc_status = run_smc_scan(kite) or {}
        except Exception as e:
            smc_status = {"status": "error", "error": str(e)}
    else:
        smc_status = {"status": "error", "error": "Not logged in. Please complete Kite login."}
    return jsonify(smc_status)

# ---------------- Main ----------------

if __name__ == '__main__':
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
