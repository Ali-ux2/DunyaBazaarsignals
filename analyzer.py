from collections import deque
from config import (
    PAIRS, EMA_SHORT, EMA_MID, EMA_LONG, RSI_PERIOD, ADX_PERIOD, ATR_PERIOD,
    BB_PERIOD, BB_STD, ADX_MIN, ATR_MIN_PCT, ATR_MAX_PCT, WEIGHTED_SCORE_THRESHOLD
)


class S3Analyzer:
    def __init__(self):
        self.price_history = {p: deque(maxlen=120) for p in PAIRS}
        self.high_history  = {p: deque(maxlen=120) for p in PAIRS}
        self.low_history   = {p: deque(maxlen=120) for p in PAIRS}
        self.open_history  = {p: deque(maxlen=120) for p in PAIRS}
        self.m5_trend = {p: None for p in PAIRS}
        self.last_signal = {p: None for p in PAIRS}

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

    def _atr(self, highs, lows, closes, period=14):
        if len(closes) < period + 1:
            return None
        trs = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        return sum(trs[-period:]) / period

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
            hd = highs[i] - highs[i - 1]
            ld = lows[i - 1] - lows[i]
            plus_dm.append(hd if (hd > ld and hd > 0) else 0)
            minus_dm.append(ld if (ld > hd and ld > 0) else 0)
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
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

    def _candle_pattern(self, opens, highs, lows, closes):
        if len(closes) < 2:
            return None
        o1, h1, l1, c1 = opens[-2], highs[-2], lows[-2], closes[-2]
        o2, h2, l2, c2 = opens[-1], highs[-1], lows[-1], closes[-1]

        body2 = abs(c2 - o2)
        range2 = h2 - l2 if h2 > l2 else 0.00001

        # Bullish engulfing
        if c2 > o2 and c1 < o1 and c2 > o1 and o2 < c1:
            return "BULL"
        # Bearish engulfing
        if c2 < o2 and c1 > o1 and c2 < o1 and o2 > c1:
            return "BEAR"
        # Hammer
        lower_wick = min(o2, c2) - l2
        upper_wick = h2 - max(o2, c2)
        if body2 > 0 and lower_wick > 2 * body2 and upper_wick < body2:
            return "BULL"
        # Shooting star
        if body2 > 0 and upper_wick > 2 * body2 and lower_wick < body2:
            return "BEAR"
        return None

    def _rsi_divergence(self, prices, rsi_lookback=5):
        if len(prices) < RSI_PERIOD + rsi_lookback + 1:
            return None
        rsi_series = []
        for i in range(rsi_lookback + 1):
            subset = prices[:-(rsi_lookback - i)] if i < rsi_lookback else prices
            r = self._rsi(subset, RSI_PERIOD)
            if r is not None:
                rsi_series.append(r)
        if len(rsi_series) < 2:
            return None

        price_now = prices[-1]
        price_prev = prices[-(rsi_lookback + 1)]
        rsi_now = rsi_series[-1]
        rsi_prev = rsi_series[0]

        if price_now < price_prev and rsi_now > rsi_prev:
            return "BULL"
        if price_now > price_prev and rsi_now < rsi_prev:
            return "BEAR"
        return None

    # ---------- Main analyze ----------
    def analyze(self, pair, price, high=None, low=None, open_=None):
        prices = list(self.price_history[pair])
        highs  = list(self.high_history[pair])
        lows   = list(self.low_history[pair])
        opens  = list(self.open_history[pair])

        prices.append(price)
        highs.append(high if high else price)
        lows.append(low if low else price)
        opens.append(open_ if open_ else (prices[-2] if len(prices) > 1 else price))

        self.price_history[pair].append(price)
        self.high_history[pair].append(high if high else price)
        self.low_history[pair].append(low if low else price)
        self.open_history[pair].append(opens[-1])

        if len(prices) < 60:
            return None, 0.0

        # ---------- HARD FILTERS ----------
        atr = self._atr(highs, lows, prices, ATR_PERIOD)
        if atr is None or price <= 0:
            return None, 0.0
        atr_pct = atr / price
        if atr_pct < ATR_MIN_PCT:
            return None, 0.0
        if atr_pct > ATR_MAX_PCT:
            return None, 0.0

        adx = self._adx(highs, lows, prices, ADX_PERIOD)
        if adx is None or adx < ADX_MIN:
            return None, 0.0

        # ---------- WEIGHTED VOTING ----------
        call_score = 0.0
        put_score = 0.0

        ema9 = self._ema(prices, EMA_SHORT)
        ema21 = self._ema(prices, EMA_MID)
        if ema9 and ema21:
            if ema9 > ema21:
                call_score += 1.5
            else:
                put_score += 1.5

        ema50 = self._ema(prices, EMA_LONG)
        if ema50:
            if price > ema50:
                call_score += 1.0
            else:
                put_score += 1.0

        rsi = self._rsi(prices, RSI_PERIOD)
        if rsi is not None:
            if 55 <= rsi < 72:
                call_score += 1.5
            elif 28 < rsi <= 45:
                put_score += 1.5

        ema12 = self._ema(prices, 12)
        ema26 = self._ema(prices, 26)
        if ema12 and ema26:
            prev = prices[:-3]
            e12p = self._ema(prev, 12)
            e26p = self._ema(prev, 26)
            if e12p and e26p:
                macd_now = ema12 - ema26
                macd_prev = e12p - e26p
                if macd_now > macd_prev:
                    call_score += 1.0
                else:
                    put_score += 1.0

        bb_low, bb_mid, bb_high = self._bollinger(prices, BB_PERIOD, BB_STD)
        if bb_low is not None and bb_high > bb_low:
            pos = (price - bb_low) / (bb_high - bb_low)
            if 0.5 < pos < 0.85:
                call_score += 1.0
            elif 0.15 < pos < 0.5:
                put_score += 1.0

        pattern = self._candle_pattern(opens, highs, lows, prices)
        if pattern == "BULL":
            call_score += 1.5
        elif pattern == "BEAR":
            put_score += 1.5

        divergence = self._rsi_divergence(prices)
        if divergence == "BULL":
            call_score += 1.0
        elif divergence == "BEAR":
            put_score += 1.0

        m15 = self.m5_trend.get(pair)
        if m15 == "BULLISH":
            call_score += 1.5
        elif m15 == "BEARISH":
            put_score += 1.5

        max_score = max(call_score, put_score)
        if max_score < WEIGHTED_SCORE_THRESHOLD:
            return None, 0.0

        if call_score > put_score:
            confidence = min(100.0, (call_score / 10.0) * 100)
            return "CALL", confidence
        elif put_score > call_score:
            confidence = min(100.0, (put_score / 10.0) * 100)
            return "PUT", confidence

        return None, 0.0