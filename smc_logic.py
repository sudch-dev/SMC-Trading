import os
from datetime import datetime, date, timedelta
from math import log1p
import pytz
import time
import json

IST = pytz.timezone("Asia/Kolkata")

# ----- Config -----
MAX_UNDERLYINGS = int(os.getenv("MAX_UNDERLYINGS", "60"))
BUDGET = float(os.getenv("BUDGET", "1000"))  # total budget cap (₹)
RING_STRIKES = int(os.getenv("RING_STRIKES", "6"))          # steps on each side of ATM
RING_WIDTH_PCT = float(os.getenv("RING_WIDTH_PCT", "0.02"))  # ±% of underlying price
DEBUG_SCAN = os.getenv("DEBUG_SCAN", "0") in ("1", "true", "True")

# Simple NIFTY50 list (symbols) for equity fallback
NIFTY50 = [
    "RELIANCE"
]

INDEX_MAP = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK"
}

# ---------- utilities ----------
def _nearest_or_next_expiry(instruments):
    today = date.today()
    exps = sorted({i["expiry"].date() for i in instruments if i.get("expiry")})
    if not exps:
        return None
    for d in exps:
        if d >= today:
            return d
    return exps[-1]

def _is_option(instr):
    return instr.get("segment", "").startswith("NFO-") and instr.get("instrument_type") in ("CE", "PE")

def _moneyness(under_price, strike):
    if not under_price or under_price <= 0 or strike is None:
        return 1.0
    return abs(strike - under_price) / under_price

def _time_penalty(days_to_exp):
    if days_to_exp is None: return 0.3
    if days_to_exp < 2:     return 0.8
    if 2 <= days_to_exp <= 5:  return 0.2
    if 6 <= days_to_exp <= 21: return 0.0
    if 22 <= days_to_exp <= 40:return 0.15
    return 0.3

def _trend_bias(ohlc):
    try:
        o, c = ohlc["open"], ohlc["close"]
        if c > o * 1.002: return "bull"
        if c < o * 0.998: return "bear"
    except Exception:
        pass
    return "flat"

def _get_underlying_quote(kite, name):
    try_syms = []
    if name in INDEX_MAP:
        try_syms.append(INDEX_MAP[name])
    try_syms.append(f"NSE:{name}")
    for sym in try_syms:
        try:
            q = kite.quote(sym)
            data = q.get(sym) or {}
            ltp = data.get("last_price")
            ohlc = data.get("ohlc", {})
            if ltp:
                return ltp, ohlc
        except Exception:
            continue
    return None, {}

def _score_option(option_quote, under_price, strike, days_to_exp, opt_type, bias):
    ltp = option_quote.get("last_price") or 0
    volume = option_quote.get("volume") or 0
    depth = option_quote.get("depth") or {}
    bids = depth.get("buy", [])
    asks = depth.get("sell", [])
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None

    liq = log1p(volume)
    if (best_bid and best_ask and ltp and ltp > 0 and best_ask >= best_bid):
        spr = (best_ask - best_bid) / max(ltp, 1e-6)
    else:
        spr = 0.05

    money = _moneyness(under_price, strike)
    tpen = _time_penalty(days_to_exp)
    dir_bonus = 0.15 if ((bias == "bull" and opt_type == "CE") or (bias == "bear" and opt_type == "PE")) else 0.0
    return float(0.45 * liq - 0.30 * spr - 0.15 * money - 0.10 * tpen + dir_bonus)

def _chunked_quote(kite, symbols, out_errors, big=250, small=60):
    quotes = {}
    def try_chunk(size):
        for i in range(0, len(symbols), size):
            sub = symbols[i:i+size]
            try:
                q = kite.quote(sub)
                quotes.update(q or {})
            except Exception as e:
                out_errors.append(f"quote(chunk={size}) error: {str(e)}")
    try_chunk(big)
    missing = [s for s in symbols if s not in quotes]
    if missing:
        try_chunk(small)
    missing = [s for s in symbols if s not in quotes]
    for s in missing:
        try:
            q = kite.quote([s])
            quotes.update(q or {})
            time.sleep(0.03)
        except Exception as e:
            out_errors.append(f"quote(single {s}) error: {str(e)}")
    return quotes

# ---------- simple TA for equity fallback ----------
def _ema(series, period):
    if not series: return None
    k = 2/(period+1)
    ema = float(series[0])
    out = []
    for v in series:
        ema = float(v)*k + ema*(1-k)
        out.append(ema)
    return out[-1]

def _stoch_k(ohlc, k_period=14):
    if len(ohlc) < k_period: return None
    highs = [c['high'] for c in ohlc]
    lows  = [c['low'] for c in ohlc]
    closes= [c['close'] for c in ohlc]
    hi = max(highs[-k_period:]); lo = min(lows[-k_period:])
    denom = (hi - lo) or 1e-12
    return 100.0 * (closes[-1] - lo) / denom

# ---------- main ----------
def run_smc_scan(kite):
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    out = {
        "status": "ok",
        "budget": float(BUDGET),
        "ts": now,
        "picks": [],
        "errors": [],
        "diag": {}
    }

    # ======= PRIMARY: NFO Options scan =======
    try:
        instruments = kite.instruments("NFO")
        options = [i for i in instruments if _is_option(i)]
        out["diag"]["nfo_count"] = len(instruments or [])
        out["diag"]["opt_count"] = len(options)

        if options:
            nearest_exp = _nearest_or_next_expiry(options)
            out["diag"]["expiry"] = str(nearest_exp) if nearest_exp else None
            if nearest_exp:
                opts_near = [i for i in options if i.get("expiry") and i["expiry"].date() == nearest_exp]
                names = sorted({i.get("name") for i in opts_near if i.get("name")})[:MAX_UNDERLYINGS] if MAX_UNDERLYINGS else sorted({i.get("name") for i in opts_near if i.get("name")})
                out["diag"]["underlyings"] = len(names)

                candidate_symbols = []
                under_info = {}

                for nm in names:
                    under_price, under_ohlc = _get_underlying_quote(kite, nm)
                    bias = _trend_bias(under_ohlc) if under_price else "flat"
                    under_info[nm] = (under_price, bias)

                    these = [i for i in opts_near if i.get("name") == nm]
                    strikes_sorted = sorted({i.get("strike") for i in these if i.get("strike") is not None})
                    if not strikes_sorted:
                        continue

                    # ATM (use underlying if available else median strike)
                    atm = min(strikes_sorted, key=lambda s: abs(s - under_price)) if under_price else strikes_sorted[len(strikes_sorted)//2]

                    # indices for ±RING_STRIKES
                    try:
                        atm_idx = strikes_sorted.index(atm)
                    except ValueError:
                        atm_idx = min(range(len(strikes_sorted)), key=lambda i: abs(strikes_sorted[i] - (under_price or atm)))
                    lo_idx = max(0, atm_idx - RING_STRIKES)
                    hi_idx = min(len(strikes_sorted) - 1, atm_idx + RING_STRIKES)
                    step_ring = set(strikes_sorted[lo_idx:hi_idx+1])

                    # ±% band
                    band_ref = under_price or atm
                    pct_band = set([s for s in strikes_sorted if abs(s - band_ref) <= band_ref * RING_WIDTH_PCT])

                    ring = step_ring | pct_band
                    subset = [i for i in these if i.get("strike") in ring]
                    for i in subset:
                        ts = i.get("tradingsymbol")
                        if ts:
                            candidate_symbols.append(f"NFO:{ts}")

                candidate_symbols = sorted(set(candidate_symbols))
                out["diag"]["candidates"] = len(candidate_symbols)

                if candidate_symbols:
                    quotes = _chunked_quote(kite, candidate_symbols, out["errors"])
                    out["diag"]["quoted"] = len(quotes)

                    scored = []
                    today = date.today()
                    for i in opts_near:
                        nm = i.get("name"); ts = i.get("tradingsymbol")
                        if not nm or not ts: continue
                        sym = f"NFO:{ts}"
                        q = quotes.get(sym)
                        if not q: continue

                        under_price, bias = under_info.get(nm, (None, "flat"))
                        strike = i.get("strike")
                        ltp = q.get("last_price") or 0.0
                        lot = i.get("lot_size") or 1
                        expd = i.get("expiry").date() if i.get("expiry") else None
                        days_to_exp = (expd - today).days if expd else None
                        opt_type = i.get("instrument_type")

                        score = _score_option(q, under_price, strike, days_to_exp, opt_type, bias)

                        per_pick = max(BUDGET / 5.0, 1e-6)
                        lot_cost = (ltp or 0) * lot
                        suggested_lots = int(per_pick // lot_cost) if lot_cost > 0 else 0
                        cap_req = suggested_lots * lot_cost

                        reason = []
                        if bias in ("bull","bear"): reason.append(f"trend={bias}")
                        if days_to_exp is not None: reason.append(f"TTE={days_to_exp}d")
                        if under_price and strike: reason.append(f"moneyness={_moneyness(under_price, strike):.3f}")

                        scored.append({
                            "symbol": sym,
                            "name": nm,
                            "type": opt_type,
                            "strike": float(strike) if strike is not None else None,
                            "expiry": str(expd) if expd else None,
                            "ltp": float(ltp),
                            "lot_size": int(lot),
                            "score": round(score, 6),
                            "suggested_lots": int(suggested_lots),
                            "capital_required": round(cap_req, 2),
                            "reason": "; ".join(reason) or "liquidity/price factors"
                        })

                    scored.sort(key=lambda x: x["score"], reverse=True)
                    if scored:
                        out["picks"] = scored[:5]
                        return out
                else:
                    out["errors"].append("No option candidates (ring produced 0).")
            else:
                out["errors"].append("No usable expiry discovered.")

        else:
            out["errors"].append("No NFO options returned (permissions/maintenance?).")

    except Exception as e:
        out["errors"].append(f"options-scan error: {str(e)}")

    # ======= FALLBACK 1: Equity scan (NIFTY50) =======
    try:
        # choose a timeframe based on NIFTY mood (ema spread/atr)
        index_token = 256265  # NIFTY
        to_date = datetime.now()
        from_date = to_date - timedelta(days=180)
        try:
            daily = kite.historical_data(index_token, from_date, to_date, "day")
        except Exception:
            daily = []
        interval = "day"
        if daily and len(daily) >= 60:
            # simple mood: ema20/ema50 spread
            closes = [c['close'] for c in daily]
            def _ema_last(vals, p):
                if not vals: return None
                k = 2/(p+1); e = float(vals[0])
                for v in vals: e = float(v)*k + e*(1-k)
                return e
            e20 = _ema_last(closes, 20); e50 = _ema_last(closes, 50)
            px  = closes[-1]
            spread = abs(e20 - e50)/px*100 if e20 and e50 and px else 0
            interval = "1hour" if spread >= 0.6 else "2hour" if spread >= 0.3 else "day"

        # pull each stock and compute a simple score
        ideas = []
        for sym in NIFTY50:
            try:
                token = kite.ltp([f"NSE:{sym}"]).get(f"NSE:{sym}", {}).get("instrument_token")
                if not token:
                    # fallback token via instruments dump
                    token = None
                    try:
                        inst = kite.instruments()
                        for row in inst:
                            if row.get("exchange") == "NSE" and row.get("tradingsymbol") == sym:
                                token = row.get("instrument_token")
                                break
                    except Exception:
                        pass
                if not token: continue

                hist = kite.historical_data(token, to_date - timedelta(days=90), to_date, interval)
                if not hist or len(hist) < 25: continue

                closes = [c['close'] for c in hist]
                ema5 = _ema(closes, 5); ema10 = _ema(closes, 10)
                k = _stoch_k(hist, 14)
                trend = "bull" if ema5 and ema10 and ema5 > ema10 else "bear" if ema5 and ema10 and ema5 < ema10 else "flat"

                # equity score: trend + oversold/overbought proximity
                score = 0.0
                score += 0.5 if trend == "bull" else -0.5 if trend == "bear" else 0
                if k is not None:
                    if trend == "bull": score += max(0, (50 - min(k, 50))/50)  # prefer lower k in bull
                    if trend == "bear": score += max(0, (max(k, 50) - 50)/50) # prefer higher k in bear
                ltp = hist[-1]['close']
                ideas.append({
                    "symbol": f"NSE:{sym}",
                    "name": sym,
                    "type": "EQUITY",
                    "strike": None,
                    "expiry": None,
                    "ltp": float(ltp),
                    "lot_size": 1,
                    "score": round(score, 6),
                    "suggested_lots": int((BUDGET/5)//ltp) if ltp else 0,
                    "capital_required": round(int((BUDGET/5)//ltp) * ltp, 2) if ltp else 0.0,
                    "reason": f"trend={trend}; stoch={round(k,2) if k is not None else 'NA'}"
                })
            except Exception:
                continue

        ideas.sort(key=lambda x: x["score"], reverse=True)
        if ideas:
            out["errors"].append("Used equity fallback (options unavailable).")
            out["picks"] = ideas[:5]
            return out
    except Exception as e:
        out["errors"].append(f"equity-fallback error: {str(e)}")

    # ======= FALLBACK 2: Always return 5 placeholders =======
    out["status"] = "degraded"
    out["errors"].append("All paths returned empty; sending placeholders for UI continuity.")
    out["picks"] = [{
        "symbol": "NFO:N/A",
        "name": "N/A",
        "type": "N/A",
        "strike": None,
        "expiry": None,
        "ltp": 0.0,
        "lot_size": 0,
        "score": 0.0,
        "suggested_lots": 0,
        "capital_required": 0.0,
        "reason": "No data / permissions / market closed"
    } for _ in range(5)]
    return out
