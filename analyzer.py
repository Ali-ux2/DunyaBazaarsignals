import numpy as np
import pandas as pd
from collections import deque
from config import (
    PAIRS, MIN_SCORE,
    EMA_TREND_FAST, EMA_TREND_SLOW,
    EMA_SHORT_FAST, EMA_SHORT_SLOW,
    RSI_PERIOD,
    STOCH_PERIOD, STOCH_SMOOTH_K, STOCH_SMOOTH_D,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    CANDLES_TO_TRACK, CANDLES_MIN_REQUIRED,
)

VOTE_COLS = ["ema_trend", "ema_short", "macd", "rsi", "stoch", "candle", "sr"]
TOTAL_VOTES = 7

VOTE_LABELS = {
    "ema_trend": "EMA 9/21 trend + slope + bounce",
    "ema_short": "EMA 5/20 short trend",
    "macd":      "MACD histogram",
    "rsi":       "RSI in clean zone",
    "stoch":     "Stoch K vs D, not extreme",
    "candle":    "Clean body candle",
    "sr":        "Support/Resistance confluence",
}

# S/R vote tuning
SR_WINDOW = 20          # lookback for support/resistance
SR_THRESHOLD = 0.20     # price must be within 20% of range from the level


class S3Analyzer:
    def __init__(self, pairs=None):
        if pairs is None:
            pairs = PAIRS
        self.pairs = pairs
        self.opens  = {p: deque(maxlen=CANDLES_TO_TRACK) for p in pairs}
        self.highs  = {p: deque(maxlen=CANDLES_TO_TRACK) for p in pairs}
        self.lows   = {p: deque(maxlen=CANDLES_TO_TRACK) for p in pairs}
        self.closes = {p: deque(maxlen=CANDLES_TO_TRACK) for p in pairs}
        self.last_signal = {p: None for p in pairs}

    # ------------------------------------------------------------------
    def analyze(self, pair, price, high=None, low=None, open_=None):
        if pair not in self.closes:
            return None, 0.0
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

        df = self._make_df(pair)
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
        if pair not in self.closes:
            return None
        if len(self.closes[pair]) < CANDLES_MIN_REQUIRED:
            return None
        df = self._make_df(pair)
        v = self._build_votes(df).iloc[-1]
        if bool(v.get("chop", False)):
            return None

        # S/R display values (for the signal card)
        recent_highs = list(self.highs[pair])[-SR_WINDOW:]
        recent_lows  = list(self.lows[pair])[-SR_WINDOW:]
        resistance = max(recent_highs) if recent_highs else 0.0
        support    = min(recent_lows)  if recent_lows  else 0.0

        return {
            "score": int(v["score"]),
            "chop": False,
            "support": support,
            "resistance": resistance,
            "sr_vote": int(v["sr"]),
            "call_reasons": [VOTE_LABELS[c] for c in VOTE_COLS if int(v[c]) > 0],
            "put_reasons":  [VOTE_LABELS[c] for c in VOTE_COLS if int(v[c]) < 0],
        }

    # ------------------------------------------------------------------
    def _make_df(self, pair):
        return pd.DataFrame({
            "open":  list(self.opens[pair]),
            "high":  list(self.highs[pair]),
            "low":   list(self.lows[pair]),
            "close": list(self.closes[pair]),
        })

    @staticmethod
    def _ema(s, n):
        return s.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _rsi(s, n=7):
        d = s.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        return 100 - 100 / (1 + up / dn.replace(0, np.nan))

    # ------------------------------------------------------------------
    def _build_votes(self, df):
        c, o, h, l = df["close"], df["open"], df["high"], df["low"]
        v = pd.DataFrame(index=df.index)

        ema_fast = self._ema(c, EMA_TREND_FAST)
        ema_slow = self._ema(c, EMA_TREND_SLOW)
        ema_sf   = self._ema(c, EMA_SHORT_FAST)
        ema_ss   = self._ema(c, EMA_SHORT_SLOW)

        # ---------------- Vote 1: EMA trend + slope + bounce ----------------
        slope = ema_fast.diff()
        call_base = (ema_fast > ema_slow) & (slope > 0)
        put_base  = (ema_fast < ema_slow) & (slope < 0)

        touch = (l <= ema_fast * 1.0005) & (h >= ema_fast * 0.9995)
        recent_touch = touch.rolling(3).max().fillna(0).astype(bool)

        call_valid = call_base & recent_touch & (c > ema_fast)
        put_valid  = put_base  & recent_touch & (c < ema_fast)
        v["ema_trend"] = np.where(call_valid, 1, np.where(put_valid, -1, 0))

        # ---------------- Vote 2: short EMA trend ----------------
        v["ema_short"] = np.where(
            ema_sf > ema_ss, 1, np.where(ema_sf < ema_ss, -1, 0)
        )

        # ---------------- Vote 3: MACD histogram ----------------
        macd_line = self._ema(c, MACD_FAST) - self._ema(c, MACD_SLOW)
        hist = macd_line - self._ema(macd_line, MACD_SIGNAL)
        v["macd"] = np.where(hist > 0, 1, np.where(hist < 0, -1, 0))

        # ---------------- Vote 4: RSI in clean zone ----------------
        r = self._rsi(c, RSI_PERIOD)
        v["rsi"] = np.where(
            (r >= 55) & (r <= 70), 1,
            np.where((r >= 30) & (r <= 45), -1, 0)
        )

        # ---------------- Vote 5: Stoch K vs D ----------------
        hh = h.rolling(STOCH_PERIOD).max()
        ll = l.rolling(STOCH_PERIOD).min()
        k = (100 * (c - ll) / (hh - ll).replace(0, np.nan)).rolling(STOCH_SMOOTH_K).mean()
        d = k.rolling(STOCH_SMOOTH_D).mean()
        stoch_call = (k > d) & (k < 80)
        stoch_put  = (k < d) & (k > 20)
        v["stoch"] = np.where(stoch_call, 1, np.where(stoch_put, -1, 0))

        # ---------------- Vote 6: Clean body candle ----------------
        rng = (h - l).replace(0, np.nan)
        body = (c - o).abs()
        body_ok = body / rng >= 0.60
        upper_wick = h - np.maximum(c, o)
        lower_wick = np.minimum(c, o) - l
        call_candle = (c > o) & body_ok & (upper_wick / rng < 0.30)
        put_candle  = (c < o) & body_ok & (lower_wick / rng < 0.30)
        v["candle"] = np.where(call_candle, 1, np.where(put_candle, -1, 0))

        # ---------------- Vote 7: Support/Resistance confluence ----------------
        recent_h = h.rolling(SR_WINDOW).max()
        recent_l = l.rolling(SR_WINDOW).min()
        sr_range = (recent_h - recent_l).replace(0, np.nan)

        # Distance from close to each level, as a fraction of the range
        dist_from_support    = (c - recent_l) / sr_range
        dist_from_resistance = (recent_h - c) / sr_range

        near_support    = dist_from_support    < SR_THRESHOLD
        near_resistance = dist_from_resistance < SR_THRESHOLD

        # If price is near BOTH (very tight range), vote neutral — no edge
        ambiguous = near_support & near_resistance

        sr_call = near_support    & ~ambiguous   # at support → expect bounce
        sr_put  = near_resistance & ~ambiguous   # at resistance → expect rejection

        v["sr"] = np.where(sr_call, 1, np.where(sr_put, -1, 0))

        # ---------------- Chop filter ----------------
        band = (ema_slow * 0.0008).replace(0, np.nan)
        near = (c - ema_slow).abs() < band
        chop = near.rolling(5).sum() >= 4
        v["chop"] = chop.fillna(False)

        v["score"] = v[VOTE_COLS].sum(axis=1)
        return v