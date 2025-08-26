# smc_logic.py
import os
from datetime import datetime, date, timedelta
from math import log1p

# ---------- Config ----------
BUDGET = float(os.getenv("BUDGET", "1000"))     # total ₹ budget
RING_STRIKES = int(os.getenv("RING_STRIKES", "4"))  # strike steps on each side of ATM
DEBUG_SCAN = os.getenv("DEBUG_SCAN", "0") in ("1", "true", "True")

# Underlyings we support (index names as they appear in instruments list)
UNDERLYINGS = ["NIFTY", "BANKNIFTY"]

# ---------- Small TA on UNDERLYING (not option) ----------
def _ema(series, period):
    if not series: return None
    k = 2 / (period + 1)
    e = float(series[0])
    for v in series:
        e = float(v) * k + e * (1 - k)
    return e

def _rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0: gains.append(diff)
        else: losses.append(-diff)
    ag = sum(gains) / period if gains else 0.0
    al = sum(losses) / period if losses else 0.0
    if al == 0: return 100.0
    rs = ag / al
    return 100 - (100 / (1 + rs))

def _pivots(prev_high, prev_low, prev_close):
    pp = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2 * pp - prev_low
    s1 = 2 * pp - prev_high
    return pp, r1, s1

def _nearest_expiry_for(names, instruments):
    today = date.today()
    exps = sorted({
        r["expiry"].date()
        for r in instruments
        if r.get("expiry") and r.get("name") in names and r.get("segment","").startswith("NFO-")
    })
    for d in exps:
        if d >= today:
            return d
    return exps[-1] if exps else None

def _score_option(q, lot_size, ltp, best_bid, best_ask):
    # simple: prefer liquidity & tight spread & cheaper contracts (to fit budget)
    liq = log1p(q.get("volume") or 0)
    spread = 0.08
    if best_bid and best_ask and ltp:
        spread = max(0.0, (best_ask - best_bid) / max(ltp, 1e-6))
    affordability = 1.0 if (ltp * lot_size) <= (BUDGET/5.0) else 0.0
    return 0.6*liq - 0.3*spread + 0.1*affordability

def _build_ring_strikes(strikes_sorted, atm, steps):
    # get indices for atm; if exact match missing, use nearest
    try:
        idx = strikes_sorted.index(atm)
    except ValueError:
        idx = min(range(len(strikes_sorted)), key=lambda i: abs(strikes_sorted[i]-atm))
    lo = max(0, idx - steps)
    hi = min(len(strikes_sorted)-1, idx + steps)
    return set(strikes_sorted[lo:hi+1])

# ---------- Main ----------
def run_smc_scan(kite):
    """
    Scan only NIFTY & BANKNIFTY CE/PE for nearest expiry.
    Direction from underlying:
      Bullish if EMA5>EMA10 AND price>R1 AND RSI(14)<70  -> prefer CE
      Bearish if EMA5<EMA10 AND price<S1 AND RSI(14)>30  -> prefer PE
      Else: consider both sides but rank by liquidity/spread.
    Returns up to 5 picks total.
    """
    out = {"status": "ok", "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "budget": BUDGET, "picks": [], "errors": [], "diag": {}}
    try:
        # 1) Load NFO instruments once and filter to our underlyings
        nfo = kite.instruments("NFO")
        base = [r for r in nfo if r.get("name") in UNDERLYINGS and r.get("instrument_type") in ("CE","PE")]
        out["diag"]["nfo_count"] = len(nfo)
        out["diag"]["filtered"] = len(base)
        if not base:
            out["status"] = "error"; out["errors"].append("No NFO instruments for NIFTY/BANKNIFTY"); return out

        exp = _nearest_expiry_for(UNDERLYINGS, base)
        if not exp:
            out["status"] = "error"; out["errors"].append("No upcoming expiry found"); return out
        out["diag"]["expiry"] = str(exp)

        base = [r for r in base if r.get("expiry") and r["expiry"].date() == exp]

        # 2) Decide direction from UNDERLYING OHLC (daily, 30 days)
        # instrument_token for indices (Zerodha): NIFTY=256265, BANKNIFTY=260105 (commonly used; fallback via instruments if needed)
        index_tokens = {"NIFTY": 256265, "BANKNIFTY": 260105}
        bias_map = {}   # name -> "CE"/"PE"/"BOTH"
        last_price_map = {}  # last close for ATM calc

        for nm in UNDERLYINGS:
            token = index_tokens.get(nm)
            if not token:
                # fallback via kite.instruments("NSE") search
                try:
                    for r in kite.instruments("NSE"):
                        if r.get("name")==nm or r.get("tradingsymbol")==nm:
                            token = r.get("instrument_token"); break
                except Exception:
                    pass
            try:
                to_d = datetime.now()
                fr_d = to_d - timedelta(days=40)
                hist = kite.historical_data(token, fr_d, to_d, "day")
            except Exception:
                hist = []

            if not hist or len(hist) < 15:
                # if no data, allow both sides
                bias_map[nm] = "BOTH"
                last_price_map[nm] = None
                continue

            closes = [c["close"] for c in hist]
            highs  = [c["high"] for c in hist]
            lows   = [c["low"]  for c in hist]
            ema5   = _ema(closes[-10:], 5)
            ema10  = _ema(closes[-10:], 10)
            rsi    = _rsi(closes[-15:], 14)
            prev_h, prev_l, prev_c = highs[-2], lows[-2], closes[-2]
            pp, r1, s1 = _pivots(prev_h, prev_l, prev_c)
            px = closes[-1]

            bullish = (ema5 and ema10 and ema5 > ema10) and (px > r1) and (rsi is not None and rsi < 70)
            bearish = (ema5 and ema10 and ema5 < ema10) and (px < s1) and (rsi is not None and rsi > 30)

            if bullish: bias_map[nm] = "CE"
            elif bearish: bias_map[nm] = "PE"
            else: bias_map[nm] = "BOTH"

            last_price_map[nm] = px
            if DEBUG_SCAN:
                out["diag"][f"{nm}_ta"] = {"ema5": round(ema5,2) if ema5 else None,
                                           "ema10": round(ema10,2) if ema10 else None,
                                           "rsi": round(rsi,2) if rsi else None,
                                           "pp": round(pp,2), "r1": round(r1,2), "s1": round(s1,2), "px": round(px,2)}

        # 3) Build candidate option symbols near ATM for each underlying, respecting side bias
        candidates = []
        by_under = {nm: [r for r in base if r.get("name")==nm] for nm in UNDERLYINGS}

        for nm, rows in by_under.items():
            if not rows: continue
            strikes = sorted({r.get("strike") for r in rows if r.get("strike") is not None})
            if not strikes: continue

            px = last_price_map.get(nm)
            if not px:
                # fallback: use median strike as ATM proxy
                atm = strikes[len(strikes)//2]
            else:
                atm = min(strikes, key=lambda s: abs(s - px))

            allowed_types = ("CE","PE") if bias_map.get(nm) == "BOTH" else (bias_map[nm],)
            ring = _build_ring_strikes(strikes, atm, RING_STRIKES)

            for r in rows:
                if r.get("strike") in ring and r.get("instrument_type") in allowed_types:
                    ts = r.get("tradingsymbol")
                    if ts:
                        candidates.append(("NFO:" + ts, r))

        out["diag"]["candidates"] = len(candidates)
        if not candidates:
            out["errors"].append("No option candidates (ring/side filter empty).")

        # 4) Quote candidates and score
        quotes = {}
        # chunk small to be safe on free tier
        for i in range(0, len(candidates), 80):
            sub = [sym for sym, _ in candidates[i:i+80]]
            try:
                q = kite.quote(sub)
                quotes.update(q or {})
            except Exception as e:
                out["errors"].append(f"quote error: {str(e)}")

        scored = []
        for sym, meta in candidates:
            q = quotes.get(sym) or {}
            ltp = q.get("last_price") or 0.0
            depth = q.get("depth") or {}
            buy = depth.get("buy") or []
            sell = depth.get("sell") or []
            best_bid = buy[0]["price"] if buy else None
            best_ask = sell[0]["price"] if sell else None
            lot = meta.get("lot_size") or 1
            score = _score_option(q, lot, ltp, best_bid, best_ask)

            # budget split: 5 ideas
            per_pick = max(BUDGET/5.0, 1e-6)
            lot_cost = (ltp or 0) * lot
            suggested_lots = int(per_pick // lot_cost) if lot_cost > 0 else 0
            cap_req = round(suggested_lots * lot_cost, 2)

            scored.append({
                "symbol": sym,
                "name": meta.get("name"),
                "type": meta.get("instrument_type"),
                "strike": float(meta.get("strike")) if meta.get("strike") is not None else None,
                "expiry": str(meta.get("expiry").date()) if meta.get("expiry") else None,
                "ltp": float(ltp),
                "lot_size": int(lot),
                "score": round(score, 6),
                "suggested_lots": suggested_lots,
                "capital_required": cap_req,
                "reason": f"{meta.get('name')} bias={bias_map.get(meta.get('name'),'BOTH')}"
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        out["picks"] = scored[:5]
        if not out["picks"]:
            out["status"] = "degraded"
            out["errors"].append("No quotes returned; try during market hours or widen RING_STRIKES.")
        return out

    except Exception as e:
        out["status"] = "error"
        out["errors"].append(str(e))
        return out
