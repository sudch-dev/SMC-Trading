import os
from datetime import datetime, date
from math import log1p
import pytz
import time

IST = pytz.timezone("Asia/Kolkata")

# ----- Config -----
MAX_UNDERLYINGS = int(os.getenv("MAX_UNDERLYINGS", "60"))
BUDGET = float(os.getenv("BUDGET", "1000"))  # total budget cap (₹)
RING_STRIKES = int(os.getenv("RING_STRIKES", "6"))          # steps on each side of ATM
RING_WIDTH_PCT = float(os.getenv("RING_WIDTH_PCT", "0.02"))  # ±% of underlying price
DEBUG_SCAN = os.getenv("DEBUG_SCAN", "0") in ("1", "true", "True")

INDEX_MAP = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
}

def _nearest_or_next_expiry(instruments):
    """Return nearest usable expiry; if none on/after today, take latest available."""
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
    """Return (ltp, ohlc) for underlying 'name'."""
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

def _chunked_quote(kite, symbols, out_errors, big=250, small=60, single_timeout=3):
    """
    Try big chunks, then smaller, then single-symbol fallback.
    Returns merged quotes dict.
    """
    quotes = {}

    def try_chunk(size):
        for i in range(0, len(symbols), size):
            sub = symbols[i:i+size]
            try:
                q = kite.quote(sub)
                quotes.update(q or {})
            except Exception as e:
                out_errors.append(f"quote(chunk={size}) error: {str(e)}")

    # big chunks first
    try_chunk(big)

    # retry missing via small chunks
    missing = [s for s in symbols if s not in quotes]
    if missing:
        try_chunk(small)

    # final retry one-by-one with short sleep
    missing = [s for s in symbols if s not in quotes]
    for s in missing:
        try:
            q = kite.quote([s])
            quotes.update(q or {})
            time.sleep(0.05)
        except Exception as e:
            out_errors.append(f"quote(single {s}) error: {str(e)}")

    return quotes

def run_smc_scan(kite):
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    out = {
        "status": "ok",
        "budget": float(BUDGET),
        "ts": now,
        "picks": [],
        "errors": [],
        # diagnostics:
        "diag": {
            "nfo_count": 0, "opt_count": 0, "names": 0,
            "nearest_expiry": None, "opts_near": 0,
            "candidate_symbols": 0, "quoted": 0
        }
    }

    try:
        instruments = kite.instruments("NFO")
        out["diag"]["nfo_count"] = len(instruments or [])
        options = [i for i in instruments if _is_option(i)]
        out["diag"]["opt_count"] = len(options)

        if not options:
            out["status"] = "error"; out["errors"].append("No NFO option instruments returned.")
            return out

        # Pick nearest (or next best) expiry with actual instruments
        nearest_exp = _nearest_or_next_expiry(options)
        out["diag"]["nearest_expiry"] = str(nearest_exp) if nearest_exp else None

        if not nearest_exp:
            out["status"] = "error"; out["errors"].append("No upcoming expiry found.")
            return out

        opts_near = [i for i in options if i.get("expiry") and i["expiry"].date() == nearest_exp]
        if not opts_near:
            # fallback: take most populated expiry
            exp_groups = {}
            for i in options:
                if i.get("expiry"):
                    d = i["expiry"].date()
                    exp_groups[d] = exp_groups.get(d, 0) + 1
            if exp_groups:
                best = max(exp_groups.items(), key=lambda x: x[1])[0]
                opts_near = [i for i in options if i.get("expiry") and i["expiry"].date() == best]
                out["diag"]["nearest_expiry"] = f"{nearest_exp} (fallback→{best})"

        out["diag"]["opts_near"] = len(opts_near)

        # Underlying universe (throttled)
        names = sorted({i.get("name") for i in opts_near if i.get("name")})
        out["diag"]["names"] = len(names)
        if MAX_UNDERLYINGS and len(names) > MAX_UNDERLYINGS:
            names = names[:MAX_UNDERLYINGS]

        candidate_symbols = []
        under_info = {}

        for nm in names:
            under_price, under_ohlc = _get_underlying_quote(kite, nm)
            bias = _trend_bias(under_ohlc) if under_price else "flat"
            under_info[nm] = (under_price, bias)

            these = [i for i in opts_near if i.get("name") == nm]
            if not these:
                continue

            strikes_sorted = sorted({i.get("strike") for i in these if i.get("strike") is not None})
            if not strikes_sorted:
                continue

            # If no underlying price, use median strike as ATM proxy
            atm = None
            if under_price:
                atm = min(strikes_sorted, key=lambda s: abs(s - under_price))
            else:
                mid = len(strikes_sorted) // 2
                atm = strikes_sorted[mid]

            # robust step
            if len(strikes_sorted) >= 3:
                diffs = sorted([abs(b - a) for a, b in zip(strikes_sorted, strikes_sorted[1:]) if b != a])
                step = diffs[0] if diffs else (strikes_sorted[1] - strikes_sorted[0])
            elif len(strikes_sorted) == 2:
                step = abs(strikes_sorted[1] - strikes_sorted[0])
            else:
                step = max(round((under_price or atm) * 0.005), 1)

            # Build ring by steps ±RING_STRIKES
            try:
                atm_idx = strikes_sorted.index(atm)
            except ValueError:
                atm_idx = min(range(len(strikes_sorted)), key=lambda i: abs(strikes_sorted[i] - (under_price or atm)))
            lo_idx = max(0, atm_idx - RING_STRIKES)
            hi_idx = min(len(strikes_sorted) - 1, atm_idx + RING_STRIKES)
            step_ring = set(strikes_sorted[lo_idx:hi_idx + 1])

            # Build ring by price band ±RING_WIDTH_PCT
            band_ref = under_price or atm
            pct_band = set([s for s in strikes_sorted if abs(s - band_ref) <= band_ref * RING_WIDTH_PCT])

            ring = step_ring | pct_band
            subset = [i for i in these if i.get("strike") in ring]

            for i in subset:
                ts = i.get("tradingsymbol")
                if ts:
                    candidate_symbols.append(f"NFO:{ts}")

        candidate_symbols = sorted(set(candidate_symbols))
        out["diag"]["candidate_symbols"] = len(candidate_symbols)

        if not candidate_symbols:
            out["status"] = "error"
            out["errors"].append("No candidate option symbols after filtering (ring/quotes).")
            if DEBUG_SCAN:
                out["errors"].append("Hint: widen RING_STRIKES or RING_WIDTH_PCT, or increase MAX_UNDERLYINGS.")
            return out

        # Quote with retries/fallbacks
        errors = out["errors"]
        quotes = _chunked_quote(kite, candidate_symbols, errors)
        out["diag"]["quoted"] = len(quotes)

        scored = []
        today = date.today()

        # Index by tradingsymbol for fast lookups
        quote_keys = set(quotes.keys())

        for i in opts_near:
            nm = i.get("name")
            ts = i.get("tradingsymbol")
            if not nm or not ts:
                continue
            sym = f"NFO:{ts}"
            if sym not in quote_keys:
                continue

            q = quotes.get(sym) or {}
            under_price, bias = under_info.get(nm, (None, "flat"))
            strike = i.get("strike")
            ltp = q.get("last_price") or 0
            lot = i.get("lot_size") or 1
            expd = i.get("expiry").date() if i.get("expiry") else None
            days_to_exp = (expd - today).days if expd else None
            opt_type = i.get("instrument_type")

            score = _score_option(q, under_price, strike, days_to_exp, opt_type, bias)

            per_pick = max(BUDGET / 5.0, 1e-6)
            lot_cost = (ltp or 0) * lot
            suggested_lots = int(per_pick // lot_cost) if lot_cost > 0 else 0
            cap_req = suggested_lots * lot_cost

            reason_bits = []
            if bias in ("bull", "bear"): reason_bits.append(f"fits trend ({bias})")
            if days_to_exp is not None:   reason_bits.append(f"{days_to_exp}d to expiry")
            if (under_price or strike):   reason_bits.append(f"moneyness={_moneyness(under_price or strike, strike):.3f}")
            reason = "; ".join(reason_bits) or "liquidity/price factors"

            scored.append({
                "symbol": sym,
                "name": nm,
                "type": opt_type,
                "strike": float(strike) if strike is not None else None,
                "expiry": str(expd) if expd else None,
                "ltp": float(ltp) if ltp else 0.0,
                "lot_size": int(lot),
                "score": round(score, 6),
                "suggested_lots": int(suggested_lots),
                "capital_required": round(cap_req, 2),
                "reason": reason
            })

        if not scored:
            out["status"] = "error"
            out["errors"].append("No quotes matched the filtered option list (post-quote).")
            if DEBUG_SCAN:
                out["errors"].append(f"quoted_keys={len(quote_keys)}, opts_near={len(opts_near)}")
            return out

        scored.sort(key=lambda x: x["score"], reverse=True)
        out["picks"] = scored[:5]
        return out

    except Exception as e:
        out["status"] = "error"
        out["errors"].append(str(e))
        return out
