from datetime import datetime, timedelta
import statistics, json

# =========================
# Helpers: OB, EMA, Stoch, ATR
# =========================
def detect_order_blocks(data):
    """Loose OB: single displacement close, wick-inclusive zones."""
    bullish_ob, bearish_ob = [], []
    n = len(data)
    for i in range(1, n - 1):
        c0, c1 = data[i], data[i + 1]
        bull_low, bull_high = c0['low'], c0['open']    # last down candle
        bear_low, bear_high = c0['open'], c0['high']   # last up candle
        # Bearish OB: up candle then next close < its low
        if c0['close'] > c0['open'] and c1['close'] < c0['low']:
            bearish_ob.append({
                "zone_low": float(min(bear_low, bear_high)),
                "zone_high": float(max(bear_low, bear_high)),
                "timestamp": c0.get('date')
            })
        # Bullish OB: down candle then next close > its high
        if c0['open'] > c0['close'] and c1['close'] > c0['high']:
            bullish_ob.append({
                "zone_low": float(min(bull_low, bull_high)),
                "zone_high": float(max(bull_low, bull_high)),
                "timestamp": c0.get('date')
            })
    return bullish_ob, bearish_ob

def calculate_ema(data, period):
    closes = [c['close'] for c in data]
    if not closes:
        return [None]
    k = 2 / (period + 1)
    ema_vals, ema = [], float(closes[0])
    for c in closes:
        ema = float(c) * k + ema * (1 - k)
        ema_vals.append(round(ema, 2))
    return ema_vals

def _stochastic_kd_latest_prev(data, k_period=14, d_period=3):
    if len(data) < k_period:
        return None, None, None, None
    highs = [c['high'] for c in data]
    lows  = [c['low'] for c in data]
    closes= [c['close'] for c in data]
    k_vals = []
    for i in range(k_period - 1, len(data)):
        hi = max(highs[i-k_period+1:i+1])
        lo = min(lows[i-k_period+1:i+1])
        denom = (hi - lo) or 1e-12
        k_vals.append(100.0 * (closes[i] - lo) / denom)
    if not k_vals:
        return None, None, None, None
    d_vals = [sum(k_vals[j-d_period+1:j+1])/d_period for j in range(d_period-1, len(k_vals))]
    k_now = round(k_vals[-1], 2)
    k_prev = round(k_vals[-2], 2) if len(k_vals) > 1 else None
    d_now = round(d_vals[-1], 2) if d_vals else None
    d_prev = round(d_vals[-2], 2) if len(d_vals) > 1 else None
    return k_now, d_now, k_prev, d_prev

def _atr_percent(cl_data, period=14):
    """Daily ATR% = (ATR / close) * 100, over last 'period' bars."""
    if len(cl_data) < period + 1:
        return None
    trs = []
    for i in range(1, len(cl_data)):
        h = cl_data[i]['high']; l = cl_data[i]['low']
        pc = cl_data[i-1]['close']
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    atr = sum(trs[-period:]) / float(period)
    last_close = cl_data[-1]['close']
    return (atr / last_close) * 100.0 if last_close else None

# =========================
# Market mood → interval
# =========================
def _pick_interval_by_mood(kite, index_token=256265):
    """
    Uses NIFTY daily data to infer mood and pick interval:
      - Trending vs Sideways via EMA20/EMA50 spread (% of price)
      - Volatile vs Calm via ATR14 %
    """
    to_date = datetime.now()
    from_date = to_date - timedelta(days=270)  # ~9 months
    try:
        daily = kite.historical_data(index_token, from_date, to_date, "day")
        if not daily or len(daily) < 60:
            return "4hour", 90  # safe default
        # trend strength on daily
        ema20 = calculate_ema(daily, 20)[-1]
        ema50 = calculate_ema(daily, 50)[-1]
        px    = float(daily[-1]['close'])
        spread_pct = abs(ema20 - ema50) / px * 100.0 if ema20 and ema50 and px else 0.0
        # volatility
        atr_pct = _atr_percent(daily, 14) or 0.0

        trending = spread_pct >= 0.5     # >=0.5% separation ⇒ trend
        volatile = atr_pct   >= 1.8      # >=1.8% daily ATR ⇒ volatile

        # map to interval + lookback days
        if trending and volatile:
            return "1hour", 45   # fast & noisy → 1h
        elif trending and not volatile:
            return "2hour", 60   # steady trend → 2h
        elif (not trending) and volatile:
            return "4hour", 120  # choppy but active → 4h
        else:
            return "day", 270    # calm & sideways → daily
    except Exception:
        return "4hour", 90

# =========================
# Main scan (keeps schema)
# =========================
def _in_zone_with_buffer(price, lo, hi, buffer_pct=0.003):
    return lo*(1-buffer_pct) <= price <= hi*(1+buffer_pct)

def run_smc_scan(kite):
    """
    SAME signature & SAME return structure expected by app.py.
    Auto-detects market mood (from NIFTY) and auto-selects timeframe.
    - Interval ∈ {"1hour","2hour","4hour","day"} (Render/Kite-compatible strings)
    - OB detection (loose), Stochastic(14,3)
    - EMA5/EMA10 used for responsiveness, returned under keys 'ema20'/'ema50'
    - 'rsi' carries Stochastic %K for compatibility
    - Only returns CONFIRMED stocks (clean list)
    """
    # 1) Decide interval + lookback
    interval, lookback_days = _pick_interval_by_mood(kite, 256265)
    to_date = datetime.now()
    from_date = to_date - timedelta(days=lookback_days)

    results = {}

    # 2) Load tokens
    with open("nifty500_tokens.json","r") as f:
        tokens = json.load(f)
    if isinstance(tokens, list):
        tokens = {t['symbol']: t['token'] for t in tokens}
    tokens.setdefault("NIFTY", 256265)  # ok to include index

    # thresholds (can tweak live)
    BUY_K_MAX, SELL_K_MIN, ZONE_BUFFER = 40, 60, 0.003

    for symbol, token in tokens.items():
        try:
            ohlc = kite.historical_data(token, from_date, to_date, interval)
            if not ohlc or len(ohlc) < 20:
                continue

            bullish, bearish = detect_order_blocks(ohlc)
            price = float(ohlc[-1]['close'])

            # fast EMAs for responsiveness (kept under ema20/ema50 keys)
            ema5  = calculate_ema(ohlc, 5)[-1]
            ema10 = calculate_ema(ohlc, 10)[-1]

            k_now, d_now, k_prev, d_prev = _stochastic_kd_latest_prev(ohlc, 14, 3)
            if k_now is None:
                continue
            rsi_value = k_now  # %K into 'rsi' key for schema compatibility

            # volume spike vs last 10 completed bars
            if len(ohlc) >= 12:
                avg_vol = statistics.mean([c['volume'] for c in ohlc[-11:-1]])
            else:
                avg_vol = statistics.mean([c['volume'] for c in ohlc[:-1]]) if len(ohlc) > 1 else 0
            volume_spike = ohlc[-1]['volume'] > 1.5 * avg_vol if avg_vol else False

            # trend label via EMA5/EMA10
            trend = "Bullish" if ema5 and ema10 and ema5 > ema10 else \
                    "Bearish" if ema5 and ema10 and ema5 < ema10 else "Neutral"

            confirmed = False

            # ------- Buy confirmations -------
            for ob in reversed(bullish):
                lo, hi = ob['zone_low'], ob['zone_high']
                in_zone = _in_zone_with_buffer(price, lo, hi, ZONE_BUFFER)
                cross_up = (k_prev is not None and d_prev is not None and k_prev <= d_prev and k_now > d_now)
                if in_zone and (
                    (trend == "Bullish" and k_now <= BUY_K_MAX) or
                    (cross_up and (k_prev is not None and k_prev <= 20 and k_now > 20))
                ):
                    results[symbol] = {
                        "status": "Confirmed Buy",
                        "zone": [lo, hi],
                        "price": price,
                        "ema20": ema5,     # intentional mapping for schema
                        "ema50": ema10,    # intentional mapping for schema
                        "rsi": rsi_value,  # Stoch %K
                        "volume_spike": volume_spike,
                        "trend": trend
                    }
                    confirmed = True
                    break
            if confirmed:
                continue

            # ------- Sell confirmations -------
            for ob in reversed(bearish):
                lo, hi = ob['zone_low'], ob['zone_high']
                in_zone = _in_zone_with_buffer(price, lo, hi, ZONE_BUFFER)
                cross_dn = (k_prev is not None and d_prev is not None and k_prev >= d_prev and k_now < d_now)
                if in_zone and (
                    (trend == "Bearish" and k_now >= SELL_K_MIN) or
                    (cross_dn and (k_prev is not None and k_prev >= 80 and k_now < 80))
                ):
                    results[symbol] = {
                        "status": "Confirmed Sell",
                        "zone": [lo, hi],
                        "price": price,
                        "ema20": ema5,
                        "ema50": ema10,
                        "rsi": rsi_value,  # Stoch %K
                        "volume_spike": volume_spike,
                        "trend": trend
                    }
                    break

        except Exception:
            continue

    return results
