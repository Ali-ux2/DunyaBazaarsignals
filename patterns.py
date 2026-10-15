"""Candle-pattern family P (from the user's pattern sheets). Every detector reads only bars <= i and returns
(direction, description) for the NEXT candle, or None. No martingale; 'sure shot' claims are ignored.
V depth for P15 ≥ 1 ATR (otherwise every wiggle is a 'V').
Definitions (relative to the median body of the 20 bars ending at i-1):
  normal body  0.4–1.5× median     long wick  ≥ body or ≥ 50% of range     small/doji  body ≤ 0.3× median
  marubozu     body ≥ 85% of range and ≥ 1.8× median     'not break'  not exceeding the reference candle's high/low
  SNR          a recent swing (3-bar fractal) high/low in the last 40 bars within 0.3 ATR."""
import numpy as np, pandas as pd
G, R = 1, -1
NAMES = {
    "P1": "8 same colour → 9th same", "P2": "2G2R rejecting resistance → R", "P3": "G,R,G long lower wick → R",
    "P4": "R tail > next G head → G", "P5": "2R long tails, 2nd inside 1st, then G → R", "P6": "R,G,R → R",
    "P6b": "R,G,R,G → G", "P7": "R + 2 normal G → R", "P8": "G + 2 normal R → G", "P9": "3G + R long tail → G",
    "P10": "3R + G long head → R", "P11": "3R + inside G → G (30s bars)", "P13": "repeated retests then breakout → reversal (2m)",
    "P14": "trendline from first 2G/2R broken → follow", "P15": "V / inverted V breaking the horizontal → opposite",
    "P16": "3-4 normal G + R → G", "P17": "3-4 normal R + G → R", "P18": "R marubozu, 3 normal G, R inside → R",
    "P19": "G marubozu, 3 normal R, G inside → G", "P20": "2 normal R, G, R not breaking G low → R",
    "P21": "2 normal G, R, G not breaking R high → G", "P22": "3-5 G + smaller R not breaking → G",
    "P23": "3-5 R + smaller G not breaking → R", "P24": "sideways at SNR: G,R,doji R,G,smaller R → G",
    "P25": "doji R, normal R, long G breaking SNR → G", "P26": "3 descending G + 2 small R near SNR → G"}
# how each pattern counts for double confirmation: 'S' = its own shape is a STRUCTURAL element (level / wick / range),
# 'M' = its own sequence is a MOMENTUM element (streak / impulse). The other category must come from double_confirm().
DC_ROLE = {"P1": "M", "P2": "S", "P3": "S", "P4": "S", "P5": "S", "P6": "M", "P6b": "M", "P7": "M", "P8": "M", "P9": "S", "P10": "S",
           "P11": "S", "P13": "S", "P14": "S", "P15": "S", "P16": "M", "P17": "M", "P18": "S", "P19": "S", "P20": "S", "P21": "S",
           "P22": "M", "P23": "M", "P24": "S", "P25": "S", "P26": "S"}
EXPIRY = {"P13": 2}

class Bars:
    def __init__(self, o, h, l, c, atr=None):
        self.o, self.h, self.l, self.c = (np.asarray(a, float) for a in (o, h, l, c))
        self.body = np.abs(self.c - self.o); self.rng = np.maximum(self.h - self.l, 1e-12)
        self.col = np.sign(self.c - self.o).astype(int)
        self.uw = self.h - np.maximum(self.o, self.c); self.lw = np.minimum(self.o, self.c) - self.l
        self.atr = np.asarray(atr, float) if atr is not None else pd.Series(self.rng).rolling(14, min_periods=1).mean().values
    def med(self, i): return float(np.median(self.body[max(0, i - 20):i])) if i > 0 else float(self.body[0]) or 1e-12
    def normal(self, j, i): m = self.med(i); return 0.4 * m <= self.body[j] <= 1.5 * m
    def small(self, j, i): return self.body[j] <= 0.3 * self.med(i)
    def maru(self, j, i): return self.body[j] >= 0.85 * self.rng[j] and self.body[j] >= 1.8 * self.med(i)
    def lwick(self, j): return self.lw[j] >= self.body[j] or self.lw[j] >= 0.5 * self.rng[j]
    def uwick(self, j): return self.uw[j] >= self.body[j] or self.uw[j] >= 0.5 * self.rng[j]
    def cols(self, a, b): return list(self.col[a:b + 1])
    def levels(self, i, look=40):
        hi, lo = [], []
        for j in range(max(1, i - look), i - 2):
            if self.h[j] >= self.h[j - 1] and self.h[j] >= self.h[j + 1]: hi.append(self.h[j])
            if self.l[j] <= self.l[j - 1] and self.l[j] <= self.l[j + 1]: lo.append(self.l[j])
        return hi, lo
    def near(self, price, i, kind="any"):
        hi, lo = self.levels(i); lv = hi + lo if kind == "any" else (hi if kind == "res" else lo)
        return any(abs(price - v) <= 0.3 * self.atr[i] for v in lv)

def _seq(b, i, want):
    n = len(want)
    return i - n + 1 >= 0 and b.cols(i - n + 1, i) == list(want)

def detect(b, i):
    """All P patterns (1m logic) that complete on bar i: [(key, dir, desc)]."""
    out = []; add = lambda k, d: out.append((k, d, NAMES[k]))
    if i < 25: return out
    c = b.col
    if all(c[i - 7:i + 1] == G): add("P1", "CALL")
    elif all(c[i - 7:i + 1] == R): add("P1", "PUT")
    if _seq(b, i, (G, G, R, R)) and b.near(max(b.h[i - 3:i + 1]), i, "res"): add("P2", "PUT")
    if _seq(b, i, (G, R, G)) and b.lwick(i): add("P3", "PUT")
    if _seq(b, i, (R, G)) and b.lw[i - 1] > b.uw[i]: add("P4", "CALL")
    if _seq(b, i, (R, R, G)) and b.lwick(i - 2) and b.lwick(i - 1) and b.h[i - 1] <= b.h[i - 2] and b.l[i - 1] >= b.l[i - 2]: add("P5", "PUT")
    if _seq(b, i, (R, G, R)): add("P6", "PUT")
    if _seq(b, i, (R, G, R, G)): add("P6b", "CALL")
    if _seq(b, i, (R, G, G)) and b.normal(i - 1, i) and b.normal(i, i): add("P7", "PUT")
    if _seq(b, i, (G, R, R)) and b.normal(i - 1, i) and b.normal(i, i): add("P8", "CALL")
    if _seq(b, i, (G, G, G, R)) and b.lwick(i): add("P9", "CALL")
    if _seq(b, i, (R, R, R, G)) and b.uwick(i): add("P10", "PUT")
    # P14: trendline through the lows of the first 2 G after an R, broken by an R close (mirror for resistance)
    for j in range(i - 10, i - 3):
        if c[j] == R and c[j + 1] == G and c[j + 2] == G and b.l[j + 2] >= b.l[j + 1]:
            sl = b.l[j + 2] - b.l[j + 1]; line = lambda k: b.l[j + 1] + sl * (k - j - 1)
            if all(b.c[k] >= line(k) for k in range(j + 3, i)) and c[i] == R and b.c[i] < line(i): add("P14", "PUT"); break
        if c[j] == G and c[j + 1] == R and c[j + 2] == R and b.h[j + 2] <= b.h[j + 1]:
            sl = b.h[j + 2] - b.h[j + 1]; line = lambda k: b.h[j + 1] + sl * (k - j - 1)
            if all(b.c[k] <= line(k) for k in range(j + 3, i)) and c[i] == G and b.c[i] > line(i): add("P14", "CALL"); break
    # P15: V — down leg into a low (2-4 bars back), up leg closing above the leg's starting horizontal → PUT; inverted V → CALL
    for m in range(i - 4, i - 1):
        a = m - 3
        dep = b.atr[i]
        if b.l[m] == b.l[a:i + 1].min() and max(b.o[a], b.c[a]) - b.l[m] >= dep and sum(c[a:m + 1] == R) >= 2 and sum(c[m + 1:i + 1] == G) >= 2 and b.c[i] > max(b.o[a], b.c[a]):
            add("P15", "PUT"); break
        if b.h[m] == b.h[a:i + 1].max() and b.h[m] - min(b.o[a], b.c[a]) >= dep and sum(c[a:m + 1] == G) >= 2 and sum(c[m + 1:i + 1] == R) >= 2 and b.c[i] < min(b.o[a], b.c[a]):
            add("P15", "CALL"); break
    for k_ in (3, 4):
        if c[i] == R and all(c[i - k_:i] == G) and all(b.normal(j, i) for j in range(i - k_, i)) and c[i - k_ - 1] != G: add("P16", "CALL")
        if c[i] == G and all(c[i - k_:i] == R) and all(b.normal(j, i) for j in range(i - k_, i)) and c[i - k_ - 1] != R: add("P17", "PUT")
    j = i - 4
    if c[j] == R and b.maru(j, i) and all(c[j + 1:i] == G) and all(b.normal(k, i) for k in range(j + 1, i)) and c[i] == R \
            and b.h[i] <= b.h[j] and b.l[i] >= b.l[j]: add("P18", "PUT")
    if c[j] == G and b.maru(j, i) and all(c[j + 1:i] == R) and all(b.normal(k, i) for k in range(j + 1, i)) and c[i] == G \
            and b.h[i] <= b.h[j] and b.l[i] >= b.l[j]: add("P19", "CALL")
    if _seq(b, i, (R, R, G, R)) and b.normal(i - 3, i) and b.normal(i - 2, i) and b.l[i] >= b.l[i - 1]: add("P20", "PUT")
    if _seq(b, i, (G, G, R, G)) and b.normal(i - 3, i) and b.normal(i - 2, i) and b.h[i] <= b.h[i - 1]: add("P21", "CALL")
    for k_ in (3, 4, 5):
        if c[i] == R and all(c[i - k_:i] == G) and c[i - k_ - 1] != G and b.body[i] < b.body[i - 1] and b.c[i] >= b.o[i - 1]: add("P22", "CALL")
        if c[i] == G and all(c[i - k_:i] == R) and c[i - k_ - 1] != R and b.body[i] < b.body[i - 1] and b.c[i] <= b.o[i - 1]: add("P23", "PUT")
    if _seq(b, i, (G, R, R, G, R)) and b.small(i - 2, i) and b.body[i] < b.body[i - 3] and b.near(b.c[i], i) \
            and (b.h[i - 4:i + 1].max() - b.l[i - 4:i + 1].min()) <= 2.0 * b.atr[i]: add("P24", "CALL")
    if _seq(b, i, (R, R, G)) and b.small(i - 2, i) and b.normal(i - 1, i) and b.body[i] >= 1.5 * b.med(i):
        hi, _ = b.levels(i)
        if any(b.c[i - 1] < v < b.c[i] for v in hi): add("P25", "CALL")
    if _seq(b, i, (G, G, G, R, R)) and b.h[i - 3] < b.h[i - 4] and b.h[i - 2] < b.h[i - 3] and b.small(i - 1, i) and b.small(i, i) \
            and min(b.l[i - 1], b.l[i]) >= b.l[i - 2] and b.near(b.l[i], i): add("P26", "CALL")
    return out

def detect_p11(b, i):
    """P11 on 30-second bars (stream pairs only; built from ticks)."""
    if i >= 4 and _seq(b, i, (R, R, R, G)) and b.h[i] <= b.h[i - 1] and b.l[i] >= b.l[i - 1]: return [("P11", "CALL", NAMES["P11"])]
    return []

def resample2(x):
    """2-minute bars from 1-minute bars (labels = 2m bar open)."""
    return x[["open", "high", "low", "close"]].resample("2min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()

def detect_p13(b, i, touches=3):
    """Level = extreme of the 20 bars before i, touched (within 0.3 ATR) ≥ `touches` times; bar i closes beyond it → reversal."""
    if i < 22: return []
    a = b.atr[i]; hs = b.h[i - 20:i]; ls = b.l[i - 20:i]; res, sup = hs.max(), ls.min()
    if sum(hs >= res - 0.3 * a) >= touches and b.c[i] > res: return [("P13", "PUT", NAMES["P13"])]
    if sum(ls <= sup + 0.3 * a) >= touches and b.c[i] < sup: return [("P13", "CALL", NAMES["P13"])]
    return []

def bars_from(x): return Bars(x.open.values, x.high.values, x.low.values, x.close.values, x.atr.values if "atr" in x else None)

def p13_on_1m(x, i):
    """P13 evaluated when bar i closes a 2-minute bar (minute odd). Uses only bars <= i."""
    t = x.index[i]
    if t.minute % 2 != 1: return []
    x2 = resample2(x.iloc[max(0, i - 80):i + 1])
    if len(x2) < 23: return []
    return detect_p13(Bars(x2.open, x2.high, x2.low, x2.close), len(x2) - 1)

_CACHE = {}
def detect_cached(x, i):
    """detect() shared across the per-key history detectors (one Bars build and one detect per bar)."""
    k = (id(x), len(x), x.index[-1])
    if _CACHE.get("k") != k: _CACHE.clear(); _CACHE["k"] = k; _CACHE["b"] = bars_from(x)
    if i not in _CACHE: _CACHE[i] = detect(_CACHE["b"], i)
    return _CACHE[i]
def detector(key):
    """History detector (x, i) -> 'CALL'|'PUT'|None for one pattern key (1m bars; P13 via 2m aggregation, horizon 2)."""
    def det(x, i):
        hits = p13_on_1m(x, i) if key == "P13" else detect_cached(x, i)
        for k, d, _ in hits:
            if k == key: return d
        return None
    det.horizon = EXPIRY.get(key, 1)
    return det

def p_double_confirm(x, i, key, side):
    """Pattern's own shape counts as one element (DC_ROLE); the other must come from double_confirm().
    Returns (ok, text)."""
    from bugstrats import double_confirm
    m, st, txt = double_confirm(x, i, side)
    role = DC_ROLE.get(key, "S")
    ok = (m or role == "M") and (st or role == "S")
    return ok, f"{key} gives {'structure' if role == 'S' else 'momentum'} · {txt}"
