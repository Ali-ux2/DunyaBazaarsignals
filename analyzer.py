"""
Vote-based confluence analyzer — M1 OTC, non-MTG compounding.
Votes can be +1, -1, or 0. Signal only when |score| >= MIN_SCORE.
With MIN_SCORE = 6, every vote must agree and pass quality checks.
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
    "ema_trend": "EMA 9/21 trend + slope",
    "ema_short": "EMA 5/20 short trend",
    "macd":      "MACD histogram",
    "rsi":       "RSI in clean zone",
    "stoch":     "Stoch K vs D, not extreme",
    "candle":    "Clean body candle",
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
        last = votes.iloc[-1]

        if bool(last.get("chop", False)):
            return None, 0.0

        score = int(last["score"])
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
            "chop": bool(v.get("chop", False)),
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

        ema_fast = self._ema(c, EMA_TREND_FAST)
        ema_slow = self._ema(c, EMA_TREND_SLOW)
        ema_short_f = self._ema(c, EMA_SHORT_FAST)
        ema_short_s = self._ema(c, EMA_SHORT_SLOW)

        slope = ema_fast.diff()
        call_trend = (ema_fast > ema_slow) & (slope > 0)
        put_trend  = (ema_fast < ema_slow) & (slope < 0)
        v["ema_trend"] = np.where(call_trend, 1, np.where(put_trend, -1, 0))

        v["ema_short"] = np.where(
            ema_short_f > ema_short_s, 1,
            np.where(ema_short_f < ema_short_s, -1, 0)
        )

        macd_line = self._ema(c, MACD_FAST) - self._ema(c, MACD_SLOW)
        hist = macd_line - self._ema(macd_line, MACD_SIGNAL)
        v["macd"] = np.where(hist > 0, 1, np.where(hist < 0, -1, 0))

        r = self._rsi(c, RSI_PERIOD)
        v["rsi"] = np.where(
            (r >= 55) & (r <= 70), 1,
            np.where((r >= 30) & (r <= 45), -1, 0)
        )

        hh = h.rolling(STOCH_PERIOD).max()
        ll = l.rolling(STOCH_PERIOD).min()
        k = (100 * (c - ll) / (hh - ll).replace(0, np.nan)).rolling(STOCH_SMOOTH_K).mean()
        d = k.rolling(STOCH_SMOOTH_D).mean()
        stoch_call = (k > d) & (k < 80)
        stoch_put  = (k < d) & (k > 20)
        v["stoch"] = np.where(stoch_call, 1, np.where(stoch_put, -1, 0))

        rng = (h - l).replace(0, np.nan)
        body = (c - o).abs()
        body_ok = body / rng >= 0.60
        upper_wick = h - np.maximum(c, o)
        lower_wick = np.minimum(c, o) - l
        call_candle = (c > o) & body_ok & (upper_wick / rng < 0.30)
        put_candle  = (c < o) & body_ok & (lower_wick / rng < 0.30)
        v["candle"] = np.where(call_candle, 1, np.where(put_candle, -1, 0))

        band = (ema_slow * 0.0008).replace(0, np.nan)
        near = (c - ema_slow).abs() < band
        chop = near.rolling(5).sum() >= 4
        v["chop"] = chop.fillna(False)

        touch = (l <= ema_fast * 1.0005) & (h >= ema_fast * 0.9995)
        recent_touch = touch.rolling(3).max().fillna(0).astype(bool)
        away_call = c > ema_fast
        away_put  = c < ema_fast
        v.loc[~recent_touch | ~away_call, "ema_trend"] = v.loc[
            ~recent_touch | ~away_call, "ema_trend"
        ].where(v.loc[~recent_touch | ~away_call, "ema_trend"] <= 0, 0)
        v.loc[~recent_touch | ~away_put, "ema_trend"] = v.loc[
            ~recent_touch | ~away_put, "ema_trend"
        ].where(v.loc[~recent_touch | ~away_put, "ema_trend"] >= 0, 0)

        v["score"] = v[VOTE_COLS].sum(axis=1)
        return v