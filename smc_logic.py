# smc_logic.py
# ---------------------------------------------------------------------
# Clean, symbol-agnostic ITM/ATM/OTM classification using underlying LTP.
# Applies TA + Greeks gates and returns tradable picks with TP/SL.
# Plug this into your scanner backend (e.g., import and call select_trades()).
# ---------------------------------------------------------------------

from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Any

# ------------------------------ Config --------------------------------

# Moneyness band: within ±0.25% of spot is treated as ATM
ATM_BAND_PCT = 0.0025

# Greeks gates (tune as you wish)
IV_MIN = 0.15
IV_MAX = 0.35
GAMMA_MIN = 0.0100
# Call delta "avoid ATM" band: avoid |Δ| >= 0.25 (you can change per your rule)
DELTA_ATM_AVOID = 0.25
# Put delta preferred band (optional): 0.40–0.55; we use it as a soft gate
PUT_DELTA_BAND = (0.40, 0.55)

# Theta minimum daily decay (only enforced for PUT short logic below)
THETA_MIN_PCT_PER_DAY = 0.015  # 1.5%

# Trade filters
EXCLUDE_ATM = True     # often useful near expiry
REQUIRE_GREKS = True   # enforce IV/Gamma/Delta/Theta gates where applicable

# ------------------------------ Types ---------------------------------

@dataclass
class OptionRow:
    # Basic identity
    root: str               # e.g., 'SBIN', 'RELIANCE'
    type: str               # 'CE' or 'PE'
    strike: float
    expiry: str             # e.g., '2025-08-28'
    dte: int                # days to expiry (integer)

    # Prices
    ltp: float              # option LTP
    lot_size: int           # lot size

    # Greeks (optional; pass None if not available)
    iv: Optional[float] = None        # e.g., 0.22 for 22%
    gamma: Optional[float] = None
    delta: Optional[float] = None
    theta_pct_day: Optional[float] = None  # e.g., 0.022 for 2.2%/day

    # TA/Model flags
    ta_tag: Optional[str] = None      # e.g., 'Bullish weak/exhausted', 'Bearish confirmation'

    # Target & Stop (optional; will be kept if provided)
    tp: Optional[float] = None
    sl: Optional[float] = None

# --------------------------- Core Helpers -----------------------------

def classify_moneyness(spot: float, strike: float, opt_type: str) -> str:
    """
    spot  = underlying LTP (cash or consistent futures LTP)
    strike = option strike
    opt_type = 'CE' or 'PE'
    """
    if spot <= 0 or strike <= 0:
        return "NA"

    pct = abs(spot - strike) / spot
    if pct <= ATM_BAND_PCT:
        return "ATM"

    if opt_type.upper() == "CE":
        return "ITM" if spot > strike else "OTM"
    else:  # PE
        return "ITM" if spot < strike else "OTM"

def pass_greeks_gates(row: OptionRow) -> (bool, List[str]):
    """
    Enforce Greeks rules. Returns (ok, reasons)
    reasons = list of rule hits/fails for logging in 'why' text.
    """
    reasons = []
    ok = True

    # IV band (if available)
    if row.iv is not None:
        if not (IV_MIN <= row.iv <= IV_MAX):
            ok = False
            reasons.append(f"IV {safe_fmt(row.iv)}∉[{safe_fmt(IV_MIN)},{safe_fmt(IV_MAX)}]")
        else:
            reasons.append(f"IV {safe_fmt(row.iv)}∈[{safe_fmt(IV_MIN)},{safe_fmt(IV_MAX)}]")
    else:
        reasons.append("IV missing")

    # Gamma minimum (if available)
    if row.gamma is not None:
        if row.gamma < GAMMA_MIN:
            ok = False
            reasons.append(f"Γ {safe_fmt(row.gamma)}<{safe_fmt(GAMMA_MIN)}")
        else:
            reasons.append(f"Γ {safe_fmt(row.gamma)}>{safe_fmt(GAMMA_MIN)}")
    else:
        reasons.append("Γ missing")

    # Delta rules (if available)
    if row.delta is not None:
        abs_delta = abs(row.delta)
        # Avoid ATM shorts: too high delta (near ATM)
        if abs_delta >= DELTA_ATM_AVOID:
            # We mark as "avoid ATM short"; some users prefer hard-fail here
            reasons.append(f"|Δ| {safe_fmt(abs_delta)}≥{safe_fmt(DELTA_ATM_AVOID)} (avoid ATM short)")
            # If you want to *hard fail* ATM avoidance, uncomment next 2 lines:
            # ok = False
            # reasons.append("Delta gate FAIL (ATM avoid)")
        else:
            reasons.append(f"|Δ| {safe_fmt(abs_delta)}<{safe_fmt(DELTA_ATM_AVOID)} (ok)")
        # Put-specific soft band (optional)
        if row.type.upper() == "PE":
            lo, hi = PUT_DELTA_BAND
            if not (lo <= abs_delta <= hi):
                # Soft fail; we keep it as a caution rather than disqualify
                reasons.append(f"Put Δ band {safe_fmt(abs_delta)}∉[{safe_fmt(lo)},{safe_fmt(hi)}] (soft)")
            else:
                reasons.append(f"Put Δ band ok {safe_fmt(abs_delta)}∈[{safe_fmt(lo)},{safe_fmt(hi)}]")
    else:
        reasons.append("Δ missing")

    # Theta (only meaningful for certain strategies; you can enforce per type)
    if row.type.upper() == "PE" and row.theta_pct_day is not None:
        if row.theta_pct_day < THETA_MIN_PCT_PER_DAY:
            ok = False
            reasons.append(f"θ {pct_fmt(row.theta_pct_day)}<{pct_fmt(THETA_MIN_PCT_PER_DAY)}/day")
        else:
            reasons.append(f"θ {pct_fmt(row.theta_pct_day)}>{pct_fmt(THETA_MIN_PCT_PER_DAY)}/day")
    elif row.type.upper() == "PE":
        reasons.append("θ missing")

    return ok, reasons

def safe_fmt(x: Optional[float]) -> str:
    try:
        return f"{float(x):.4f}"
    except Exception:
        return str(x)

def pct_fmt(x: Optional[float]) -> str:
    try:
        return f"{100*float(x):.2f}%"
    except Exception:
        return str(x)

# --------------------------- Selection Logic --------------------------

def select_trades(
    option_rows: List[OptionRow],
    underlying_quotes: Dict[str, float],
    *,
    exclude_atm: bool = EXCLUDE_ATM,
    require_greeks: bool = REQUIRE_GREKS,
) -> List[Dict[str, Any]]:
    """
    Build tradable picks from raw option rows + underlying quotes.
    Returns a list of dicts ready for frontend (keeps your TP/SL if provided).
    """
    picks: List[Dict[str, Any]] = []

    for row in option_rows:
        # 1) Spot from underlying map
        spot = float(underlying_quotes.get(row.root, 0.0))
        mny = classify_moneyness(spot, row.strike, row.type)

        # 2) ATM filter (optional)
        if exclude_atm and mny == "ATM":
            continue

        # 3) Greeks gating
        why_parts = []
        greeks_ok = True
        if require_greeks:
            greeks_ok, reasons = pass_greeks_gates(row)
            why_parts.extend(reasons)
            if not greeks_ok:
                # You can choose to skip immediately if Greeks fail
                # continue
                pass
        else:
            why_parts.append("Greeks not enforced")

        # 4) TA tag formatting
        ta_txt = row.ta_tag or "TA: n/a"

        # 5) Decision side based on your style
        #    Here we infer typical intent from TA + option type:
        #    - If TA says 'Bullish weak/exhausted' -> prefer SHORT CE
        #    - If TA says 'Bearish confirmation'   -> prefer SHORT PE
        #    You can plug your SMC bias confirmation here.
        trade_type = "HOLD"
        if row.type.upper() == "CE" and (row.ta_tag or "").lower().startswith("bullish"):
            trade_type = "SHORT"
        elif row.type.upper() == "PE" and (row.ta_tag or "").lower().startswith("bearish"):
            trade_type = "SHORT"
        # If you have separate SMC gates, replace the above with your bias flags.

        # 6) Assemble the pick
        why_text = f"{ta_txt} | " + " | ".join(why_parts) if why_parts else ta_txt

        pick = {
            "name": row.root,
            "type": row.type.upper(),
            "strike": row.strike,
            "expiry": row.expiry,
            "dte": row.dte,

            "spot": spot,
            "ltp": row.ltp,
            "lot_size": row.lot_size,

            "moneyness": mny,
            "trade_type": trade_type,

            "iv": row.iv,
            "gamma": row.gamma,
            "delta": row.delta,
            "theta_pct_day": row.theta_pct_day,

            "tp": row.tp,
            "sl": row.sl,

            "why": why_text,
            "greeks_ok": greeks_ok,
        }

        picks.append(pick)

    # Optional: prioritize by your preference (e.g., cheapest premium first, or best TP/SL ratio)
    # Here we sort by 'trade_type' SHORT first, then by ltp ascending
    picks.sort(key=lambda p: (p["trade_type"] != "SHORT", p["ltp"] if p["ltp"] is not None else 1e9))

    return picks

# ------------------------------ Demo ----------------------------------

if __name__ == "__main__":
    # Example usage with minimal fake data (replace with your chain + quotes)
    underlying_quotes = {
        "RELIANCE": 1385.0,
        "SBIN": 818.0,
        "TATASTEEL": 158.2,
        "NTPC": 338.0,
        "HDFCBANK": 972.0,
    }

    rows = [
        # CE examples
        OptionRow(root="TATASTEEL", type="CE", strike=160, expiry="2025-08-28", dte=2,
                  ltp=0.10, lot_size=5500, iv=0.26, gamma=0.0481, delta=0.12,
                  ta_tag="Bullish weak/exhausted", tp=0.05, sl=0.15),
        OptionRow(root="NTPC", type="CE", strike=340, expiry="2025-08-28", dte=2,
                  ltp=0.05, lot_size=1500, iv=0.13, gamma=0.0277, delta=0.08,
                  ta_tag="Bullish weak/exhausted", tp=0.05, sl=0.10),
        OptionRow(root="RELIANCE", type="CE", strike=1400, expiry="2025-08-28", dte=2,
                  ltp=3.45, lot_size=500, iv=0.19, gamma=0.0178, delta=0.29,
                  ta_tag="Bullish weak/exhausted", tp=1.90, sl=5.50),

        # PE examples
        OptionRow(root="SBIN", type="PE", strike=810, expiry="2025-08-28", dte=2,
                  ltp=3.40, lot_size=750, iv=0.20, gamma=0.0430, delta=-0.35, theta_pct_day=0.0541,
                  ta_tag="Bearish confirmation", tp=1.85, sl=5.45),
        OptionRow(root="HDFCBANK", type="PE", strike=960, expiry="2025-08-28", dte=2,
                  ltp=2.30, lot_size=1100, iv=0.24, gamma=0.0144, delta=-0.20, theta_pct_day=0.4427,
                  ta_tag="Bearish confirmation", tp=1.25, sl=3.70),
    ]

    picks = select_trades(rows, underlying_quotes)

    # Pretty-print summary
    from pprint import pprint
    pprint(picks)
