from datetime import datetime, timedelta
import statistics
import json

# ---- Loose OB detection on daily candles ----
def detect_order_blocks(data):
    """
    Kept the same function name for compatibility.
    Now uses a looser rule with ONE confirming close.
    Returns lists of dicts with 'zone_low'/'zone_high' stored.
    """
    bullish_ob = []
    bearish_ob = []
    n = len(data)

    for i in range(1, n - 1):
        c0 = data[i]       # candidate OB candle
        c1 = data[i + 1]   # confirmation candle (displacement)

        # Define zones (wick-inclusive, looser)
        bull_zone_low, bull_zone_high = c0['low'], c0['open']   # last down candle
        bear_zone_low, bear_zone_high = c0['open'], c0['high']  # last up candle

        # Bearish OB: up candle then next close < its low
        if c0['close'] > c0['open'] and c1['close'] < c0['low']:
            bearish_ob.append({
                "index": i,
                "timestamp": c0.get('date'),
                "zone_low": float(min(bear_zone_low, bear_zone_high)),
                "zone_high": float(max(bear_zone_low, bear_zone_high)),
                "ref": c0
            })

        # Bullish OB: down candle then next close > its high
        if c0['open'] > c0['close'] and c1['close'] > c0['high']:
            bullish_ob.append({
                "index": i,
                "timestamp": c0.get('date'),
                "zone_low": float(min(bull_zone_low, bull_zone_high)),
                "zone_high": float(max(bull_zone_low, bull_zone_high)),
                "ref": c0
            })

    return bullish_ob, bearish_ob


# ---- Simple EMA helper (kept because callers expect ema20/ema50 keys) ----
def calculate_ema(data, period):
    closes = [c['close'] for c in data]
    if not closes:
        return [None]
    k = 2 / (period + 1)
    ema_values = []
    ema = float(closes[0])
    for c in closes:
        ema = float(c) * k + ema * (1 - k)
        ema_values.append(round(ema, 2))
    return ema_values

# ---- Stochastic (14,3) %K/%D; we will return %K via the existing 'rsi' key ----
def _stochastic_kd(data, k_period=14, d_period=3):
    if len(data) < k_period:
        return None, None
    highs = [c['high'] for c in data]
    lows = [c['low'] for c in data]
    closes = [c['close'] for c in data]

    k_vals = []
    for i in range(k_period - 1, len(data)):
        hi = max(highs[i - k_period + 1:i + 1])
        lo = min(lows[i - k_period + 1:i + 1])
        denom = (hi - lo) or 1e-12
        k = 100.0 * (closes[i] - lo) / denom
        k_vals.append(k)

    if not k_vals:
        return None, None
    if len(k_vals) < d_period:
        return round(k_vals[-1], 2), None

    d_vals = []
    for j in range(d_period - 1, len(k_vals)):
        d_vals.append(sum(k_vals[j - d_period + 1:j + 1]) / d_period)

    return round(k_vals[-1], 2), round(d_vals[-1], 2) if d_vals else None


def run_smc_scan(kite):
    """
    SAME SIGNATURE & SAME RETURN STRUCTURE as your original.

    - timeframe: DAILY ("day")
    - logic: OB detection (loose) + Stochastic(14,3)
    - compatibility: 'rsi' field now carries Stochastic %K (float)
    """
    from_date = datetime.now() - timedelta(days=370)  # ~1 year
    to_date = datetime.now()
    results = {}

    with open("nifty500_tokens.json", "r") as f:
        tokens = json.load(f)
    if isinstance(tokens, list):
        tokens = {item['symbol']: item['token'] for item in tokens}
    tokens.setdefault("NIFTY", 256265)

    for symbol, token in tokens.items():
        try:
            # ---- DAILY candles ----
            ohlc = kite.historical_data(token, from_date, to_date, "day")
            if not ohlc or len(ohlc) < 20:
                continue

            bullish, bearish = detect_order_blocks(ohlc)
            current_price = float(ohlc[-1]['close'])

            # Keep ema20/ema50 fields for backward compatibility (daily EMAs)
            ema20 = calculate_ema(ohlc, 20)[-1] if len(ohlc) >= 20 else None
            ema50 = calculate_ema(ohlc, 50)[-1] if len(ohlc) >= 50 else ema20

            # Stochastic(14,3): store %K in 'rsi' field to preserve structure
            stoch_k, stoch_d = _stochastic_kd(ohlc, 14, 3)
            rsi_value = stoch_k  # <- IMPORTANT: keeping key name 'rsi' as requested

            # Volume spike vs last 10 completed daily bars (unchanged key)
            if len(ohlc) >= 12:
                avg_volume = statistics.mean([c['volume'] for c in ohlc[-11:-1]])
            else:
                avg_volume = statistics.mean([c['volume'] for c in ohlc[:-1]]) if len(ohlc) > 1 else 0
            volume_spike = ohlc[-1]['volume'] > 1.5 * avg_volume if avg_volume else False

            # Trend label using price vs ema20 to preserve 'trend' key semantics
            trend = "Bullish" if (ema20 is not None and current_price > ema20) else \
                    ("Bearish" if (ema20 is not None and current_price < ema20) else "Neutral")

            # Fill results exactly like before
            # Use newest OB from each side and check if price is inside
            filled = False
            for ob in reversed(bullish):
                if ob['zone_low'] <= current_price <= ob['zone_high']:
                    results[symbol] = {
                        "status": "In Buy Block",
                        "zone": [ob['zone_low'], ob['zone_high']],
                        "price": current_price,
                        "ema20": ema20,
                        "ema50": ema50,
                        "rsi": rsi_value,            # %K stored here to keep structure
                        "volume_spike": volume_spike,
                        "trend": trend
                    }
                    filled = True
                    break

            if filled:
                continue

            for ob in reversed(bearish):
                if ob['zone_low'] <= current_price <= ob['zone_high']:
                    results[symbol] = {
                        "status": "In Sell Block",
                        "zone": [ob['zone_low'], ob['zone_high']],
                        "price": current_price,
                        "ema20": ema20,
                        "ema50": ema50,
                        "rsi": rsi_value,            # %K stored here
                        "volume_spike": volume_spike,
                        "trend": trend
                    }
                    filled = True
                    break

            # If not currently in any zone, you may skip symbol to match old behavior
            # (i.e., only symbols inside an OB appear in results)

        except Exception:
            continue

    return results
