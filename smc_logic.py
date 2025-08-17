from datetime import datetime, timedelta
import statistics
import json

def detect_order_blocks(data):
    """
    Expects list of candles: {'date','open','high','low','close','volume'}
    Returns lists of OB dicts with zone_low/zone_high included.
    """
    bullish_ob = []
    bearish_ob = []
    n = len(data)
    for i in range(2, n - 2):
        curr = data[i]
        n1 = data[i + 1]
        n2 = data[i + 2]

        # ----- Bearish OB: strong up candle then two closes below its low -----
        if curr['close'] > curr['open'] and n1['close'] < curr['low'] and n2['close'] < curr['low']:
            # OB zone (wick-inclusive convention): [open, high]
            zone_low = curr['open']
            zone_high = curr['high']

            # Mitigation: any future candle overlapping the zone
            mitigated = False
            for j in range(i + 3, n):
                if (data[j]['high'] >= zone_low) and (data[j]['low'] <= zone_high):
                    mitigated = True
                    break
            if not mitigated:
                bearish_ob.append({
                    "index": i,
                    "timestamp": curr.get('date'),
                    "type": "bearish",
                    "zone_low": float(zone_low),
                    "zone_high": float(zone_high),
                    "candle": curr
                })

        # ----- Bullish OB: strong down candle then two closes above its high -----
        if curr['open'] > curr['close'] and n1['close'] > curr['high'] and n2['close'] > curr['high']:
            # OB zone (wick-inclusive convention): [low, open]
            zone_low = curr['low']
            zone_high = curr['open']

            mitigated = False
            for j in range(i + 3, n):
                if (data[j]['high'] >= zone_low) and (data[j]['low'] <= zone_high):
                    mitigated = True
                    break
            if not mitigated:
                bullish_ob.append({
                    "index": i,
                    "timestamp": curr.get('date'),
                    "type": "bullish",
                    "zone_low": float(zone_low),
                    "zone_high": float(zone_high),
                    "candle": curr
                })

    return bullish_ob, bearish_ob


def calculate_ema_from_closes(closes, period):
    """Return list of EMAs for the closes; last value is the most recent EMA."""
    if not closes:
        return []
    k = 2 / (period + 1)
    ema_values = []
    ema = float(closes[0])
    for c in closes:
        ema = (float(c) * k) + (ema * (1 - k))
        ema_values.append(ema)
    return [round(v, 2) for v in ema_values]


def rsi_wilder_from_closes(closes, period=14):
    """
    Wilder's RSI; returns the latest RSI value.
    Requires at least period+1 closes.
    """
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(0.0, change)
        loss = max(0.0, -change)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def run_smc_scan(kite):
    # Use a longer lookback for daily candles to stabilize EMA50 & RSI14
    from_date = datetime.now() - timedelta(days=270)  # ~9 months
    to_date = datetime.now()
    results = {}

    with open("nifty500_tokens.json", "r") as f:
        tokens = json.load(f)

    # Ensure dict[str,int] format if file is a list of dicts
    if isinstance(tokens, list):
        tokens = {item['symbol']: item['token'] for item in tokens}

    # Optionally add index
    tokens["NIFTY"] = 256265

    for symbol, token in tokens.items():
        try:
            # ---------- DAILY candles ----------
            ohlc = kite.historical_data(token, from_date, to_date, "day")
            if not ohlc or len(ohlc) < 60:  # need history for EMA50/RSI14
                continue

            bullish_obs, bearish_obs = detect_order_blocks(ohlc)

            closes = [c['close'] for c in ohlc]
            last_close = closes[-1]

            ema20 = calculate_ema_from_closes(closes, 20)[-1]
            ema50 = calculate_ema_from_closes(closes, 50)[-1]
            rsi = rsi_wilder_from_closes(closes[-(50 + 1):], period=14)

            # Volume spike vs last 10 completed daily bars
            if len(ohlc) >= 12:
                avg_vol = statistics.mean([c['volume'] for c in ohlc[-11:-1]])
            else:
                avg_vol = statistics.mean([c['volume'] for c in ohlc[:-1]]) if len(ohlc) > 1 else 0
            volume_spike = ohlc[-1]['volume'] > 1.5 * avg_vol if avg_vol else False

            trend_tag = "Bullish" if ema20 > ema50 else ("Bearish" if ema20 < ema50 else "Neutral")

            flagged = False
            for ob in reversed(bullish_obs):
                if ob['zone_low'] <= last_close <= ob['zone_high']:
                    results[symbol] = {
                        "status": "In Buy Block",
                        "zone": [round(ob['zone_low'], 2), round(ob['zone_high'], 2)],
                        "price": last_close,
                        "ema20": ema20,
                        "ema50": ema50,
                        "rsi14": rsi,
                        "volume_spike": volume_spike,
                        "trend": trend_tag,
                        "ob_time": ob["timestamp"]
                    }
                    flagged = True
                    break

            if flagged:
                continue

            for ob in reversed(bearish_obs):
                if ob['zone_low'] <= last_close <= ob['zone_high']:
                    results[symbol] = {
                        "status": "In Sell Block",
                        "zone": [round(ob['zone_low'], 2), round(ob['zone_high'], 2)],
                        "price": last_close,
                        "ema20": ema20,
                        "ema50": ema50,
                        "rsi14": rsi,
                        "volume_spike": volume_spike,
                        "trend": trend_tag,
                        "ob_time": ob["timestamp"]
                    }
                    break

        except Exception:
            # Skip token-specific issues (rate limits/invalid tokens)
            continue

    return results
