from datetime import datetime, timedelta
import statistics, json

def detect_order_blocks(data):
    bullish_ob, bearish_ob = [], []
    n = len(data)
    for i in range(1, n - 1):
        c0, c1 = data[i], data[i + 1]
        bull_low, bull_high = c0['low'], c0['open']
        bear_low, bear_high = c0['open'], c0['high']
        if c0['close'] > c0['open'] and c1['close'] < c0['low']:
            bearish_ob.append({"zone_low": float(min(bear_low, bear_high)),
                               "zone_high": float(max(bear_low, bear_high)),
                               "timestamp": c0.get('date')})
        if c0['open'] > c0['close'] and c1['close'] > c0['high']:
            bullish_ob.append({"zone_low": float(min(bull_low, bull_high)),
                               "zone_high": float(max(bull_low, bull_high)),
                               "timestamp": c0.get('date')})
    return bullish_ob, bearish_ob

def calculate_ema(data, period):
    closes = [c['close'] for c in data]
    if not closes: return [None]
    k = 2 / (period + 1)
    ema_vals, ema = [], float(closes[0])
    for c in closes:
        ema = float(c) * k + ema * (1 - k)
        ema_vals.append(round(ema, 2))
    return ema_vals

def _stochastic_kd_latest_prev(data, k_period=14, d_period=3):
    if len(data) < k_period: return None, None, None, None
    highs = [c['high'] for c in data]; lows = [c['low'] for c in data]; closes = [c['close'] for c in data]
    k_vals = []
    for i in range(k_period - 1, len(data)):
        hi = max(highs[i-k_period+1:i+1]); lo = min(lows[i-k_period+1:i+1])
        denom = (hi - lo) or 1e-12
        k_vals.append(100.0 * (closes[i] - lo) / denom)
    if not k_vals: return None, None, None, None
    d_vals = [sum(k_vals[j-d_period+1:j+1])/d_period for j in range(d_period-1, len(k_vals))]
    k_now = round(k_vals[-1], 2); k_prev = round(k_vals[-2], 2) if len(k_vals) > 1 else None
    d_now = round(d_vals[-1], 2) if d_vals else None
    d_prev = round(d_vals[-2], 2) if len(d_vals) > 1 else None
    return k_now, d_now, k_prev, d_prev

def _in_zone_with_buffer(price, lo, hi, buffer_pct=0.003):
    return lo*(1-buffer_pct) <= price <= hi*(1+buffer_pct)

def run_smc_scan(kite):
    """
    SAME signature & return structure.
    Timeframe: 4-hour.
    EMA5/EMA10 used (returned under ema20/ema50 for schema).
    Stochastic thresholds: 40/60.
    """
    from_date = datetime.now() - timedelta(days=90)
    to_date = datetime.now()
    results = {}

    with open("nifty500_tokens.json","r") as f:
        tokens = json.load(f)
    if isinstance(tokens, list): tokens = {t['symbol']: t['token'] for t in tokens}
    tokens.setdefault("NIFTY", 256265)

    BUY_K_MAX, SELL_K_MIN, ZONE_BUFFER = 40, 60, 0.003

    for symbol, token in tokens.items():
        try:
            ohlc = kite.historical_data(token, from_date, to_date, "4hour")
            if not ohlc or len(ohlc) < 20: continue

            bullish, bearish = detect_order_blocks(ohlc)
            price = float(ohlc[-1]['close'])

            ema5  = calculate_ema(ohlc, 5)[-1]
            ema10 = calculate_ema(ohlc, 10)[-1]

            k_now, d_now, k_prev, d_prev = _stochastic_kd_latest_prev(ohlc, 14, 3)
            if k_now is None: continue
            rsi_value = k_now   # store %K in 'rsi'

            avg_vol = statistics.mean([c['volume'] for c in ohlc[-11:-1]]) if len(ohlc) >= 12 else \
                      statistics.mean([c['volume'] for c in ohlc[:-1]]) if len(ohlc) > 1 else 0
            volume_spike = ohlc[-1]['volume'] > 1.5 * avg_vol if avg_vol else False

            trend = "Bullish" if ema5 and ema10 and ema5 > ema10 else \
                    "Bearish" if ema5 and ema10 and ema5 < ema10 else "Neutral"

            confirmed = False
            # Buy confirmation
            for ob in reversed(bullish):
                lo, hi = ob['zone_low'], ob['zone_high']
                in_zone = _in_zone_with_buffer(price, lo, hi, ZONE_BUFFER)
                cross_up = (k_prev is not None and d_prev is not None and k_prev <= d_prev and k_now > d_now)
                if in_zone and ((trend=="Bullish" and k_now<=BUY_K_MAX) or (cross_up and k_prev<=20 and k_now>20)):
                    results[symbol] = {"status":"Confirmed Buy","zone":[lo,hi],"price":price,
                                       "ema20":ema5,"ema50":ema10,"rsi":rsi_value,
                                       "volume_spike":volume_spike,"trend":trend}
                    confirmed=True; break
            if confirmed: continue

            # Sell confirmation
            for ob in reversed(bearish):
                lo, hi = ob['zone_low'], ob['zone_high']
                in_zone = _in_zone_with_buffer(price, lo, hi, ZONE_BUFFER)
                cross_dn = (k_prev is not None and d_prev is not None and k_prev >= d_prev and k_now < d_now)
                if in_zone and ((trend=="Bearish" and k_now>=SELL_K_MIN) or (cross_dn and k_prev>=80 and k_now<80)):
                    results[symbol] = {"status":"Confirmed Sell","zone":[lo,hi],"price":price,
                                       "ema20":ema5,"ema50":ema10,"rsi":rsi_value,
                                       "volume_spike":volume_spike,"trend":trend}
                    break
        except Exception:
            continue

    return results
