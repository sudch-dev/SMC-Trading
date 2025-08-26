import os
from datetime import datetime, date, timedelta
from math import log1p
import json, gzip
from pathlib import Path

# ---------------- Config ----------------
BUDGET = float(os.getenv("BUDGET", "10000"))             # budget per pick
RING_STRIKES = int(os.getenv("RING_STRIKES", "4"))       # strikes each side of ATM
MAX_STOCKS = int(os.getenv("MAX_STOCKS", "50"))          # cap universe

# TP/SL heuristics (option premium multiples)
TP_MULT_LONG   = float(os.getenv("TP_MULT_LONG", "1.50"))
SL_MULT_LONG   = float(os.getenv("SL_MULT_LONG", "0.70"))
TP_MULT_SHORT  = float(os.getenv("TP_MULT_SHORT", "0.60"))
SL_MULT_SHORT  = float(os.getenv("SL_MULT_SHORT", "1.50"))

DEBUG_SCAN = os.getenv("DEBUG_SCAN", "0") in ("1", "true", "True")

# NIFTY-50 list (tradingsymbols used by instruments "name")
NIFTY50 = [
    "RELIANCE","HDFCBANK","ICICIBANK","INFY","TCS","ITC","SBIN","BHARTIARTL","AXISBANK","KOTAKBANK",
    "ASIANPAINT","ADANIENT","HCLTECH","MARUTI","BAJFINANCE","SUNPHARMA","TITAN","ULTRACEMCO","NTPC","WIPRO",
    "NESTLEIND","ONGC","M&M","POWERGRID","JSWSTEEL","TATASTEEL","COALINDIA","HINDUNILVR","BAJAJFINSV","TECHM",
    "GRASIM","HDFCLIFE","DIVISLAB","BRITANNIA","DRREDDY","INDUSINDBK","TATAMOTORS","BAJAJ-AUTO","HEROMOTOCO","CIPLA",
    "EICHERMOT","LTIM","HINDALCO","BPCL","ADANIPORTS","SHRIRAMFIN","UPL","APOLLOHOSP","LT","BRITANNIA"
][:MAX_STOCKS]

# --------- Caches (RAM-light with /tmp gz) ---------
CACHE_DIR = Path("/tmp")
NFO_PATH  = CACHE_DIR / "nfo_slim.json.gz"
NSE_PATH  = CACHE_DIR / "nse_slim.json.gz"

_TOKEN_CACHE = {}   # symbol -> NSE instrument_token


# --------- Utilities ---------
def _gz_write(path, obj):
    try:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(obj, f)
    except Exception:
        pass

def _gz_read(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)

def _slim_rows_nfo(raw, allow_names):
    """Keep only tiny fields and only for our 50 names & CE/PE."""
    out = []
    allow = set(allow_names)
    for r in raw:
        nm = r.get("name")
        if nm not in allow:
            continue
        it = r.get("instrument_type")
        if it not in ("CE", "PE"):
            continue
        out.append({
            "tradingsymbol": r.get("tradingsymbol"),
            "name": nm,
            "instrument_type": it,
            "strike": r.get("strike"),
            "expiry": r.get("expiry"),
            "lot_size": r.get("lot_size"),
        })
    return out

def _slim_rows_nse(raw, allow_syms):
    """Only keep token mapping for the 50 underlyings."""
    need = set(allow_syms)
    out = []
    for r in raw:
        ts = r.get("tradingsymbol")
        if ts in need:
            out.append({
                "tradingsymbol": ts,
                "instrument_token": r.get("instrument_token"),
            })
            if len(out) == len(need):
                break
    return out

def _load_instruments(kite):
    """
    Returns: (nfo_slim_rows, nse_slim_rows)
    • First call per dyno: fetch full dumps, slim, gzip to /tmp.
    • Subsequent calls: read small gz files (fast, low-RAM).
    """
    if NFO_PATH.exists() and NSE_PATH.exists():
        try:
            return _gz_read(NFO_PATH), _gz_read(NSE_PATH)
        except Exception:
            # fall back to refetch
            pass

    raw_nfo = kite.instruments("NFO") or []
    raw_nse = kite.instruments("NSE") or []

    nfo_rows = _slim_rows_nfo(raw_nfo, NIFTY50)
    nse_rows = _slim_rows_nse(raw_nse, NIFTY50)

    # free heavy lists ASAP
    del raw_nfo, raw_nse

    _gz_write(NFO_PATH, nfo_rows)
    _gz_write(NSE_PATH, nse_rows)
    return nfo_rows, nse_rows


def _map_tokens(nse_rows, symbols):
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
    gains=losses=0.0
    for i in range(1, p+1):
        d=closes[i]-closes[i-1]
        gains += d if d>0 else 0
        losses+= -d if d<0 else 0
    if losses==0: return 100.0
    rs=(gains/p)/(losses/p)
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

def _tick_round(x, tick=0.05):
    if x is None: return None
    return round(round(float(x)/tick)*tick, 2)

def _score(q, lot, ltp, trade_type):
    liq = log1p(q.get("volume") or 0)
    d = q.get("depth") or {}
    bb = (d.get("buy") or [{}])[0].get("price")
    ba = (d.get("sell") or [{}])[0].get("price")
    spr = (ba-bb)/ltp if (bb and ba and ltp) else 0.08
    aff = 1.0 if (trade_type == "LONG" and ltp and (ltp*lot) <= BUDGET) else 0.0
    return 0.6*liq - 0.3*spr + 0.1*aff

def _trade_type_for_side(side, ema5, ema10, px, r1, s1, rsi):
    overbought = (rsi is not None and rsi >= 70)
    oversold   = (rsi is not None and rsi <= 30)
    bull = (ema5 and ema10 and ema5 > ema10) and (px > r1) and (rsi is None or rsi < 70)
    bear = (ema5 and ema10 and ema5 < ema10) and (px < s1) and (rsi is None or rsi > 30)
    if side == "CE":
        if bull and not overbought: return "LONG", "Bullish confirmation"
        return "SHORT", "Bullish weak/exhausted"
    else:
        if bear and not oversold: return "LONG", "Bearish confirmation"
        return "SHORT", "Bearish weak/exhausted"


# ---------------- Main Scan ----------------
def run_smc_scan(kite):
    out = {"status":"ok","ts":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "budget":BUDGET,"picks":[],"errors":[],"diag":{}}
    try:
        nfo, nse = _load_instruments(kite)
        out["diag"]["nfo_filtered"]=len(nfo)

        if not nfo:
            out["status"]="error"; out["errors"].append("No NFO rows for NIFTY50"); return out

        # nearest expiry
        exp = _nearest_expiry(nfo)
        out["diag"]["expiry"]=str(exp) if exp else None
        if not exp:
            out["status"]="error"; out["errors"].append("No upcoming expiry"); return out
        base = [r for r in nfo if r.get("expiry") and _to_date(r["expiry"])==exp]

        # token map for underlying stocks (NSE)
        tokens = _map_tokens(nse, NIFTY50)

        # TA per stock
        ta = {}
        last_px = {}
        to_d = datetime.now(); fr_d = to_d - timedelta(days=40)
        for sym in NIFTY50:
            t = tokens.get(sym)
            if not t: continue
            try:
                hist = kite.historical_data(t, fr_d, to_d, "day")
            except Exception:
                hist = []
            if not hist or len(hist) < 15:
                continue
            closes=[c["close"] for c in hist]
            highs=[c["high"] for c in hist]; lows=[c["low"] for c in hist]
            ema5=_ema(closes[-10:],5); ema10=_ema(closes[-10:],10)
            rsi=_rsi(closes[-15:],14)
            pp,r1,s1=_pivots(highs[-2],lows[-2],closes[-2])
            px=closes[-1]
            ta[sym]={"ema5":ema5,"ema10":ema10,"rsi":rsi,"pp":pp,"r1":r1,"s1":s1,"px":px}
            last_px[sym]=px

        if DEBUG_SCAN:
            out["diag"]["ta_count"]=len(ta)

        # bucket NFO rows by underlying name
        by_name = {}
        for r in base:
            nm = r.get("name")
            if nm not in ta:
                continue
            by_name.setdefault(nm, []).append(r)

        # Candidates near ATM for each name; decide LONG/SHORT per side
        candidates = []
        for nm, rows in by_name.items():
            strikes = sorted({r.get("strike") for r in rows if r.get("strike") is not None})
            if not strikes: continue
            px = last_px.get(nm)
            atm = min(strikes, key=lambda s: abs(s - px)) if px else strikes[len(strikes)//2]
            ring = _ring(strikes, atm, RING_STRIKES)

            for r in rows:
                if r.get("strike") not in ring: continue
                side = r.get("instrument_type")
                trade_type, rationale = _trade_type_for_side(
                    side,
                    ta[nm]["ema5"], ta[nm]["ema10"],
                    ta[nm]["px"], ta[nm]["r1"], ta[nm]["s1"], ta[nm]["rsi"]
                )
                candidates.append(("NFO:"+r["tradingsymbol"], r, side, trade_type, rationale, nm))

        out["diag"]["candidates"]=len(candidates)
        if not candidates:
            out["errors"].append("No option candidates after ring filter")
            return out

        # Quote and score
        quotes = {}
        for i in range(0, len(candidates), 60):
            batch=[s for s,_,_,_,_,_ in candidates[i:i+60]]
            try:
                quotes.update(kite.quote(batch) or {})
            except Exception as e:
                out["errors"].append(f"quote error: {str(e)}")

        scored = []
        for sym, meta, side, trade_type, rationale, nm in candidates:
            q=quotes.get(sym) or {}
            ltp=q.get("last_price") or 0.0
            lot=meta.get("lot_size") or 1
            sc=_score(q, lot, ltp, trade_type)

            # sizing (longs only; shorts require margin)
            lot_cost = ltp * lot
            lots = int(BUDGET // lot_cost) if (trade_type=="LONG" and lot_cost>0) else 0

            if trade_type == "LONG":
                tp = _tick_round(ltp * TP_MULT_LONG)
                sl = _tick_round(ltp * SL_MULT_LONG)
                entry_action = "BUY"
                exit_action = "SELL"
            else:
                tp = _tick_round(ltp * TP_MULT_SHORT)
                sl = _tick_round(ltp * SL_MULT_SHORT)
                entry_action = "SELL"
                exit_action = "BUY"

            scored.append({
                "symbol": sym,
                "tradingsymbol": meta.get("tradingsymbol"),
                "name": nm,
                "type": side,
                "trade_type": trade_type,
                "strike": float(meta.get("strike")) if meta.get("strike") is not None else None,
                "expiry": str(_to_date(meta.get("expiry"))) if meta.get("expiry") else None,
                "ltp": float(ltp),
                "lot_size": int(lot),
                "score": round(sc,6),
                "suggested_lots": lots,
                "reason": f"{nm} {side} → {trade_type} | {rationale}",
                "tp": tp,
                "sl": sl,
                "entry_action": entry_action,
                "exit_action": exit_action,
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        out["picks"] = scored[:50]
        return out

    except Exception as e:
        out["status"]="error"; out["errors"].append(str(e)); return out
