# smc_logic.py
import os
from datetime import datetime, date, timedelta
from math import log1p

# ---------------- Configuration ----------------
BUDGET       = float(os.getenv("BUDGET", "10000"))       # rupees, used per-pick for LONG sizing
RING_STRIKES = int(os.getenv("RING_STRIKES", "4"))       # strikes on each side of ATM to include
MAX_STOCKS   = int(os.getenv("MAX_STOCKS", "50"))        # cap the NIFTY50 universe if needed

# TP/SL as PERCENT of option premium (1.5% default each side)
TP_PCT_LONG  = float(os.getenv("TP_PCT_LONG",  "0.015"))  # +1.5% for long premium
SL_PCT_LONG  = float(os.getenv("SL_PCT_LONG",  "0.015"))  # -1.5% for long premium
TP_PCT_SHORT = float(os.getenv("TP_PCT_SHORT", "0.015"))  # -1.5% (profit if price falls)
SL_PCT_SHORT = float(os.getenv("SL_PCT_SHORT", "0.015"))  # +1.5% (risk if price rises)

# NSE option tick size (₹0.05)
TICK_SIZE    = float(os.getenv("TICK_SIZE", "0.05"))

DEBUG_SCAN   = os.getenv("DEBUG_SCAN", "0").lower() in ("1", "true", "yes")

# NIFTY-50 names (tradingsymbols used by instruments "name")
NIFTY50 = [
    "RELIANCE","HDFCBANK","ICICIBANK","INFY","TCS","ITC","SBIN","BHARTIARTL","AXISBANK","KOTAKBANK",
    "ASIANPAINT","ADANIENT","HCLTECH","MARUTI","BAJFINANCE","SUNPHARMA","TITAN","ULTRACEMCO","NTPC","WIPRO",
    "NESTLEIND","ONGC","M&M","POWERGRID","JSWSTEEL","TATASTEEL","COALINDIA","HINDUNILVR","BAJAJFINSV","TECHM",
    "GRASIM","HDFCLIFE","DIVISLAB","BRITANNIA","DRREDDY","INDUSINDBK","TATAMOTORS","BAJAJ-AUTO","HEROMOTOCO","CIPLA",
    "EICHERMOT","LTIM","HINDALCO","BPCL","ADANIPORTS","SHRIRAMFIN","UPL","APOLLOHOSP","LT","BRITANNIA"
][:MAX_STOCKS]

# ---------------- Caches ----------------
_INSTR_CACHE = {"loaded": False, "nfo": [], "nse": []}
_TOKEN_CACHE = {}   # NSE underlying token map (symbol -> instrument_token)


# ---------------- Utilities ----------------
def _load_instruments(kite):
    if not _INSTR_CACHE["loaded"]:
        _INSTR_CACHE["nfo"] = kite.instruments("NFO") or []
        _INSTR_CACHE["nse"] = kite.instruments("NSE") or []
        _INSTR_CACHE["loaded"] = True
    return _INSTR_CACHE["nfo"], _INSTR_CACHE["nse"]

def _map_tokens(nse_rows, symbols):
    """Build NSE underlying token map once."""
    global _TOKEN_CACHE
    if _TOKEN_CACHE:
        return _TOKEN_CACHE
    wanted = set(symbols)
    for r in nse_rows:
        tsym = r.get("tradingsymbol")
        if tsym in wanted:
            _TOKEN_CACHE[tsym] = r.get("instrument_token")
            if len(_TOKEN_CACHE) == len(wanted):
                break
    return _TOKEN_CACHE

def _to_date(obj):
    if not obj: return None
    try: return obj.date()
    except Exception: return obj

def _ema(vals, p):
    if not vals: return None
    k = 2/(p+1); e = float(vals[0])
    for v in vals: e = float(v)*k + e*(1-k)
    return e

def _rsi(closes, p=14):
    if len(closes) < p+1: return None
    g=l=0.0
    for i in range(1, p+1):
        d=closes[i]-closes[i-1]
        g += d if d>0 else 0
        l += -d if d<0 else 0
    if l==0: return 100.0
    rs=(g/p)/(l/p)
    return 100 - (100/(1+rs))

def _pivots(h,l,c):
    pp=(h+l+c)/3.0
    r1=2*pp-l; s1=2*pp-h
    return pp,r1,s1

def _nearest_expiry(rows):
    today=date.today()
    exps=sorted({_to_date(r["expiry"]) for r in rows if r.get("expiry")})
    for d in exps:
        if d>=today: return d
    return exps[-1] if exps else None

def _ring(strikes, atm, steps):
    try: i=strikes.index(atm)
    except ValueError: i=min(range(len(strikes)), key=lambda k: abs(strikes[k]-atm))
    lo=max(0, i-steps); hi=min(len(strikes)-1, i+steps)
    return set(strikes[lo:hi+1])

def _tick_round(x, tick=TICK_SIZE):
    """Round any price to the nearest exchange tick (₹0.05 by default)."""
    if x is None: return None
    return round(round(float(x)/tick)*tick, 2)

def _score(q, lot, ltp, trade_type):
    """Liquidity ↑, spread ↓, affordability ↑ (for LONG only)."""
    liq = log1p(q.get("volume") or 0)
    d = q.get("depth") or {}
    bb = (d.get("buy") or [{}])[0].get("price")
    ba = (d.get("sell") or [{}])[0].get("price")
    spr = (ba-bb)/ltp if (bb and ba and ltp) else 0.08
    aff = 1.0 if (trade_type=="LONG" and ltp and (ltp*lot) <= BUDGET) else 0.0
    return 0.6*liq - 0.3*spr + 0.1*aff

def _trade_type_for_side(side, ema5, ema10, px, r1, s1, rsi):
    """Return ('LONG'|'SHORT', rationale string) for CE/PE."""
    overbought = (rsi is not None and rsi >= 70)
    oversold   = (rsi is not None and rsi <= 30)
    bull = (ema5 and ema10 and ema5 > ema10) and (px > r1) and (rsi is None or rsi < 70)
    bear = (ema5 and ema10 and ema5 < ema10) and (px < s1) and (rsi is None or rsi > 30)
    if side == "CE":
        return ("LONG","Bullish confirmation") if (bull and not overbought) else ("SHORT","Bullish weak/exhausted")
    else:  # PE
        return ("LONG","Bearish confirmation") if (bear and not oversold) else ("SHORT","Bearish weak/exhausted")


# ---------------- Core Scan ----------------
def run_smc_scan(kite):
    """
    Returns:
      {
        status, ts, budget, picks: [ ... ], errors: [], diag: {}
      }
    Each pick includes:
      symbol, tradingsymbol, name, type(CE/PE), trade_type(LONG/SHORT),
      strike, expiry, ltp, lot_size, suggested_lots, score, reason,
      tp, sl, entry_action(BUY/SELL), exit_action(SELL/BUY)
    """
    out = {"status":"ok","ts":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "budget":BUDGET,"picks":[],"errors":[],"diag":{}}
    try:
        # 1) Load instruments (cached)
        nfo, nse = _load_instruments(kite)

        # 2) Filter to NIFTY50 stock options (CE/PE)
        base = [r for r in nfo if r.get("name") in NIFTY50 and r.get("instrument_type") in ("CE","PE")]
        out["diag"]["nfo_total"] = len(nfo)
        out["diag"]["nfo_filtered"] = len(base)
        if not base:
            out["status"]="error"; out["errors"].append("No NFO rows for NIFTY50"); return out

        # 3) Nearest expiry
        exp = _nearest_expiry(base)
        out["diag"]["expiry"] = str(exp) if exp else None
        if not exp:
            out["status"]="error"; out["errors"].append("No upcoming expiry"); return out
        base = [r for r in base if r.get("expiry") and _to_date(r["expiry"])==exp]

        # 4) Map NSE tokens for underlyings
        tokens = _map_tokens(nse, NIFTY50)

        # 5) TA per stock (daily)
        ta = {}; last_px = {}
        to_d=datetime.now(); fr_d=to_d - timedelta(days=40)
        for sym in NIFTY50:
            t = tokens.get(sym)
            if not t: continue
            try: hist = kite.historical_data(t, fr_d, to_d, "day")
            except Exception: hist = []
            if not hist or len(hist) < 15: continue
            closes=[c["close"] for c in hist]
            highs=[c["high"] for c in hist]; lows=[c["low"] for c in hist]
            ema5=_ema(closes[-10:],5); ema10=_ema(closes[-10:],10)
            rsi=_rsi(closes[-15:],14)
            pp,r1,s1=_pivots(highs[-2],lows[-2],closes[-2])
            px=closes[-1]
            ta[sym]={"ema5":ema5,"ema10":ema10,"rsi":rsi,"pp":pp,"r1":r1,"s1":s1,"px":px}
            last_px[sym]=px

        if DEBUG_SCAN:
            out["diag"]["ta_count"] = len(ta)

        # 6) Bucket NFO rows by name; build candidates near ATM with trade_type
        by_name={}
        for r in base:
            nm=r.get("name")
            if nm not in ta: continue
            by_name.setdefault(nm,[]).append(r)

        candidates=[]
        for nm, rows in by_name.items():
            strikes=sorted({r.get("strike") for r in rows if r.get("strike") is not None})
            if not strikes: continue
            px = last_px.get(nm)
            atm = min(strikes, key=lambda s: abs(s-px)) if px else strikes[len(strikes)//2]
            ring = _ring(strikes, atm, RING_STRIKES)

            for r in rows:
                if r.get("strike") not in ring: continue
                side = r.get("instrument_type")  # CE/PE
                ttype, why = _trade_type_for_side(
                    side, ta[nm]["ema5"], ta[nm]["ema10"], ta[nm]["px"], ta[nm]["r1"], ta[nm]["s1"], ta[nm]["rsi"]
                )
                candidates.append(("NFO:"+r["tradingsymbol"], r, side, ttype, why, nm))

        out["diag"]["candidates"] = len(candidates)
        if not candidates:
            out["errors"].append("No option candidates after ring filter")
            return out

        # 7) Quote + rank
        quotes={}
        for i in range(0, len(candidates), 60):
            batch=[s for s,_,_,_,_,_ in candidates[i:i+60]]
            try: quotes.update(kite.quote(batch) or {})
            except Exception as e: out["errors"].append(f"quote error: {str(e)}")

        picks=[]
        for sym, meta, side, ttype, why, nm in candidates:
            q = quotes.get(sym) or {}
            ltp = float(q.get("last_price") or 0.0)
            lot = int(meta.get("lot_size") or 1)
            sc  = _score(q, lot, ltp, ttype)

            # LONG sizing (SHORT margin not modeled → lots=0)
            lot_cost = ltp * lot
            lots = int(BUDGET // lot_cost) if (ttype=="LONG" and lot_cost>0) else 0

            # TP/SL at ±1.5% of option premium, tick-rounded
            if ttype=="LONG":
                tp = _tick_round(ltp * (1.0 + TP_PCT_LONG),  TICK_SIZE)
                sl = _tick_round(ltp * (1.0 - SL_PCT_LONG),  TICK_SIZE)
                entry_action = "BUY";  exit_action = "SELL"
            else:
                tp = _tick_round(ltp * (1.0 - TP_PCT_SHORT), TICK_SIZE)  # want premium to fall
                sl = _tick_round(ltp * (1.0 + SL_PCT_SHORT), TICK_SIZE)  # risk if premium rises
                entry_action = "SELL"; exit_action = "BUY"

            picks.append({
                "symbol": sym,                          # e.g., "NFO:HINDUNILVR25AUG2700CE"
                "tradingsymbol": meta.get("tradingsymbol"),
                "name": nm,
                "type": side,                           # CE / PE
                "trade_type": ttype,                    # LONG / SHORT
                "strike": float(meta.get("strike")) if meta.get("strike") is not None else None,
                "expiry": str(_to_date(meta.get("expiry"))) if meta.get("expiry") else None,
                "ltp": ltp,
                "lot_size": lot,
                "score": round(sc, 6),
                "suggested_lots": lots,
                "reason": f"{nm} {side} → {ttype} | {why}",
                "tp": tp,
                "sl": sl,
                "entry_action": entry_action,           # BUY/SELL for entry
                "exit_action":  exit_action,            # SELL/BUY for exits (TP/SL)
            })

        # rank globally; UI buckets by CE/PE + LONG/SHORT
        picks.sort(key=lambda x: x["score"], reverse=True)
        out["picks"] = picks[:50]
        return out

    except Exception as e:
        out["status"]="error"; out["errors"].append(str(e)); return out
