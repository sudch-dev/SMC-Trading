from datetime import datetime, timedelta
import statistics
import json

def detect_order_blocks(data):
    bullish_ob = []
    bearish_ob = []
    n = len(data)

    for i in range(1, n - 1):
        c0 = data[i]
        c1 = data[i + 1]

        # Wick-inclusive zones (looser)
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
    SAME signature and return structure.
    Timeframe switched to 4-hour candles.
    """
    from_date = datetime.now() - timedelta(days=60)   # ~2 months of 4h data
    to_date = datetime.now()
    results = {}

    with open("nifty500_tokens.json", "r") as f:
        tokens = json.load(f)
    if isinstance(tokens, list):
        tokens = {item['symbol']: item['token'] for item in tokens}
    tokens.setdefault("NIFTY", 256265)

    for symbol, token in tokens.items():
        try:
            # -------- 4 HOUR candles --------
            ohlc = kite.historical_data(token, from_date, to_date, "4hour")
            if not ohlc or len(ohlc) < 20:
                continue

            bullish, bearish = detect_order_blocks(ohlc)
            current_price = float(ohlc[-1]['close'])

            ema20 = calculate_ema(ohlc, 20)[-1] if len(ohlc) >= 20 else None
            ema50 = calculate_ema(ohlc, 50)[-1] if len(ohlc) >= 50 else ema20

            stoch_k, stoch_d = _stochastic_kd(ohlc, 14, 3)
            rsi_value = stoch_k   # keep structure: put Stochastic %K in 'rsi' key

            if len(ohlc) >= 12:
                avg_volume = statistics.mean([c['volume'] for c in ohlc[-11:-1]])
            else:
                avg_volume = statistics.mean([c['volume'] for c in ohlc[:-1]]) if len(ohlc) > 1 else 0
            volume_spike = ohlc[-1]['volume'] > 1.5 * avg_volume if avg_volume else False

            trend = "Bullish" if (ema20 is not None and current_price > ema20) else \
                    ("Bearish" if (ema20 is not None and current_price < ema20) else "Neutral")

            filled = False
            for ob in reversed(bullish):
                if ob['zone_low'] <= current_price <= ob['zone_high']:
                    results[symbol] = {
                        "status": "In Buy Block",
                        "zone": [ob['zone_low'], ob['zone_high']],
                        "price": current_price,
                        "ema20": ema20,
                        "ema50": ema50,
                        "rsi": rsi_value,   # stochastic %K
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
                        "rsi": rsi_value,   # stochastic %K
                        "volume_spike": volume_spike,
                        "trend": trend
                    }
                    break

        except Exception:
            continue

    return results
