
from flask import Flask, redirect, request, render_template, jsonify
from kiteconnect import KiteConnect
import os
from dotenv import load_dotenv
from smc_logic import run_smc_scan

load_dotenv()

app = Flask(__name__)

kite = KiteConnect(api_key=os.getenv("API_KEY"))

access_token = None
smc_status = {}

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
    data = kite.generate_session(request_token, api_secret=os.getenv("API_SECRET"))
    access_token = data["access_token"]
    kite.set_access_token(access_token)
    return redirect('/dashboard')

@app.route('/dashboard')
def dashboard():
    return render_template('index.html')

@app.route('/api/smc-status')
def api_smc_status():
    global smc_status
    if access_token:
        kite.set_access_token(access_token)
        smc_status = run_smc_scan(kite)
    return jsonify(smc_status)
