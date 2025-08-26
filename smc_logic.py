import os
from datetime import datetime, date, timedelta
from math import log1p, log, sqrt, exp
import json, gzip
from pathlib import Path

# ======================= CONFIG =======================
BUDGET = float(os.getenv("BUDGET", "10000"))             # budget per pick
RING_STRIKES = int(os.getenv("RING_STRIKES", "4"))       # strikes each side of ATM
MAX_STOCKS = int(os.getenv("MAX_STOCKS", "50"))          # cap universe

# TP/SL heuristics (option premium multiples)
TP_MULT_LONG   = float(os.getenv("TP_MULT_LONG", "1.50"))
SL_MULT_LONG   = float(os.getenv("SL_MULT_LONG", "0.70"))
TP_MULT_SHORT  = float(os.getenv("TP_MULT_SHORT", "0.60"))
SL_MULT_SHORT  = float(os.getenv("SL_MULT_SHORT", "1.50"))

# Greeks model (annualized)
RISK_FREE = float(os.getenv("RISK_FREE", "0.07"))        # ~7% India
DIV_YIELD = float(os.getenv("DIV_YIELD", "0.00"))        # dividend yield for stocks

# --- Greeks gates (tunable) ---
# IV ranges (annualized as decimals, e.g., 0.18 = 18%)
IV_MIN_LONG   = float(os.getenv("IV_MIN_LONG",  "0.12"))
IV_MAX_LONG   = float(os.getenv("IV_MAX_LONG",  "0.45"))
IV_MIN_SHORT  = float(os.getenv("IV_MIN_SHORT", "0.28"))   # prefer short when IV is elevated

# Delta bands (abs value for puts)
DELTA_MIN_CALL = float(os.getenv("DELTA_MIN_CALL", "0.30"))
DELTA_MAX_CALL = float(os.getenv("DELTA_MAX_CALL", "0.65"))
DELTA_MIN_PUT  = float(os.getenv("DELTA_MIN_PUT",  "0.30"))
DELTA_MAX_PUT  = float(os.getenv("DELTA_MAX_PUT",  "0.65"))

# Gamma cap (avoid ultra-near-expiry convexity blow-ups)
GAMMA_MAX = float(os.getenv("GAMMA_MAX", "0.02"))

# Theta daily bleed cap as fraction of premium (e.g., 0.02 => ≤2% of premium/day)
THETA_MAX_PCT = float(os.getenv("THETA_MAX_PCT", "0.02"))

# If Greeks missing, should we keep the pick? (0/1)
ALLOW_IF_NO_GREEKS = os.getenv("ALLOW_IF_NO_GREEKS", "0") in ("1", "true", "True")

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


# ======================= UTILITIES =======================
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
    if NFO_PATH.exists() and NSE_PATH.exists():
        try:
            return _gz_read(NFO_PATH), _gz_read(NSE_PATH)
        except Exception:
            pass
    raw_nfo = kite.instruments("NFO") or []
    raw_nse = kite.instruments("NSE") or []
    nfo_rows = _slim_rows_nfo(raw_nfo, NIFTY50)
    nse_rows = _slim_rows_nse(raw_nse, NIFTY50)
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


# ======================= SMC (TA) DECISION =======================
def _trade_bias_from_ta(side, ema5, ema10, px, r1, s1, rsi):
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


# ======================= BLACK-SCHOLES / GREEKS =======================
try:
    from math import erf
except ImportError:
    def erf(x):
        sign = 1 if x >= 0 else -1
        x = abs(x)
        a1,a2,a3,a4,a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        t = 1.0/(1.0+0.3275911*x)
        y = 1 - (((((a5*t + a4)*t) + a3)*t + a2)*t + a1)*t*exp(-x*x)
        return sign*y

def _phi(x):  return (1.0 / (sqrt(2.0*3.141592653589793))) * exp(-0.5*x*x)
def _Phi(x):  return 0.5 * (1.0 + erf(x / sqrt(2.0)))

def _yearfrac(start_dt, end_dt):
    days = max(0.0, (end_dt.date() - start_dt.date()).days)
    return max(1.0/365.0, days/365.0)

def bs_price(side, S, K, T, r=RISK_FREE, q=DIV_YIELD, sigma=0.2):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0: return None
    d1 = (log(S/K) + (r - q + 0.5*sigma*sigma)*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)
    disc_r, disc_q = exp(-r*T), exp(-q*T)
    if side == "CE": return disc_q*S*_Phi(d1) - disc_r*K*_Phi(d2)
    else:            return disc_r*K*_Phi(-d2) - disc_q*S*_Phi(-d1)

def bs_greeks(side, S, K, T, r=RISK_FREE, q=DIV_YIELD, sigma=0.2):
    if S <= 0 or K <= 0 or T <= 0 or sigma is None or sigma <= 0:
        return {k: None for k in ("delta","gamma","theta","vega","rho")}
    d1 = (log(S/K) + (r - q + 0.5*sigma*sigma)*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)
    disc_r, disc_q = exp(-r*T), exp(-q*T)
    pdf = _phi(d1)
    if side == "CE":
        delta = disc_q * _Phi(d1)
        theta = ( - (disc_q*S*pdf*sigma)/(2*sqrt(T)) - r*disc_r*K*_Phi(d2) + q*disc_q*S*_Phi(d1) )
        rho   =  K*T*disc_r*_Phi(d2)
    else:
        delta = -disc_q * _Phi(-d1)
        theta = ( - (disc_q*S*pdf*sigma)/(2*sqrt(T)) + r*disc_r*K*_Phi(-d2) - q*disc_q*S*_Phi(-d1) )
        rho   = -K*T*disc_r*_Phi(-d2)
    gamma = disc_q * pdf / (S*sigma*sqrt(T))
    vega  = disc_q * S * pdf * sqrt(T)
    return {"delta": float(delta), "gamma": float(gamma),
            "theta": float(theta/365.0),      # per day
            "vega":  float(vega/100.0),       # per +1.00 vol
            "rho":   float(rho/100.0)}        # per +1.00 rate

def implied_vol(side, S, K, T, price, r=RISK_FREE, q=DIV_YIELD, lo=1e-4, hi=5.0, tol=1e-4, max_iter=60):
    if price is None or price <= 0 or S <= 0 or K <= 0 or T <= 0: return None
    # Expand bracket if needed
    for _ in range(10):
        plo = bs_price(side, S, K, T, r, q, lo)
        phi = bs_price(side, S, K, T, r, q, hi)
        if plo is None or phi is None: return None
        if (plo - price) * (phi - price) <= 0: break
        hi *= 1.5
        if hi > 10: break
    a,b = lo,hi
    fa = bs_price(side,S,K,T,r,q,a) - price
    fb = bs_price(side,S,K,T,r,q,b) - price
    if fa*fb > 0: return None
    for _ in range(max_iter):
        m  = 0.5*(a+b)
        fm = bs_price(side,S,K,T,r,q,m) - price
        if abs(fm) < tol: return float(m)
        if fa*fm <= 0: b,fb = m,fm
        else:         a,fa = m,fm
    return float(0.5*(a+b))


# ======================= GREEK-AWARE SMC =======================
def _apply_greeks_gates(side, base_trade_type, ltp, iv, greeks):
    """
    Enforce Greeks constraints on the base TA decision.
    Returns (final_trade_type, reason_suffix, passed)
    """
    if iv is None or greeks.get("delta") is None:
        if ALLOW_IF_NO_GREEKS:
            return base_trade_type, "Greeks missing (allowed)", True
        return "SHORT" if base_trade_type=="LONG" else base_trade_type, "Greeks missing (blocked long)", False

    delta = greeks.get("delta") or 0.0
    gamma = greeks.get("gamma") or 0.0
    theta = greeks.get("theta") or 0.0  # per day; usually negative for longs

    # theta bleed fraction of premium/day (positive magnitude)
    theta_bleed_pct = abs(theta) / max(ltp, 1e-9)

    ok = True
    fails = []

    if base_trade_type == "LONG":
        # IV range
        if not (IV_MIN_LONG <= iv <= IV_MAX_LONG):
            ok = False; fails.append(f"IV {iv:.2f} ∉ [{IV_MIN_LONG:.2f},{IV_MAX_LONG:.2f}]")
        # Delta band
        if side == "CE":
            if not (DELTA_MIN_CALL <= delta <= DELTA_MAX_CALL):
                ok = False; fails.append(f"Δ {delta:.2f} (call band {DELTA_MIN_CALL}-{DELTA_MAX_CALL})")
        else:
            if not (DELTA_MIN_PUT <= abs(delta) <= DELTA_MAX_PUT):
                ok = False; fails.append(f"|Δ| {abs(delta):.2f} (put band {DELTA_MIN_PUT}-{DELTA_MAX_PUT})")
        # Gamma cap
        if gamma > GAMMA_MAX:
            ok = False; fails.append(f"Γ {gamma:.4f} > {GAMMA_MAX}")
        # Theta bleed
        if theta_bleed_pct > THETA_MAX_PCT:
            ok = False; fails.append(f"θ {theta_bleed_pct:.2%} > {THETA_MAX_PCT:.2%} per day")

        if ok:
            return "LONG", "Greeks ok", True
        else:
            return "SHORT", "Greeks fail: " + "; ".join(fails), False

    else:  # base SHORT
        # For shorts, prefer elevated IV; delta extremes are okay but avoid ultra-gamma
        if iv < IV_MIN_SHORT:
            ok = False; fails.append(f"IV {iv:.2f} < {IV_MIN_SHORT:.2f}")
        if gamma > GAMMA_MAX:
            ok = False; fails.append(f"Γ {gamma:.4f} > {GAMMA_MAX}")

        if ok:
            return "SHORT", "Greeks favor short", True
        else:
            # If short gates fail, we won’t flip to long; keep SHORT but mark weak
            return "SHORT", "Weak short (Greeks fail: " + "; ".join(fails) + ")", False


# ======================= MAIN SCAN =======================
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

        # TA per stock (daily)
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

        # Build candidate list near ATM
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
                # Base TA bias
                base_type, rationale = _trade_bias_from_ta(
                    side, ta[nm]["ema5"], ta[nm]["ema10"], ta[nm]["px"], ta[nm]["r1"], ta[nm]["s1"], ta[nm]["rsi"]
                )
                candidates.append(("NFO:"+r["tradingsymbol"], r, side, base_type, rationale, nm))

        out["diag"]["candidates"]=len(candidates)
        if not candidates:
            out["errors"].append("No option candidates after ring filter")
            return out

        # Quote in batches
        quotes = {}
        for i in range(0, len(candidates), 60):
            batch=[s for s,_,_,_,_,_ in candidates[i:i+60]]
            try:
                quotes.update(kite.quote(batch) or {})
            except Exception as e:
                out["errors"].append(f"quote error: {str(e)}")

        # Score and compute Greeks-aware decision
        scored = []
        for sym, meta, side, base_type, rationale, nm in candidates:
            q = quotes.get(sym) or {}
            ltp = float(q.get("last_price") or 0.0)
            lot = int(meta.get("lot_size") or 1)
            sc  = _score(q, lot, ltp, base_type)

            # ---- IV + Greeks inputs ----
            S   = float(last_px.get(nm) or 0.0)  # underlying spot
            K   = float(meta.get("strike") or 0.0)
            now  = datetime.now()
            expd = _to_date(meta.get("expiry"))
            exp_dt = datetime.combine(expd, datetime.min.time()) if hasattr(expd, "year") else now
            T   = _yearfrac(now, exp_dt)

            iv  = implied_vol(side, S, K, T, ltp) if (S>0 and K>0 and T>0 and ltp>0) else None
            gks = bs_greeks(side, S, K, T, sigma=iv) if iv else {k: None for k in ("delta","gamma","theta","vega","rho")}

            # ---- Apply Greeks gates on top of TA bias ----
            final_type, greek_note, _passed = _apply_greeks_gates(side, base_type, ltp, iv, gks)

            # ---- sizing (longs only) ----
            lot_cost = ltp * lot
            lots = int(BUDGET // lot_cost) if (final_type=="LONG" and lot_cost>0) else 0

            # ---- TP/SL & actions ----
            if final_type == "LONG":
                tp = _tick_round(ltp * TP_MULT_LONG)
                sl = _tick_round(ltp * SL_MULT_LONG)
                entry_action, exit_action = "BUY", "SELL"
            else:
                tp = _tick_round(ltp * TP_MULT_SHORT)
                sl = _tick_round(ltp * SL_MULT_SHORT)
                entry_action, exit_action = "SELL", "BUY"

            scored.append({
                "symbol": sym,
                "tradingsymbol": meta.get("tradingsymbol"),
                "name": nm,
                "type": side,                          # CE/PE
                "trade_type": final_type,              # LONG/SHORT after Greeks
                "strike": float(meta.get("strike")) if meta.get("strike") is not None else None,
                "expiry": str(_to_date(meta.get("expiry"))) if meta.get("expiry") else None,
                "ltp": float(ltp),
                "lot_size": int(lot),
                "score": round(sc,6),
                "suggested_lots": lots,
                "reason": f"{nm} {side} → {final_type} | TA: {rationale} | Gk: {greek_note}",
                "tp": tp,
                "sl": sl,
                "entry_action": entry_action,
                "exit_action": exit_action,

                # Greeks fields
                "iv": float(iv) if iv else None,
                "delta": gks.get("delta"),
                "gamma": gks.get("gamma"),
                "theta_per_day": gks.get("theta"),
                "vega_per_volpt": gks.get("vega"),
                "rho_per_ratept": gks.get("rho"),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        out["picks"] = scored[:50]
        return out

    except Exception as e:
        out["status"]="error"; out["errors"].append(str(e)); return out
