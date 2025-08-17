from datetime import datetime, timedelta
import json

# --------- Loose OB detection (daily) ---------
def detect_order_blocks_loose(data, body_only=False, min_body_ratio=0.0):
    """
    data: list of candles: {'date','open','high','low','close','volume'}
    body_only=False -> zone uses wicks (looser, wider). True -> body-only (tighter).
    min_body_ratio: 0..1, require |close-open| >= ratio * (high-low). Use 0 for loosest.
    Rule: OB = last opposite candle where the NEXT candle's close displaces beyond that candle's extreme.
      - Bullish OB: down candle s.t. next close > that candle's high
      - Bearish OB: up candle s.t. next close < that candle's low
    No mitigation filter (looser).
    """
    bulls, bears = []
    n = len(data)
    for i in range(1, n - 1):
        c0 = data[i]       # candidate OB candle
        c1 = data[i + 1]   # confirmation candle

        rng = max(c0['high'] - c0['low'], 1e-12)
        body = abs(c0['close'] - c0['open'])
        if body < min_body_ratio * rng:
            continue  # skip tiny bodies if requested

        if body_only:
            # body-only zones
            z_low = min(c0['open'], c0['close'])
            z_high = max(c0['open'], c0['close'])
            bull_zone_low, bull_zone_high = z_low, z_high
            bear_zone_low, bear_zone_high = z_low, z_high
        else:
            # wick-inclusive (looser)
            bull_zone_low, bull_zone_high = c0['low'], c0['open']  # bullish OB = last down candle
            bear_zone_low, bear_zone_high = c0['open'], c0['high'] # bearish OB = last up candle

        # Bearish OB: up candle then next close < its low
        if c0['close'] > c0['open'] and c1['close'] < c0['low']:
            bears.append({
                "index": i,
                "timestamp": c0.get('date'),
                "zone_low": float(min(bear_zone_low, bear_zone_high)),
                "zone_high": float(max(bear_zone_low, bear_zone_high)),
                "ref": c0
            })

        # Bullish OB: down candle then next close > its high
        if c0['open'] > c0['close'] and c1['close'] > c0['high']:
            bulls.append({
                "index": i,
                "timestamp": c0.get('date'),
                "zone_low": float(min(bull_zone_low, bull_zone_high)),
                "zone_high": float(max(bull_zone_low, bull_zone_high)),
                "ref": c0
            })

    return bulls, bears


# --------- Stochastic (14,3) ---------
def stochastic_14_3(data, k_period=14, d_period=3):
    if len(data) < k_period:
        return None, None
    highs = [c['high'] for c in data]
    lows = [c['low'] for c in data]
    closes = [c['close'] for c in data]

    k_values = []
    for i in range(k_period - 1, len(data)):
        hi = max(highs[i - k_period + 1:i + 1])
        lo = min(lows[i - k_period + 1:i + 1])
        denom = (hi - lo) or 1e-12
        k = 100.0 * (closes[i] - lo) / denom
        k_values.append(k)

    if not k_values:
        return None, None
    if len(k_values) < d_period:
        return round(k_values[-1], 2), None

    d_values = []
    for j in range(d_period - 1, len(k_values)):
        d_values.append(sum(k_values[j - d_period + 1:j + 1]) / d_period)

    k_latest = round(k_values[-1], 2)
    d_latest = round(d_values[-1], 2) if d_values else None
    return k_latest, d_latest


# --------- Helper: newest zone & in-zone flag ---------
def _format_zone(ob):
    if not ob:
        return None
    ts = ob["timestamp"].strftime("%Y-%m-%d") if ob["timestamp"] else None
    return [round(ob["zone_low"], 2), round(ob["zone_high"], 2), ts]

def _in_zone(price, zone):
    if not zone:
        return False
    lo, hi, _ = zone
    return lo <= price <= hi


# --------- Daily scanner (OBs + Stochastic + in-zone flags) ---------
def run_daily_ob_stoch_scan(kite, tokens_path="nifty500_tokens.json"):
    """
    Fetches ~1 year of DAILY candles, detects *loose* OBs and computes Stochastic(14,3).
    Adds 'in_bullish_zone' and 'in_bearish_zone' for the most recent zones.
    returns:
      results[symbol] = {
        "active_bullish_zone": [low, high, "YYYY-MM-DD"] or None,
        "active_bearish_zone": [low, high, "YYYY-MM-DD"] or None,
        "in_bullish_zone": bool,
        "in_bearish_zone": bool,
        "recent_bullish_obs": [[low, high, "YYYY-MM-DD"], ... up to 3],
        "recent_bearish_obs": [[low, high, "YYYY-MM-DD"], ... up to 3],
        "stoch_k": float,
        "stoch_d": float or None,
        "stoch_state": "Overbought/Oversold/Neutral",
        "last_close": float
      }
    """
    from_date = datetime.now() - timedelta(days=370)  # ~1 year
    to_date = datetime.now()

    with open(tokens_path, "r") as f:
        tokens = json.load(f)
    if isinstance(tokens, list):
        tokens = {item['symbol']: item['token'] for item in tokens}

    tokens.setdefault("NIFTY", 256265)

    results = {}
    for symbol, token in tokens.items():
        try:
            ohlc = kite.historical_data(token, from_date, to_date, "day")
            if not ohlc or len(ohlc) < 20:
                continue

            bulls, bears = detect_order_blocks_loose(
                ohlc,
                body_only=False,     # wick-inclusive zones (looser)
                min_body_ratio=0.0   # accept tiny bodies (loosest)
            )

            # newest (most recent) zones
            active_bullish_zone = _format_zone(bulls[-1]) if bulls else None
            active_bearish_zone = _format_zone(bears[-1]) if bears else None

            # latest few for display
            recent_bulls = [_format_zone(ob) for ob in bulls[-3:]] if bulls else []
            recent_bears = [_format_zone(ob) for ob in bears[-3:]] if bears else []

            last_close = float(ohlc[-1]['close'])

            # in-zone flags vs newest zones
            in_bullish = _in_zone(last_close, active_bullish_zone)
            in_bearish = _in_zone(last_close, active_bearish_zone)

            # stochastic(14,3)
            k, d = stochastic_14_3(ohlc, 14, 3)
            if k is None:
                continue
            state = "Overbought" if k >= 80 else ("Oversold" if k <= 20 else "Neutral")

            results[symbol] = {
                "active_bullish_zone": active_bullish_zone,
                "active_bearish_zone": active_bearish_zone,
                "in_bullish_zone": in_bullish,
                "in_bearish_zone": in_bearish,
                "recent_bullish_obs": [z for z in recent_bulls if z],
                "recent_bearish_obs": [z for z in recent_bears if z],
                "stoch_k": k,
                "stoch_d": d,
                "stoch_state": state,
                "last_close": round(last_close, 2),
            }

        except Exception:
            continue

    return results
