from collections import deque
from config import (
    PAIRS, EMA_SHORT, EMA_MID, EMA_LONG, RSI_PERIOD, ADX_PERIOD,
    ADX_MIN, BB_PERIOD, BB_STD, CONFLUENCE_THRESHOLD
)

class S3Analyzer:
    def __init__(self):
        self.price_history = {pair: deque(maxlen=120) for pair in PAIRS}
        self.high_history = {pair: deque(maxlen=120) for pair in PAIRS}
        self.low_history = {pair: deque(maxlen=120) for pair in PAIRS}
        self.m5_trend = {pair: None for pair in PAIRS}   # BULLISH / BEARISH
        self.last_signal = {pair: None for pair in PAIRS}

    def set_m5_trend(self, pair, trend):
        self.m5_trend[pair] = trend

    # ---------- Math helpers ----------
    def _ema(self, prices, period):
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = (p - ema) * k + ema
        return ema

    def _rsi(self, prices, period=14):
        if len(prices) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _macd_histogram(self, prices):
        if len(prices) < 35:
            return None
        ema12 = self._ema(prices, 12)
        ema26 = self._ema(prices, 26)
        if ema12 is None or ema26 is None:
            return None
        macd_now = ema12 - ema26
        prev_prices = prices[:-3]
        ema12_p = self._ema(prev_prices, 12)
        ema26_p = self._ema(prev_prices, 26)
        if ema12_p is None or ema26_p is None:
            return None
        macd_prev = ema12_p - ema26_p
        return macd_now - macd_prev

    def _bollinger(self, prices, period=20, std_mult=2.0):
        if len(prices) < period:
            return None, None, None
        window = prices[-period:]
        sma = sum(window) / period
        variance = sum((p - sma) ** 2 for p in window) / period
        std = variance ** 0.5
        return sma - std_mult * std, sma, sma + std_mult * std

    def _adx(self, highs, lows, closes, period=14):
        if len(closes) < period + 1:
            return None
        tr_list, plus_dm, minus_dm = [], [], []
        for i in range(1, len(closes)):
            high_diff = highs[i] - highs[i - 1]
            low_diff = lows[i - 1] - lows[i]
            plus_dm.append(high_diff if (high_diff > low_diff and high_diff > 0) else 0)
            minus_dm.append(low_diff if (low_diff > high_diff and low_diff > 0) else 0)
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_list.append(tr)
        atr = sum(tr_list[-period:]) / period
        if atr == 0:
            return 0
        plus_di = 100 * (sum(plus_dm[-period:]) / period) / atr
        minus_di = 100 * (sum(minus_dm[-period:]) / period) / atr
        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0
        return 100 * abs(plus_di - minus_di) / di_sum

    # ---------- Main analyze ----------
    def analyze(self, pair, price, high=None, low=None):
        prices = list(self.price_history[pair])
        prices.append(price)
        self.price_history[pair].append(price)
        self.high_history[pair].append(high if high else price)
        self.low_history[pair].append(low if low else price)

        if len(prices) < 60:
            return None, 0.0

        highs = list(self.high_history[pair])
        lows = list(self.low_history[pair])

        votes_call = 0
        votes_put = 0
        total = 0

        # 1. EMA 9 vs 21
        ema9 = self._ema(prices, EMA_SHORT)
        ema21 = self._ema(prices, EMA_MID)
        if ema9 and ema21:
            total += 1
            if ema9 > ema21:
                votes_call += 1
            else:
                votes_put += 1

        # 2. EMA 50 major trend
        ema50 = self._ema(prices, EMA_LONG)
        if ema50:
            total += 1
            if price > ema50:
                votes_call += 1
            else:
                votes_put += 1

        # 3. RSI (NON-OVERLAPPING zones - FIXED)
        rsi = self._rsi(prices, RSI_PERIOD)
        if rsi is not None:
            total += 1
            if 55 <= rsi < 72:
                votes_call += 1
            elif 28 < rsi <= 45:
                votes_put += 1
            # 45-55 = neutral, no vote

        # 4. MACD histogram direction
        macd_hist = self._macd_histogram(prices)
        if macd_hist is not None:
            total += 1
            if macd_hist > 0:
                votes_call += 1
            else:
                votes_put += 1

        # 5. Bollinger Band position
        bb_low, bb_mid, bb_high = self._bollinger(prices, BB_PERIOD, BB_STD)
        if bb_low is not None:
            total += 1
            band_width = bb_high - bb_low
            if band_width > 0:
                pos = (price - bb_low) / band_width
                if 0.5 < pos < 0.85:
                    votes_call += 1
                elif 0.15 < pos < 0.5:
                    votes_put += 1

        # 6. ADX trend strength (kill signal if choppy)
        adx = self._adx(highs, lows, prices, ADX_PERIOD)
        if adx is not None:
            if adx < ADX_MIN:
                return None, 0.0
            total += 1
            if ema9 and ema21:
                if ema9 > ema21:
                    votes_call += 1
                else:
                    votes_put += 1

        # 7. M5 higher-timeframe alignment
        m5 = self.m5_trend.get(pair)
        if m5 == "BULLISH":
            total += 1
            votes_call += 1
        elif m5 == "BEARISH":
            total += 1
            votes_put += 1

        if total < 6:
            return None, 0.0

        if votes_call >= CONFLUENCE_THRESHOLD and votes_call > votes_put:
            return "CALL", (votes_call / total) * 100
        elif votes_put >= CONFLUENCE_THRESHOLD and votes_put > votes_call:
            return "PUT", (votes_put / total) * 100

        return None, 0.0
