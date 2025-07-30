
from datetime import datetime, timedelta
import statistics


def detect_order_blocks(data):
    bullish_ob = []
    bearish_ob = []
    for i in range(2, len(data) - 2):
        curr = data[i]
        next1 = data[i + 1]
        next2 = data[i + 2]

        # Bearish OB: strong bullish candle followed by two closes below its low
        if curr['open'] < curr['close'] and next1['close'] < curr['low'] and next2['close'] < curr['low']:
            zone_high = max(curr['open'], curr['close'])
            zone_low = min(curr['open'], curr['close'])
            mitigated = False
            for j in range(i + 3, len(data)):
                if data[j]['high'] >= zone_low:
                    mitigated = True
                    break
            if not mitigated:
                bearish_ob.append(curr)

        # Bullish OB: strong bearish candle followed by two closes above its high
        if curr['open'] > curr['close'] and next1['close'] > curr['high'] and next2['close'] > curr['high']:
            zone_high = max(curr['open'], curr['close'])
            zone_low = min(curr['open'], curr['close'])
            mitigated = False
            for j in range(i + 3, len(data)):
                if data[j]['low'] <= zone_high:
                    mitigated = True
                    break
            if not mitigated:
                bullish_ob.append(curr)

    return bullish_ob, bearish_ob

def calculate_ema(data, period):
    ema_values = []
    k = 2 / (period + 1)
    ema = data[0]['close']
    for candle in data:
        ema = candle['close'] * k + ema * (1 - k)
        ema_values.append(round(ema, 2))
    return ema_values

def calculate_rsi(data, period=14):
    gains, losses = [], []
    for i in range(1, period + 1):
        change = data[i]['close'] - data[i - 1]['close']
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period if sum(losses) != 0 else 0.001
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)

def run_smc_scan(kite):
    from_date = datetime.now() - timedelta(days=5)
    to_date = datetime.now()
    results = {}
    with open("nifty500_tokens.json", "r") as f:
        import json
        tokens = json.load(f)
    tokens["NIFTY"] = 256265

    for symbol, token in tokens.items():
        try:
            ohlc = kite.historical_data(token, from_date, to_date, "15minute")
            if len(ohlc) < 20:
                continue
            bullish, bearish = detect_order_blocks(ohlc)
            current_price = ohlc[-1]['close']
            ema20 = calculate_ema(ohlc[-20:], 20)[-1]
            ema50 = calculate_ema(ohlc[-50:], 50)[-1] if len(ohlc) >= 50 else ema20
            rsi = calculate_rsi(ohlc[-15:])
            avg_volume = statistics.mean([c['volume'] for c in ohlc[-10:-1]])
            volume_spike = ohlc[-1]['volume'] > 1.5 * avg_volume

            for ob in bullish[::-1]:
                if ob['low'] <= current_price <= ob['high']:
                    results[symbol] = {
                        "status": "In Buy Block",
                        "zone": [ob['low'], ob['high']],
                        "price": current_price,
                        "ema20": ema20,
                        "ema50": ema50,
                        "rsi": rsi,
                        "volume_spike": volume_spike,
                        "trend": "Bullish" if current_price > ema20 else "Neutral"
                    }
                    break

            for ob in bearish[::-1]:
                if ob['low'] <= current_price <= ob['high']:
                    results[symbol] = {
                        "status": "In Sell Block",
                        "zone": [ob['low'], ob['high']],
                        "price": current_price,
                        "ema20": ema20,
                        "ema50": ema50,
                        "rsi": rsi,
                        "volume_spike": volume_spike,
                        "trend": "Bearish" if current_price < ema20 else "Neutral"
                    }
                    break
        except Exception as e:
            continue
    return results
