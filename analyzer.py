"""
Vote-based confluence analyzer for DunyaBazaar bot (M1 only).
7 indicators vote +1 / -1 / 0. Score = sum. Signal when |score| >= MIN_SCORE.

Supports both modes:
  - No filter:     USE_M15_GATE = False  (bot.py doesn't call set_m5_trend)
  - M5 filter:     USE_M15_GATE = True   (bot.py refreshes M5 trend)

Interface:
    analyzer.analyze(pair, price, high, low, open_) -> (direction, confidence)
    analyzer.set_m5_trend(pair, 'BULLISH'|'BEARISH')   [optional]
    analyzer.last_signal[pair]
"""

import numpy as np
import pandas as pd
from collections import deque
from config import (
    PAIRS, MIN_SCORE, USE_M15_GATE,
    EMA_TREND_FAST, EMA_TREND_SLOW,
    EMA_CROSS_FAST, EMA_CROSS_SLOW,
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    BB_PERIOD, BB_STD,
    STOCH_PERIOD, STOCH_SMOOTH_K, STOCH_SMOOTH_D,
    STOCH_LOW, STOCH_HIGH,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    CANDLES_TO_TRACK, CANDLES_MIN_REQUIRED,
)

VOTE_COLS = ["ema_trend", "ema_cross", "rsi", "bollinger", "stoch", "macd", "candle"]

VOTE_LABELS = {
    "ema_trend": "EMA trend",
    "ema_cross": "EMA cross",
    "rsi":       "RSI extreme",
    "bollinger": "Bollinger touch",
    "stoch":     "Stoch cross",
    "macd":      "MACD zero-cross",
    "candle":    "Candle pattern",
}


class S3Analyzer:
    def __init__(self):
        self.opens  = {p: deque(maxlen=CANDLES_TO_TRACK) for p in PAIRS}
        self.highs  = {p: deque(maxlen=CANDLES_TO_TRACK) for p in PAIRS}
        self.lows   = {p: deque(maxlen=CANDLES_TO_TRACK) for p in PAIRS}
        self.closes = {p: deque(maxlen=CANDLES_TO_TRACK) for p in PAIRS}
        self.m5_trend = {p: None for p in PAIRS}
        self.last_signal = {p: None for p in PAIRS}

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------
    def set_m5_trend(self, pair, trend):
        """Optional — only used when USE_M15_GATE is True."""
        self.m5_trend[pair] = trend

    def analyze(self, pair, price, high=None, low=None, open_=None):
        if high is None:
            high = price
        if low is None:
            low = price
        if open_ is None:
            open_ = self.closes[pair][-1] if self.closes[pair] else price

        self.opens[pair].append(open_)
        self.highs[pair].append(high)
        self.lows[pair].append(low)
        self.closes[pair].append(price)

        if len(self.closes[pair]) < CANDLES_MIN_REQUIRED:
            return None, 0.0

        df = pd.DataFrame({
            "open":  list(self.opens[pair]),
            "high":  list(self.highs[pair]),
            "low":   list(self.lows[pair]),
            "close": list(self.closes[pair]),
        })

        votes = self._build_votes(df)
        score = int(votes.iloc[-1]["score"])

        if abs(score) < MIN_SCORE:
            return None, 0.0

        direction = "CALL" if score > 0 else "PUT"

        # Optional higher-timeframe gate
        if USE_M15_GATE:
            trend = self.m5_trend.get(pair)
            if trend == "BULLISH" and direction == "PUT":
                return None, 0.0
            if trend == "BEARISH" and direction == "CALL":
                return None, 0.0

        confidence = min(abs(score) / len(VOTE_COLS), 1.0) * 100.0
        return direction, confidence

    def latest_explanation(self, pair):
        if len(self.closes[pair]) < CANDLES_MIN_REQUIRED:
            return None
        df = pd.DataFrame({
            "open":  list(self.opens[pair]),
            "high":  list(self.highs[pair]),
            "low":   list(self.lows[pair]),
            "close": list(self.closes[pair]),
        })
        votes = self._build_votes(df)
        last = votes.iloc[-1]
        return {
            "score": int(last["score"]),
            "call_reasons": [VOTE_LABELS[c] for c in VOTE_COLS if int(last[c]) > 0],
            "put_reasons":  [VOTE_LABELS[c] for c in VOTE_COLS if int(last[c]) < 0],
        }

    # ------------------------------------------------------------
    # Indicator primitives
    # ------------------------------------------------------------
    @staticmethod
    def _ema(s, n):
        return s.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _rsi(s, n=7):
        d = s.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        return 100 - 100 / (1 + up / dn.replace(0, np.nan))

    # ------------------------------------------------------------
    # Vote engine
    # ------------------------------------------------------------
    def _build_votes(self, df):
        c, o, h, l = df["close"], df["open"], df["high"], df["low"]
        v = pd.DataFrame(index=df.index)

        v["ema_trend"] = np.where(
            self._ema(c, EMA_TREND_FAST) > self._ema(c, EMA_TREND_SLOW), 1, -1
        )

        e_fast = self._ema(c, EMA_CROSS_FAST)
        e_slow = self._ema(c, EMA_CROSS_SLOW)
        cross_up = (e_fast > e_slow) & (e_fast.shift() <= e_slow.shift())
        cross_dn = (e_fast < e_slow) & (e_fast.shift() >= e_slow.shift())
        v["ema_cross"] = np.select([cross_up, cross_dn], [1, -1], 0)

        r = self._rsi(c, RSI_PERIOD)
        v["rsi"] = np.select([r < RSI_OVERSOLD, r > RSI_OVERBOUGHT], [1, -1], 0)

        ma = c.rolling(BB_PERIOD).mean()
        sd = c.rolling(BB_PERIOD).std()
        v["bollinger"] = np.select(
            [c < ma - BB_STD * sd, c > ma + BB_STD * sd], [1, -1], 0
        )

        hh = h.rolling(STOCH_PERIOD).max()
        ll = l.rolling(STOCH_PERIOD).min()
        k = (100 * (c - ll) / (hh - ll).replace(0, np.nan)).rolling(STOCH_SMOOTH_K).mean()
        d = k.rolling(STOCH_SMOOTH_D).mean()
        stoch_up = (k > d) & (k.shift() <= d.shift())
        stoch_dn = (k < d) & (k.shift() >= d.shift())
        v["stoch"] = np.select(
            [stoch_up & (k < STOCH_LOW), stoch_dn & (k > STOCH_HIGH)], [1, -1], 0
        )

        macd_line = self._ema(c, MACD_FAST) - self._ema(c, MACD_SLOW)
        hist = macd_line - self._ema(macd_line, MACD_SIGNAL)
        v["macd"] = np.select(
            [(hist > 0) & (hist.shift() <= 0), (hist < 0) & (hist.shift() >= 0)],
            [1, -1], 0
        )

        body = (c - o).abs()
        up_w = h - np.maximum(c, o)
        lo_w = np.minimum(c, o) - l
        bull_eng = (c.shift() < o.shift()) & (c > o) & (c > o.shift()) & (o < c.shift())
        bear_eng = (c.shift() > o.shift()) & (c < o) & (c < o.shift()) & (o > c.shift())
        hammer = (lo_w > 2 * body) & (up_w < body)
        star = (up_w > 2 * body) & (lo_w < body)
        v["candle"] = np.select([bull_eng | hammer, bear_eng | star], [1, -1], 0)

        v["score"] = v[VOTE_COLS].sum(axis=1)
        return v