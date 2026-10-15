"""Strategies E/F (ensemble members) and bug strategies B4-B6, plus the leaderboard score.
Every function uses bars <= i only (closed candles) or ticks <= now: no repaint, no lookahead.
variant: "improved" (default, C.BUG_STRAT_VARIANT) or "original"."""
from __future__ import annotations
import math
import numpy as np, pandas as pd
import config as C
from analyzer import rsi, demarker, bars5

def _v(v): return v or C.BUG_STRAT_VARIANT
def cci(x, n):
    tp = (x.high + x.low + x.close) / 3; sma = tp.rolling(n).mean()
    md = tp.rolling(n).apply(lambda a: np.mean(np.abs(a - a.mean())), raw=True)
    return ((tp - sma) / (0.015 * md.replace(0, np.nan))).fillna(0)
def ensure_cols(x):
    """Causal indicator columns shared by B3/E/F (rolling/EMA only, so value at i depends on bars <= i)."""
    if "dem" not in x: x["dem"] = demarker(x, C.E_DEM_N)
    if "rsi14" not in x: x["rsi14"] = rsi(x.close, 14)
    if "rsi12" not in x: x["rsi12"] = rsi(x.close, C.F_RSI_N)
    if "cci17" not in x: x["cci17"] = cci(x, C.F_CCI_N)
    return x

# ---------- shared ZigZag (used by E; B3 uses the same confirmation rule with its own deviation/push) ----------
def zz_dev(x, i, dev_atr=None, dev_points=None):
    """MT ZigZag 'deviation 5' is 5 POINTS: on 5-digit OTC quotes that is ~0.5 pip = noise on 1m, so the deviation is
    max(dev_points × point, dev_atr × ATR). Point size is inferred from the quote's decimals."""
    a = float(x.atr.iloc[i]); px = float(x.close.iloc[i])
    dec = 5 if px < 10 else (3 if px < 1000 else 2); pt = 10 ** -dec
    return max((dev_points if dev_points is not None else C.E_ZZ_DEV_POINTS) * pt, (dev_atr if dev_atr is not None else C.E_ZZ_DEV_ATR) * a)
def zz_confirmed_pivot(x, i, side, dev, depth=None, backstep=None):
    """Last confirmed pivot ending a leg: j = extreme of the last `depth` bars (top for side 'top'), at least 1 bar old,
    no newer extreme in the following `backstep` bars, retrace from j up to bar i >= dev, and the leg INTO j >= dev.
    Returns j or None. Only bars <= i are read."""
    depth = depth or C.E_ZZ_DEPTH; backstep = backstep or C.E_ZZ_BACKSTEP
    lo = i - depth + 1
    if lo < depth + 1: return None
    w = x.iloc[lo:i + 1]
    j = lo + int(np.argmax(w.high.values) if side == "top" else np.argmin(w.low.values))
    if j >= i or j > i - 1: return None
    ext = x.high.iloc[j] if side == "top" else x.low.iloc[j]
    after = x.iloc[j + 1:i + 1]
    retr = ext - after.low.min() if side == "top" else after.high.max() - ext
    if retr < dev: return None
    pre = x.iloc[max(0, j - depth):j]
    leg = ext - pre.low.min() if side == "top" else pre.high.max() - ext
    if leg < dev: return None
    # backstep: a pivot needs `backstep` bars after it before a new extreme could replace it — satisfied by j being the window extreme
    return j

def strat_E(x, i, variant=None):
    """E 'ZigZag + DeMarker scalping': confirmed down-leg end + DeMarker crosses up through 0.30 → CALL (mirror for PUT)."""
    ensure_cols(x); v = _v(variant)
    if i < 40: return None
    d0, d1 = x.dem.iloc[i - 1], x.dem.iloc[i]; dev = zz_dev(x, i); r = x.iloc[i]
    side = None
    if d0 < C.E_DEM_LO <= d1 and zz_confirmed_pivot(x, i, "bot", dev) is not None: side = "CALL"
    elif d0 > C.E_DEM_HI >= d1 and zz_confirmed_pivot(x, i, "top", dev) is not None: side = "PUT"
    if side is None: return None
    if v == "improved":
        if r.adx >= C.ADX_TREND and ((r.ema_f > r.ema_s) != (side == "CALL")): return None   # strong trend against
        if (r.close > r.open) != (side == "CALL"): return None                               # cross candle must agree
    return side, round(min(1.0, 0.55 + abs(d1 - 0.5)), 2)

def strat_F(x, i, variant=None):
    """F 'Double Oscillator Reversal': RSI12 (20/80) and CCI17 (±100) both extreme within the last 3 bars and both hook
    together on the closed candle; GREEN → CALL, RED → PUT."""
    ensure_cols(x); v = _v(variant)
    if i < 40: return None
    r12, cc, r = x.rsi12, x.cci17, x.iloc[i]; L = C.F_LOOK; side = None
    os_ = (r12.iloc[i - L:i] < C.F_RSI_LO).any() and (cc.iloc[i - L:i] < -C.F_CCI_X).any()
    ob_ = (r12.iloc[i - L:i] > C.F_RSI_HI).any() and (cc.iloc[i - L:i] > C.F_CCI_X).any()
    if os_ and r12.iloc[i] > r12.iloc[i - 1] and cc.iloc[i] > cc.iloc[i - 1] and r.close > r.open: side = "CALL"
    elif ob_ and r12.iloc[i] < r12.iloc[i - 1] and cc.iloc[i] < cc.iloc[i - 1] and r.close < r.open: side = "PUT"
    if side is None: return None
    if v == "improved":
        if r.adx >= C.ADX_TREND and ((r.ema_f > r.ema_s) != (side == "CALL")): return None
        if side == "CALL" and r.close <= x.close.iloc[i - 1]: return None                    # hook must recover the prior close
        if side == "PUT" and r.close >= x.close.iloc[i - 1]: return None
    return side, 0.7

# ---------- B4 Frozen Candle micro-lag (ticks) ----------
def bug_B4(ticks, now_ts, x1=None, variant=None, freeze_s=None):
    """Trend + no price change for >= freeze_s with no rejection wick, tick momentum with trend → ENTER NOW with trend.
    Returns (dir, why, price, kind) where kind is 'pause' (no ticks) or 'flat' (ticks, same price) — tracked separately."""
    v = _v(variant); fz = freeze_s or C.B4_FREEZE_S
    if now_ts % 60 > 50 or not ticks or x1 is None or len(x1) < 30: return None
    tk = [(t, p) for t, p in ticks if t <= now_ts]
    if len(tk) < 20: return None
    last_t, last_p = tk[-1]; k = len(tk) - 1
    while k > 0 and tk[k - 1][1] == last_p: k -= 1
    since = now_ts - tk[k][0]
    if since < fz: return None
    kind = "pause" if now_ts - last_t >= fz else "flat"
    bs = bars5(tk, now_ts)
    if len(bs) < C.B4_MOM_BARS + 2: return None
    r = x1.iloc[-1]; up = r.ema_f > r.ema_s
    trend = r.adx >= C.ADX_TREND
    slope = bs[-1][4] - bs[-1 - C.B4_MOM_BARS][4]; med = float(np.median([b[2] - b[3] for b in bs[-37:-1]]) or 0)
    if med <= 0: return None
    strong = abs(slope) >= 2.0 * med
    if not (trend or (v == "original" and strong)): return None
    if (slope > 0) != up or not strong: return None                                        # tick momentum with the trend
    o, h, l, c = bs[-1][1:]
    rng = h - l; wick = (h - max(o, c)) if up else (min(o, c) - l)
    if rng > 0 and wick >= 0.5 * rng: return None                                          # rejection wick → no
    if v == "improved" and kind == "pause" and since > 4 * fz: return None                 # long dead feed = outage, not micro-lag
    return ("CALL" if up else "PUT"), f"{kind} {since:.1f}s in {'up' if up else 'down'}trend (ADX {r.adx:.0f}, 30s move {abs(slope)/med:.1f}× med)", last_p, kind

# ---------- B5 5-Consecutive Exhaustion ----------
def sub_bars(ticks, end_ts, sec):
    b = {}
    for t, p in ticks:
        if t >= end_ts: break
        kk = int(t // sec) * sec; o = b.get(kk)
        if o is None: b[kk] = [p, p, p, p]
        else: o[1] = max(o[1], p); o[2] = min(o[2], p); o[3] = p
    last = int(end_ts // sec) * sec
    return [(kk, *vv) for kk, vv in sorted(b.items()) if kk + sec <= last]
def _five(o, h, l, c, variant, adx=None):
    n = C.B5_N
    if len(o) < n: return None
    o, h, l, c = (np.asarray(a[-n:], float) for a in (o, h, l, c))
    rng = h - l
    if (rng <= 0).any(): return None
    body = np.abs(c - o); col = np.sign(c - o)
    if not (abs(col.sum()) == n and (body / rng >= C.B5_BODY_FRAC).all()): return None
    if variant == "improved":
        if adx is not None and adx >= C.ADX_TREND + 10: return None                          # strong breakout, don't fade
        if body[-1] > body[-2]: return None                                                   # last body must not be growing
    return ("PUT" if col[0] > 0 else "CALL"), f"{n} consecutive full {'green' if col[0] > 0 else 'red'} bodies"
def bug_B5_sub(ticks, now_ts, sec, x1=None, variant=None):
    """Sub-minute version (10 s / 15 s bars from ticks): at the open of the 6th sub-bar → ENTER NOW opposite."""
    if now_ts % 60 > 50: return None
    bs = sub_bars(ticks, now_ts, sec)
    if len(bs) < C.B5_N or any(bs[-k][0] - bs[-k - 1][0] != sec for k in range(1, C.B5_N)): return None
    if now_ts - (bs[-1][0] + sec) > 1.5: return None                                         # only right at the 6th bar's open
    o, h, l, c = zip(*[b[1:] for b in bs])
    adx = float(x1.adx.iloc[-1]) if x1 is not None and len(x1) else None
    r = _five(o, h, l, c, _v(variant), adx)
    return (r[0], f"{sec}s: {r[1]}", c[-1]) if r else None
def bug_B5_1m(x, i, variant=None):
    r = _five(x.open.iloc[:i + 1].values, x.high.iloc[:i + 1].values, x.low.iloc[:i + 1].values, x.close.iloc[:i + 1].values,
              _v(variant), float(x.adx.iloc[i]))
    return (r[0], "1m: " + r[1]) if r else None

# ---------- B6 Bollinger Out-of-Bounds (shares the stretch maths with telemetry over-extension) ----------
def bands(x, n, k):
    m = x.close.rolling(n).mean(); s = x.close.rolling(n).std(ddof=0)
    return m, m + k * s, m - k * s
def bug_B6(x, i, n=14, k=2.0, variant=None):
    """Closed candle whose BODY (improved: whole candle incl. wicks) is outside BB(n,k) with a visible gap → reversal."""
    v = _v(variant)
    if i < n + 5: return None
    key = f"_bb{n}"
    if key + "u" not in x: m, u, l = bands(x, n, k); x[key + "m"], x[key + "u"], x[key + "l"] = m, u, l
    u, l, a, r = x[key + "u"], x[key + "l"], float(x.atr.iloc[i]), x.iloc[i]
    gap = 0.05 * a
    if v == "improved": above, below = r.low > u.iloc[i] + gap, r.high < l.iloc[i] - gap
    else: above, below = min(r.open, r.close) > u.iloc[i] + gap, max(r.open, r.close) < l.iloc[i] - gap
    if not (above or below): return None
    if v == "improved":
        prev = [(x.close.iloc[j] > u.iloc[j]) if above else (x.close.iloc[j] < l.iloc[j]) for j in (i - 1, i - 2)]
        if all(prev): return None                                                            # band walk = trend
        if float(x.atr.iloc[i]) < 1.1 * float(x.atr.iloc[i - 5]) and (r.high - r.low) < 1.5 * a: return None   # need a spike
    return ("PUT" if above else "CALL"), f"{'whole candle' if v == 'improved' else 'body'} {'above' if above else 'below'} BB({n},{k:g})"

# ---------- leaderboard ----------
def wilson_lb(w, n, z=1.64):
    if n == 0: return 0.0
    p = w / n; d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
def lb_score(w, l, payout):
    n = w + l; be = 1 / (1 + payout)
    return wilson_lb(w, n) - be, (w / n if n else 0), n, be

# ---------- double confirmation (all sources) ----------
# Classification of existing conditions:
#   momentum : RSI/DeMarker/CCI/stochastic extremes and hooks, colour streaks (B5, B3 push), strong bodies (B2), tick momentum (B4), spikes (B1),
#              trend/ADX votes (A, D), oscillator crosses (E, F)
#   structure: Bollinger band touch (B, B6), swing S/R / ZigZag pivot (B2 levels, B3/E pivots, C S/R), rejection wick / pin bar (C, B1 wick)
# Strategies that had only one category (A, D, E-cross, F, B4, B5) now also need a structural check in the same direction.
def double_confirm(x, i, side, fill=False):
    """(momentum_ok, structure_ok, text) for a CALL/PUT signal on closed bar i. Uses bars <= i only."""
    ensure_cols(x); r = x.iloc[i]; a = float(r.atr) or 1e-12; up = side == "CALL"
    rng = float(r.high - r.low) or 1e-12; body = float(r.close - r.open)
    lo_w = (min(r.open, r.close) - r.low) / rng; hi_w = (r.high - max(r.open, r.close)) / rng
    cols = np.sign(x.close.iloc[max(0, i - 2):i + 1].values - x.open.iloc[max(0, i - 2):i + 1].values)
    mom = []
    if (r.rsi <= 35) if up else (r.rsi >= 65): mom.append("RSI")
    if (r.dem <= 0.3) if up else (r.dem >= 0.7): mom.append("DeMarker")
    if (r.cci17 <= -100) if up else (r.cci17 >= 100): mom.append("CCI")
    if (r.k <= 20) if up else (r.k >= 80): mom.append("Stoch")
    if len(cols) == 3 and (cols == (-1 if up else 1)).all(): mom.append("3-candle streak")          # exhaustion into the level
    if (body >= 0.5 * a) if up else (-body >= 0.5 * a): mom.append("strong body")                    # momentum with the signal
    st = []
    bz = float(r.bb_z) if np.isfinite(r.bb_z) else 0.0
    if (bz <= -1.5) if up else (bz >= 1.5): st.append("Bollinger")
    if (r.low <= r.sup + 0.3 * a) if up else (r.high >= r.res - 0.3 * a): st.append("swing S/R")
    lo = max(0, i - 11); w = x.iloc[lo:i + 1]
    j = lo + int(np.argmin(w.low.values) if up else np.argmax(w.high.values))
    if i - 3 <= j <= i: st.append("ZigZag pivot")
    if fill:   # Tier-2 fill: structure = any one of BB touch / S/R within 0.3 ATR / wick >= FILL_WICK_MIN (no pivot-only confirmation)
        st = [k for k in st if k != "ZigZag pivot"]
        if (lo_w >= C.FILL_WICK_MIN) if up else (hi_w >= C.FILL_WICK_MIN): st.append(f"wick≥{C.FILL_WICK_MIN:.0%}")
    elif (lo_w >= 0.5) if up else (hi_w >= 0.5): st.append("rejection wick")
    return bool(mom), bool(st), f"momentum: {', '.join(mom) or '—'} · structure: {', '.join(st) or '—'}"
