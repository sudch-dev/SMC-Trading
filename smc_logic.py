import os
from datetime import datetime, date
from math import log1p
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ----- Config -----
MAX_UNDERLYINGS = int(os.getenv("MAX_UNDERLYINGS", "60"))
BUDGET = float(os.getenv("BUDGET", "1000"))  # total budget cap (₹)

# NEW: wider strike ring controls
RING_STRIKES = int(os.getenv("RING_STRIKES", "6"))          # steps on each side of ATM
RING_WIDTH_PCT = float(os.getenv("RING_WIDTH_PCT", "0.02"))  # ±% of underlying price

INDEX_MAP = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
}

def _nearest_expiry(instruments):
    today = date.today()
    exps = sorted({i["expiry"].date() for i in instruments if i.get("expiry")})
    for d in exps:
        if d >= today:
            return d
    return exps[-1] if exps else None

def _is_option(instr):
    return instr.get("segment", "").startswith("NFO-") and instr.get("instrument_type") in ("CE", "PE")

def _moneyness(under_price, strike):
    if not under_price or under_price <= 0:
        return 1.0
    return abs(strike - under_price) / under_price

def _time_penalty(days_to_exp):
    if days_to_exp is None:
        return 0.3
    if days_to_exp < 2:
        return 0.8
    if 2 <= days_to_exp <= 5:
        return 0.2
    if 6 <= days_to_exp <= 21:
        return 0.0
    if 22 <= days_to_exp <= 40:
        return 0.15
    return 0.3

def _trend_bias(ohlc):
    try:
        o = ohlc["open"]; c = ohlc["close"]
        if c > o * 1.002:
            return "bull"
        if c < o * 0.998:
            return "bear"
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
    ltp = option_quote.get("last_price", 0) or 0
    volume = option_quote.get("volume", 0) or 0
    depth = option_quote.get("depth", {}) or {}
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

    dir_bonus = 0.0
    if bias == "bull" and opt_type == "CE":
        dir_bonus = 0.15
    elif bias == "bear" and opt_type == "PE":
        dir_bonus = 0.15

    score = 0.45 * (liq) - 0.30 * spr - 0.15 * money - 0.10 * tpen + dir_bonus
    return float(score)

def run_smc_scan(kite):
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    out = {"status": "ok", "budget": float(BUDGET), "ts": now, "picks": [], "errors": []}

    try:
        instruments = kite.instruments("NFO")
        options = [i for i in instruments if _is_option(i)]
        if not options:
            out["status"] = "error"; out["errors"].append("No NFO option instruments returned.")
            return out

        nearest_exp = _nearest_expiry(options)
        if not nearest_exp:
            out["status"] = "error"; out["errors"].append("No upcoming expiry found.")
            return out

        opts_near = [i for i in options if i.get("expiry") and i["expiry"].date() == nearest_exp]

        names = sorted({i.get("name") for i in opts_near if i.get("name")})
        if MAX_UNDERLYINGS and len(names) > MAX_UNDERLYINGS:
            names = names[:MAX_UNDERLYINGS]

        candidate_symbols = []
        under_info = {}

        for nm in names:
            under_price, under_ohlc = _get_underlying_quote(kite, nm)
            if not under_price:
                continue
            bias = _trend_bias(under_ohlc)
            under_info[nm] = (under_price, bias)

            these = [i for i in opts_near if i.get("name") == nm]
            if not these:
                continue

            # --- Wider ring around ATM ---
            strikes_sorted = sorted({i.get("strike") for i in these if i.get("strike")})
            if not strikes_sorted:
                continue

            # Find ATM strike (nearest strike to underlying)
            atm = min(strikes_sorted, key=lambda s: abs(s - under_price))

            # Estimate strike step (handle irregular gaps robustly)
            if len(strikes_sorted) >= 3:
                diffs = sorted([abs(b - a) for a, b in zip(strikes_sorted, strikes_sorted[1:]) if b != a])
                step = diffs[0] if diffs else (strikes_sorted[1] - strikes_sorted[0])
            elif len(strikes_sorted) == 2:
                step = abs(strikes_sorted[1] - strikes_sorted[0])
            else:
                step = max(round(under_price * 0.005), 1)  # fallback

            # Build ring by steps ±RING_STRIKES
            try:
                atm_idx = strikes_sorted.index(atm)
            except ValueError:
                # if not exact, find nearest index
                atm_idx = min(range(len(strikes_sorted)), key=lambda i: abs(strikes_sorted[i] - under_price))
            lo_idx = max(0, atm_idx - RING_STRIKES)
            hi_idx = min(len(strikes_sorted) - 1, atm_idx + RING_STRIKES)
            step_ring = set(strikes_sorted[lo_idx:hi_idx + 1])

            # Build ring by price band ±RING_WIDTH_PCT
            pct_band = set([s for s in strikes_sorted if abs(s - under_price) <= under_price * RING_WIDTH_PCT])

            # Union of both
            ring = step_ring | pct_band
            subset = [i for i in these if i.get("strike") in ring]

            for i in subset:
                ts = i.get("tradingsymbol")
                if ts:
                    candidate_symbols.append(f"NFO:{ts}")

        if not candidate_symbols:
            out["status"] = "error"; out["errors"].append("No candidate option symbols after filtering.")
            return out

        def chunks(lst, n=300):
            for i in range(0, len(lst), n):
                yield lst[i:i+n]

        quotes = {}
        for ch in chunks(candidate_symbols, 300):
            try:
                q = kite.quote(ch)
                quotes.update(q)
            except Exception as e:
                out["errors"].append(f"quote chunk error: {str(e)}")

        scored = []
        today = date.today()
        for i in opts_near:
            nm = i.get("name"); ts = i.get("tradingsymbol")
            if not nm or not ts:
                continue
            sym = f"NFO:{ts}"
            q = quotes.get(sym)
            if not q:
                continue

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
            if under_price and strike:    reason_bits.append(f"moneyness={_moneyness(under_price, strike):.3f}")
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
            out["status"] = "error"; out["errors"].append("No quotes available for candidates.")
            return out

        scored.sort(key=lambda x: x["score"], reverse=True)
        out["picks"] = scored[:5]
        return out

    except Exception as e:
        out["status"] = "error"
        out["errors"].append(str(e))
        return out
