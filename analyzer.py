"""
Vote-based confluence analyzer — always-fire 6 votes, multi-pair.
Score = sum of votes. Signal when |score| >= MIN_SCORE.
"""

import numpy as np
import pandas as pd
from collections import deque
from config import (
    PAIRS, MIN_SCORE,
    EMA_TREND_FAST, EMA_TREND_SLOW,
    EMA_SHORT_FAST, EMA_SHORT_SLOW,
    RSI_PERIOD, BB_PERIOD, BB_STD,
    STOCH_PERIOD, STOCH_SMOOTH_K, STOCH_SMOOTH_D,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    CANDLES_TO_TRACK, CANDLES_MIN_REQUIRED,
)

VOTE_COLS = ["ema_trend", "ema_short", "macd", "rsi", "stoch", "candle"]

VOTE_LABELS = {
    "ema_trend": "EMA 9/21 trend",
    "ema_short": "EMA 5/20 short trend",
    "macd":      "MACD histogram",
    "rsi":       "RSI vs 50",
    "stoch":     "Stoch K vs D",
    "candle":    "Last candle direction",
}

TOTAL_VOTES = 6


class S3Analyzer:
    def __init__(self):
        self.pairs = PAIRS
        self.opens  = {p: deque(maxlen=CANDLES_TO_TRACK) for p in PAIRS}
        self.highs  = {p: deque(maxlen=CANDLES_TO_TRACK) for p in PAIRS}
        self.lows   = {p: deque(maxlen=CANDLES_TO_TRACK) for p in PAIRS}
        self.closes = {p: deque(maxlen=CANDLES_TO_TRACK) for p in PAIRS}
        self.last_signal = {p: None for p in PAIRS}

    def analyze(self, pair, price, high=None, low=None, open_=None):
        if high is None: high = price
        if low is None:  low = price
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
        confidence = min(abs(score) / TOTAL_VOTES, 1.0) * 100.0
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
        v = self._build_votes(df).iloc[-1]
        return {
            "score": int(v["score"]),
            "call_reasons": [VOTE_LABELS[c] for c in VOTE_COLS if int(v[c]) > 0],
            "put_reasons":  [VOTE_LABELS[c] for c in VOTE_COLS if int(v[c]) < 0],
        }

    @staticmethod
    def _ema(s, n):
        return s.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _rsi(s, n=7):
        d = s.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        return 100 - 100 / (1 + up / dn.replace(0, np.nan))

    def _build_votes(self, df):
        c, o, h, l = df["close"], df["open"], df["high"], df["low"]
        v = pd.DataFrame(index=df.index)

        v["ema_trend"] = np.where(
            self._ema(c, EMA_TREND_FAST) > self._ema(c, EMA_TREND_SLOW), 1, -1
        )
        v["ema_short"] = np.where(
            self._ema(c, EMA_SHORT_FAST) > self._ema(c, EMA_SHORT_SLOW), 1, -1
        )

        macd_line = self._ema(c, MACD_FAST) - self._ema(c, MACD_SLOW)
        hist = macd_line - self._ema(macd_line, MACD_SIGNAL)
        v["macd"] = np.where(hist > 0, 1, -1)

        r = self._rsi(c, RSI_PERIOD)
        v["rsi"] = np.where(r > 50, 1, -1)

        hh = h.rolling(STOCH_PERIOD).max()
        ll = l.rolling(STOCH_PERIOD).min()
        k = (100 * (c - ll) / (hh - ll).replace(0, np.nan)).rolling(STOCH_SMOOTH_K).mean()
        d = k.rolling(STOCH_SMOOTH_D).mean()
        v["stoch"] = np.where(k > d, 1, -1)

        v["candle"] = np.where(c > o, 1, -1)

        v["score"] = v[VOTE_COLS].sum(axis=1)
        return v