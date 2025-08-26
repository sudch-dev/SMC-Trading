# smc_logic.py
import os
from datetime import datetime, date, timedelta
from math import log1p

BUDGET = float(os.getenv("BUDGET", "1000"))
RING_STRIKES = int(os.getenv("RING_STRIKES", "4"))
UNDERLYINGS = ("NIFTY", "BANKNIFTY")

# ------------ module-level cache ------------
_INSTR_CACHE = {"loaded": False, "nfo": []}

def _load_nfo_once(kite):
    if not _INSTR_CACHE["loaded"]:
        _INSTR_CACHE["nfo"] = kite.instruments("NFO") or []
        _INSTR_CACHE["loaded"] = True
    return _INSTR_CACHE["nfo"]

# ------------ tiny TA on underlying ------------
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
    rs=gains/p/(losses/p)
    return 100 - (100/(1+rs))

def _pivots(h,l,c):
    pp=(h+l+c)/3.0
    r1=2*pp-l; s1=2*pp-h
    return pp,r1,s1

def _nearest_expiry(rows):
    today=date.today()
    exps=sorted({r["expiry"].date() for r in rows if r.get("expiry")})
    for d in exps:
        if d>=today: return d
    return exps[-1] if exps else None

def _ring(strikes, atm, steps):
    try:
        i=strikes.index(atm)
    except ValueError:
        i=min(range(len(strikes)), key=lambda k: abs(strikes[k]-atm))
    lo=max(0, i-steps); hi=min(len(strikes)-1, i+steps)
    return set(strikes[lo:hi+1])

def _score(q, lot, ltp):
    liq = log1p(q.get("volume") or 0)
    d = q.get("depth") or {}
    bb = (d.get("buy") or [{}])[0].get("price")
    ba = (d.get("sell") or [{}])[0].get("price")
    spr = (ba-bb)/ltp if (bb and ba and ltp) else 0.08
    aff = 1.0 if (ltp*lot) <= (BUDGET/5.0) else 0.0
    return 0.6*liq - 0.3*spr + 0.1*aff

def run_smc_scan(kite):
    out = {"status":"ok","ts":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "budget":BUDGET,"picks":[],"errors":[],"diag":{}}
    try:
        # 1) instruments cache
        nfo = _load_nfo_once(kite)
        base = [r for r in nfo if r.get("name") in UNDERLYINGS and r.get("instrument_type") in ("CE","PE")]
        out["diag"]["nfo_total"]=len(nfo); out["diag"]["nfo_filtered"]=len(base)
        if not base:
            out["status"]="error"; out["errors"].append("No NFO rows for NIFTY/BANKNIFTY"); return out

        # 2) nearest expiry
        exp = _nearest_expiry(base)
        out["diag"]["expiry"]=str(exp) if exp else None
        if not exp:
            out["status"]="error"; out["errors"].append("No upcoming expiry"); return out
        base = [r for r in base if r.get("expiry") and r["expiry"].date()==exp]

        # 3) underlying direction (daily)
        idx_tokens = {"NIFTY": 256265, "BANKNIFTY": 260105}
        bias = {}     # CE/PE/BOTH
        last_px = {}
        for nm in UNDERLYINGS:
            token = idx_tokens.get(nm)
            try:
                to_d=datetime.now(); fr_d=to_d - timedelta(days=40)
                hist=kite.historical_data(token, fr_d, to_d, "day")
            except Exception:
                hist=[]
            if not hist or len(hist)<15:
                bias[nm]="BOTH"; last_px[nm]=None; continue
            closes=[c["close"] for c in hist]
            highs=[c["high"] for c in hist]; lows=[c["low"] for c in hist]
            ema5=_ema(closes[-10:],5); ema10=_ema(closes[-10:],10)
            rsi=_rsi(closes[-15:],14)
            pp,r1,s1=_pivots(highs[-2],lows[-2],closes[-2])
            px=closes[-1]
            if (ema5 and ema10 and ema5>ema10) and (px>r1) and (rsi is not None and rsi<70):
                bias[nm]="CE"
            elif (ema5 and ema10 and ema5<ema10) and (px<s1) and (rsi is not None and rsi>30):
                bias[nm]="PE"
            else:
                bias[nm]="BOTH"
            last_px[nm]=px

        # 4) build candidate ring around ATM
        candidates=[]
        for nm in UNDERLYINGS:
            rows=[r for r in base if r.get("name")==nm]
            if not rows: continue
            strikes=sorted({r.get("strike") for r in rows if r.get("strike") is not None})
            if not strikes: continue
            atm = min(strikes, key=lambda s: abs(s - last_px[nm])) if last_px[nm] else strikes[len(strikes)//2]
            ring = _ring(strikes, atm, RING_STRIKES)
            allowed = ("CE","PE") if bias[nm]=="BOTH" else (bias[nm],)
            for r in rows:
                if r.get("strike") in ring and r.get("instrument_type") in allowed:
                    sym = "NFO:" + r["tradingsymbol"]
                    candidates.append((sym, r))
        out["diag"]["candidates"]=len(candidates)
        if not candidates:
            out["errors"].append("No candidates after ring/side filter")
            return out

        # 5) quote + rank
        quotes={}
        for i in range(0, len(candidates), 80):
            batch=[s for s,_ in candidates[i:i+80]]
            try:
                q=kite.quote(batch); quotes.update(q or {})
            except Exception as e:
                out["errors"].append(f"quote error: {str(e)}")

        scored=[]
        for sym, meta in candidates:
            q=quotes.get(sym) or {}
            ltp=q.get("last_price") or 0.0
            lot=meta.get("lot_size") or 1
            sc=_score(q, lot, ltp)
            per=BUDGET/5.0
            lots=int(per//(ltp*lot)) if ltp>0 else 0
            scored.append({
                "symbol": sym,
                "name": meta.get("name"),
                "type": meta.get("instrument_type"),
                "strike": float(meta.get("strike")) if meta.get("strike") is not None else None,
                "expiry": str(meta.get("expiry").date()) if meta.get("expiry") else None,
                "ltp": float(ltp),
                "lot_size": int(lot),
                "score": round(sc,6),
                "suggested_lots": lots,
                "capital_required": round(lots*ltp*lot,2),
                "reason": f"bias={bias.get(meta.get('name'),'BOTH')}"
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        out["picks"]=scored[:5]
        return out

    except Exception as e:
        out["status"]="error"; out["errors"].append(str(e)); return out
