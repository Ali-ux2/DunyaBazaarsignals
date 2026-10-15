"""Regime-aware, decorrelated M1 signal engine (pandas/numpy). NO martingale.
analyze(df, pair, payout=None) -> dict with "signal" CALL/PUT or None + "reject".
The last row of df must be a CLOSED candle."""
from __future__ import annotations
import math, json
from collections import defaultdict, deque
import numpy as np, pandas as pd
import config as C

def breakeven(payout): return 1.0 / (1.0 + payout)

# ---------- indicators ----------
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rsi(c, n):
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return (100 - 100/(1 + up/dn.replace(0, np.nan))).fillna(50)
def atr(df, n):
    pc = df.close.shift()
    tr = pd.concat([df.high-df.low, (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()
def adx(df, n):
    up, dn = df.high.diff(), -df.low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), df.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), df.index)
    tr = atr(df, n).replace(0, np.nan)
    pdi = 100*pdm.ewm(alpha=1/n, adjust=False).mean()/tr; ndi = 100*ndm.ewm(alpha=1/n, adjust=False).mean()/tr
    return (100*(pdi-ndi).abs()/(pdi+ndi).replace(0, np.nan)).ewm(alpha=1/n, adjust=False).mean().fillna(0)

def compute(df):
    x = df[["open", "high", "low", "close"]].astype(float).copy()
    x["ema_f"], x["ema_s"] = ema(x.close, C.EMA_FAST), ema(x.close, C.EMA_SLOW)
    x["rsi"] = rsi(x.close, C.RSI_LEN)
    ll, hh = x.low.rolling(C.STOCH_K).min(), x.high.rolling(C.STOCH_K).max()
    x["k"] = (100*(x.close-ll)/(hh-ll).replace(0, np.nan)).rolling(C.STOCH_SMOOTH).mean().fillna(50)
    x["d"] = x.k.rolling(C.STOCH_D).mean().fillna(50)
    x["atr"] = atr(x, C.ATR_LEN)
    x["atr_pctl"] = x.atr.rolling(C.ATR_PCTL_LOOKBACK, min_periods=C.ATR_PCTL_MIN_PERIODS).rank(pct=True)*100
    x["adx"] = adx(x, C.ADX_LEN)
    ch = (x.close - x.close.shift(C.ER_LEN)).abs(); vol = x.close.diff().abs().rolling(C.ER_LEN).sum()
    x["er"] = (ch/vol.replace(0, np.nan)).fillna(0)
    m, sd = x.close.rolling(C.BB_LEN).mean(), x.close.rolling(C.BB_LEN).std()
    x["bb_z"] = (x.close-m)/sd.replace(0, np.nan)
    x["res"], x["sup"] = x.high.rolling(C.SR_LEN).max(), x.low.rolling(C.SR_LEN).min()
    return x

# ---------- votes (one per category) ----------
def v_trend(x, i):
    r = x.iloc[i]; prev = x.ema_f.iloc[i-C.EMA_SLOPE_LOOKBACK]
    w = x.iloc[i-2:i+1]
    if r.ema_f > r.ema_s and r.ema_f > prev and r.close > r.ema_f and (w.low <= w.ema_f).any():
        return 1, "EMA9>21 rising, bounced off EMA9"
    if r.ema_f < r.ema_s and r.ema_f < prev and r.close < r.ema_f and (w.high >= w.ema_f).any():
        return -1, "EMA9<21 falling, rejected at EMA9"
    return 0, ""
def v_momentum(x, i, reg):
    r, p = x.iloc[i], x.iloc[i-1]
    if reg == "trend":
        if 55 <= r.rsi <= 70 and r.k > r.d and r.k < 80: return 1, f"RSI {r.rsi:.0f} + Stoch up"
        if 30 <= r.rsi <= 45 and r.k < r.d and r.k > 20: return -1, f"RSI {r.rsi:.0f} + Stoch down"
    else:
        if p.k <= p.d and r.k > r.d and p.k < 25: return 1, "Stoch cross up from oversold"
        if p.k >= p.d and r.k < r.d and p.k > 75: return -1, "Stoch cross down from overbought"
    return 0, ""
def v_location(x, i, reg):
    r = x.iloc[i]; rng = r.res - r.sup
    if rng < C.SR_MIN_RANGE_ATR*r.atr: return 0, ""
    pos = (r.close - r.sup)/rng
    if reg == "trend":
        if r.ema_f > r.ema_s and pos < 0.8 and r.bb_z < 2: return 1, f"room to resistance ({pos:.2f})"
        if r.ema_f < r.ema_s and pos > 0.2 and r.bb_z > -2: return -1, f"room to support ({pos:.2f})"
    else:
        if pos <= 0.2 and r.bb_z <= -1.5: return 1, f"at support, BB z {r.bb_z:.1f}"
        if pos >= 0.8 and r.bb_z >= 1.5: return -1, f"at resistance, BB z {r.bb_z:.1f}"
    return 0, ""
def v_candle(x, i, reg):
    r = x.iloc[i]; rng = r.high - r.low
    if rng <= 0: return 0, ""
    body = abs(r.close-r.open)/rng; up = (r.high-max(r.open, r.close))/rng; lo = (min(r.open, r.close)-r.low)/rng
    if reg == "trend":
        if body >= C.BODY_MIN and up < C.WICK_MAX and lo < C.WICK_MAX:
            return (1, "strong bull body") if r.close > r.open else (-1, "strong bear body")
    else:
        if lo >= 0.5 and body <= 0.35: return 1, "lower-wick rejection"
        if up >= 0.5 and body <= 0.35: return -1, "upper-wick rejection"
    return 0, ""

def overlap_ok(x, i, direction):
    """CALL: last closed candle green AND closes above the high of the previous green candle
    (searched back OVERLAP_LOOKBACK bars). PUT: red AND closes below the previous red candle's low."""
    r = x.iloc[i]
    green = r.close > r.open
    if direction == "CALL" and not green: return False
    if direction == "PUT" and not (r.close < r.open): return False
    for j in range(i-1, max(i-1-C.OVERLAP_LOOKBACK, 0), -1):
        p = x.iloc[j]
        if direction == "CALL" and p.close > p.open: return bool(r.close > p.high)
        if direction == "PUT" and p.close < p.open: return bool(r.close < p.low)
    return False

def chop(x, i):
    w = x.iloc[i-C.CHOP_WINDOW+1:i+1]
    return int(((w.close-w.ema_s).abs()/w.ema_s <= C.CHOP_PCT).sum()) >= C.CHOP_BARS
def regime(r):
    if r.adx >= C.ADX_TREND and r.er >= C.ER_TREND: return "trend"
    if r.adx <= C.ADX_RANGE: return "range"
    return "unclear"

# ---------- pre-entry confirmation ----------
def precheck(sig, f_open, price):
    """Judge the forming candle (open f_open, current price) just before entry.
    Returns (ok: bool, reason). Mirror logic for PUT via sign d."""
    d = 1 if sig["signal"] == "CALL" else -1; a = sig["atr"] or 1e-12
    move = d*(price - f_open); body = abs(price - f_open)
    if move > C.PC_EXHAUST_ATR*a: return False, f"exhausted: ran {move/a:.1f} ATR already"
    if (d == 1 and price >= sig["resistance"]) or (d == -1 and price <= sig["support"]): return False, "already at S/R target"
    if move < 0 and body >= C.PC_STRONG_BODY_ATR*a: return False, f"strong opposing candle ({body/a:.1f} ATR)"
    if -move > C.PC_MAX_ADVERSE_ATR*a: return False, f"moved {(-move)/a:.1f} ATR against"
    # "crossed BACK": only applies if the signal candle closed on the signal side of EMA9 (trend setups);
    # range-fade signals start on the far side of EMA9 by design.
    if C.PC_USE_EMA9 and d*(sig.get("sig_close", price) - sig["ema9"]) > 0 and d*(price - sig["ema9"]) < 0:
        return False, "crossed back through EMA9"
    if C.PC_USE_SIG_MID and d*(price - sig["sig_mid"]) < 0: return False, "crossed back through signal candle midpoint"
    if move >= 0: return True, "forming candle with signal"
    if body < C.PC_SMALL_BODY_ATR*a: return True, "small opposing body"
    return False, "opposing candle, not confirmed"

def switch_body_atr(sig, f_open, price):
    """Forming-candle body AGAINST the signal, in ATR (<= 0 means with the signal or doji)."""
    d = 1 if sig["signal"] == "CALL" else -1
    return -d*(price - f_open)/(sig["atr"] or 1e-12)

def switch_kind(sig, f_open, price, ok, reason):
    """Final :57 decision for direction mode: None = keep, else 'momentum'/'reversion'.
    Any switch requires a clearly opposite forming candle: body >= SWITCH_MIN_BODY_ATR."""
    k = precheck_flip_kind(ok, reason)
    if k is not None and switch_body_atr(sig, f_open, price) < C.SWITCH_MIN_BODY_ATR: return None
    return k

def precheck_flip_kind(ok, reason):
    """For PRECHECK_MODE='direction': None = keep the signal direction; else why we SWITCH.
    momentum  : forming candle strongly opposes (strong body / moved >0.5 ATR against / crossed back) -> follow it
    reversion : signal-direction move already exhausted (>1.5 ATR) or price hit the S/R target -> fade it
    Mild opposing candles and an unavailable candle keep the original direction."""
    if ok or reason in ("opposing candle, not confirmed", "forming candle unavailable"): return None
    if reason.startswith("exhausted") or reason.startswith("already at S/R"): return "reversion"
    return "momentum"

# ---------- pair tracker ----------
class PairTracker:
    def __init__(self): self.res = defaultdict(lambda: deque(maxlen=C.ROLLING_N)); self.total = defaultdict(lambda: [0, 0])
    def record(self, pair, win):
        self.res[pair].append(int(win)); self.total[pair][0 if win else 1] += 1
    def winrate(self, pair):
        d = self.res[pair]; return sum(d)/len(d) if d else None
    def gate(self, pair, payout):
        """None if OK, else reason. Rolling WR must beat breakeven(payout)+margin."""
        d = self.res[pair]
        if len(d) < C.MIN_TRADES_FOR_GATE: return None
        need = breakeven(payout) + C.PAYOUT_EDGE_MARGIN; wr = sum(d)/len(d)
        return None if wr >= need else f"pair WR {wr:.1%} < {need:.1%} needed at {payout:.0%} payout"
    def to_json(self): return {p: {"recent": list(d), "wins": self.total[p][0], "losses": self.total[p][1]} for p, d in self.res.items()}
    def load(self, data):
        for p, v in data.items():
            self.res[p].extend(v.get("recent", [])); self.total[p] = [v.get("wins", 0), v.get("losses", 0)]
    def reset(self): self.res.clear(); self.total.clear()
tracker = PairTracker()

# ---------- main ----------
def analyze(df, pair, payout=None, x=None, i=None, gaps=0, use_gate=True):
    if x is None: x = compute(df)
    if i is None: i = len(x)-1
    p = C.DEFAULT_PAYOUT if payout is None else payout
    if i + 1 < C.MIN_BARS: return {"signal": None, "reject": "not enough history"}
    if p < C.MIN_PAYOUT: return {"signal": None, "reject": f"payout {p:.0%} < min {C.MIN_PAYOUT:.0%}"}
    if gaps > C.MAX_GAP_RUNS: return {"signal": None, "reject": f"feed gaps ({gaps})"}
    r = x.iloc[i]
    if not np.isfinite([r.atr, r.atr_pctl, r.bb_z]).all(): return {"signal": None, "reject": "warming up"}
    if r.atr_pctl < C.ATR_PCTL_MIN: return {"signal": None, "reject": "volatility too low"}
    if r.atr_pctl > C.ATR_PCTL_MAX: return {"signal": None, "reject": "volatility spike"}
    reg = regime(r)
    if reg == "unclear": return {"signal": None, "reject": "regime unclear"}
    if reg == "trend" and chop(x, i): return {"signal": None, "reject": "chop around EMA21"}
    if use_gate and (g := tracker.gate(pair, p)): return {"signal": None, "reject": g}
    W = C.W_TREND if reg == "trend" else C.W_RANGE
    votes = {"trend": v_trend(x, i) if W["trend"] else (0, ""), "momentum": v_momentum(x, i, reg),
             "location": v_location(x, i, reg), "candle": v_candle(x, i, reg)}
    if not C.ALLOW_CONFLICT and len({v for v, _ in votes.values() if v}) > 1: return {"signal": None, "reject": "conflicting votes"}
    score = sum(W[k]*v for k, (v, _) in votes.items()); mx = sum(W.values()); conf = abs(score)/mx
    if conf < C.MIN_CONF: return {"signal": None, "reject": "score below threshold", "score": score}
    direction = "CALL" if score > 0 else "PUT"
    ov = None
    if C.OVERLAP_MODE != "off":
        ov = overlap_ok(x, i, direction)
        if C.OVERLAP_MODE == "require" and not ov: return {"signal": None, "reject": "overlap confirmation failed"}
    return {"signal": direction, "overlap": ov, "pair": pair, "regime": reg, "score": round(score, 2),
            "max_score": mx, "confidence": round(conf*100), "reasons": [t for v, t in votes.values() if v], "votes": {k: [v, t] for k, (v, t) in votes.items()},
            "support": float(r.sup), "resistance": float(r.res), "atr": float(r.atr), "ema9": float(r.ema_f),
            "sig_mid": float((r.high + r.low)/2), "sig_close": float(r.close), "adx": round(float(r.adx), 1), "er": round(float(r.er), 2), "payout": p, "breakeven": round(breakeven(p), 4)}

# ---------- relaxed shadow engine ----------
from contextlib import contextmanager
@contextmanager
def _overrides(ov):
    old = {k: getattr(C, k) for k in ov}
    try:
        for k, v in ov.items(): setattr(C, k, v)
        yield
    finally:
        for k, v in old.items(): setattr(C, k, v)

shadow_tracker = PairTracker()   # separate from the real tracker; never used for gating/compounding

def analyze_relaxed(df, pair, payout=None, gaps=0, x=None):
    """Same engine with C.SHADOW_OVERRIDES. Keeps MIN_PAYOUT and gap rejection; the real pair gate is not applied."""
    with _overrides(C.SHADOW_OVERRIDES):
        return analyze(df, pair, payout=payout, gaps=gaps, x=x, use_gate=False)

# ---------- ENSEMBLE: independent strategies, each -> (direction "CALL"/"PUT", score 0..1) or None ----------
def _ctx(x, i, pair, p):
    r = x.iloc[i]
    return {"pair": pair, "support": float(r.sup), "resistance": float(r.res), "atr": float(r.atr), "ema9": float(r.ema_f),
            "sig_mid": float((r.high + r.low)/2), "sig_close": float(r.close), "adx": round(float(r.adx), 1), "er": round(float(r.er), 2),
            "payout": p, "breakeven": round(breakeven(p), 4)}

def strat_trend_pullback(x, i):
    """A: EMA9/21 trend with ADX, pullback touched EMA9 in the last 3 bars, closed back with the trend."""
    r = x.iloc[i]; w = x.iloc[i-2:i+1]
    if r.adx < C.ADX_TREND: return None
    sc = min(1.0, 0.5 + (r.adx - C.ADX_TREND)/30 + 0.2*min(r.er, 1))
    if r.ema_f > r.ema_s and r.close > r.ema_f and r.close > r.open and (w.low <= w.ema_f).any(): return "CALL", round(sc, 2)
    if r.ema_f < r.ema_s and r.close < r.ema_f and r.close < r.open and (w.high >= w.ema_f).any(): return "PUT", round(sc, 2)
    return None

def strat_range_reversion(x, i):
    """B: ranging market (ADX low), price stretched to a Bollinger extreme with momentum exhaustion (RSI/Stoch)."""
    r, q = x.iloc[i], x.iloc[i-1]
    if r.adx > C.ADX_RANGE + 2 or not np.isfinite(r.bb_z): return None
    sc = lambda z: round(min(1.0, 0.5 + (abs(z) - C.B_BB_Z)/2), 2)
    if r.bb_z <= -C.B_BB_Z and (r.rsi < 35 or r.k < 20) and r.k >= q.k: return "CALL", sc(r.bb_z)
    if r.bb_z >= C.B_BB_Z and (r.rsi > 65 or r.k > 80) and r.k <= q.k: return "PUT", sc(r.bb_z)
    return None

def strat_sr_rejection(x, i):
    """C: pin/wick rejection candle at support/resistance (level within 0.3 ATR, long wick, close away from level)."""
    r = x.iloc[i]; rng = r.high - r.low
    if rng <= 0 or (r.res - r.sup) < C.SR_MIN_RANGE_ATR * r.atr: return None
    lo = (min(r.open, r.close) - r.low)/rng; up = (r.high - max(r.open, r.close))/rng
    sup_prev, res_prev = x.sup.iloc[i-1], x.res.iloc[i-1]
    if lo >= C.C_WICK_MIN and r.low <= sup_prev + 0.3*r.atr and r.close > (r.high + r.low)/2: return "CALL", round(min(1.0, lo + 0.2), 2)
    if up >= C.C_WICK_MIN and r.high >= res_prev - 0.3*r.atr and r.close < (r.high + r.low)/2: return "PUT", round(min(1.0, up + 0.2), 2)
    return None

def strat_voting(x, i, pair, p):
    """D: the original regime-aware voting engine (analyze) as one member."""
    s = analyze(None, pair, payout=p, x=x, i=i, use_gate=False)
    return ((s["signal"], round(s["confidence"]/100, 2)) if s["signal"] else None), s

STRATEGY_NAMES = {"A": "trend-pullback", "B": "range-reversion", "C": "S/R-rejection", "D": "voting-engine",
                  "E": "ZigZag+DeMarker", "F": "double-oscillator"}
STRAT_WEIGHTS = {}     # leaderboard weights 0.5-1.5 set by the bot once enough forward data (default 1.0)
STRAT_MUTED = set()    # muted members still compute (paper-tracked) but don't vote

def ensemble(df, pair, payout=None, x=None, i=None, gaps=0, use_gate=True):
    """Group signal: >= ENSEMBLE_MIN_AGREE strategies on one direction and no opposing strategy with score >= ENSEMBLE_STRONG_OPP.
    Shared gates first: history, 77% payout floor, gap rejection, warm-up, ATR percentile, pair win-rate gate, entry extension."""
    if x is None: x = compute(df)
    if i is None: i = len(x)-1
    p = C.DEFAULT_PAYOUT if payout is None else payout
    if i + 1 < C.MIN_BARS: return {"signal": None, "reject": "not enough history"}
    if p < C.MIN_PAYOUT: return {"signal": None, "reject": f"payout {p:.0%} < min {C.MIN_PAYOUT:.0%}"}
    if gaps > C.MAX_GAP_RUNS: return {"signal": None, "reject": f"feed gaps ({gaps})"}
    r = x.iloc[i]
    if not np.isfinite([r.atr, r.atr_pctl, r.bb_z]).all(): return {"signal": None, "reject": "warming up"}
    if r.atr_pctl < C.ATR_PCTL_MIN: return {"signal": None, "reject": "volatility too low"}
    if r.atr_pctl > C.ATR_PCTL_MAX: return {"signal": None, "reject": "volatility spike"}
    if use_gate and (g := tracker.gate(pair, p)): return {"signal": None, "reject": g}
    d_vote, d_full = strat_voting(x, i, pair, p)
    from bugstrats import strat_E, strat_F
    votes = {"A": strat_trend_pullback(x, i), "B": strat_range_reversion(x, i), "C": strat_sr_rejection(x, i), "D": d_vote}
    if C.ENSEMBLE_EF: votes.update(E=strat_E(x, i), F=strat_F(x, i))
    live = {k: v for k, v in votes.items() if k not in STRAT_MUTED and (not C.ENSEMBLE_ONLY or k in C.ENSEMBLE_ONLY)}
    wt = lambda k, v: v[1] * STRAT_WEIGHTS.get(k, C.STRAT_PRIOR.get(k, 1.0))
    calls = {k: v for k, v in live.items() if v and v[0] == "CALL"}; puts = {k: v for k, v in live.items() if v and v[0] == "PUT"}
    if not calls and not puts: return {"signal": None, "reject": "no strategy fired", "strategies": votes}
    side, agree, opp = ("CALL", calls, puts) if len(calls) > len(puts) or (len(calls) == len(puts) and
                       sum(wt(k, v) for k, v in calls.items()) >= sum(wt(k, v) for k, v in puts.items())) else ("PUT", puts, calls)
    if len(agree) < C.ENSEMBLE_MIN_AGREE:
        return {"signal": None, "reject": f"only {len(agree)} strategy agrees (need {C.ENSEMBLE_MIN_AGREE})", "strategies": votes}
    strong = [k for k, v in opp.items() if wt(k, v) >= C.ENSEMBLE_STRONG_OPP]
    if strong: return {"signal": None, "reject": f"strong opposing strategy {','.join(strong)}", "strategies": votes}
    ext = abs(r.close - r.ema_f)/(r.atr or 1e-12)
    if C.ENTRY_CHECK and ext > C.ENTRY_MAX_EXT_ATR:
        return {"signal": None, "reject": f"entry overextended: close {ext:.1f} ATR from EMA9 (> {C.ENTRY_MAX_EXT_ATR})", "strategies": votes}
    conf = min(1.0, sum(wt(k, v) for k, v in agree.items())/len(agree))
    out = _ctx(x, i, pair, p)
    out.update(signal=side, strategies=votes, agree=sorted(agree), oppose=sorted(opp), confidence=round(conf*100),
               score=round(sum(v[1] for v in agree.values()), 2), max_score=len(votes),
               regime=d_full.get("regime") or regime(r), overlap=(overlap_ok(x, i, side) if C.OVERLAP_MODE != "off" else None),
               votes=d_full.get("votes", {}), reasons=[f"{k} {STRATEGY_NAMES[k]} {v[1]:.2f}" for k, v in sorted(agree.items())])
    return out

def strategies_line(s):
    v = s.get("strategies") or {}
    return " · ".join(f"{k}:{(x[0] + ' ' + format(x[1], '.2f')) if x else '—'}" for k, x in sorted(v.items()))

def level_overrides(level):
    lv = dict(C.LEVELS[max(1, min(5, int(level)))])
    return lv

def apply_level(level):
    """Main thread only: set the strict thresholds to a level; shadow = one level looser (capped at 5), overlap ignored."""
    for k, v in level_overrides(level).items(): setattr(C, k, v)
    lv = int(level)
    if lv >= 4:   # level 5 is proven losing as shadow; instead test whether B or C ALONE is worth posting at level 4
        sh = level_overrides(4); sh.update(ENSEMBLE_MIN_AGREE=1, ENSEMBLE_ONLY="BC")
    else: sh = level_overrides(min(lv + 1, 4))
    sh["OVERLAP_MODE"] = "off"; C.SHADOW_OVERRIDES = sh
    C.CURRENT_LEVEL = int(level)

def analyze_relaxed_ensemble(df, pair, payout=None, gaps=0, x=None):
    with _overrides(C.SHADOW_OVERRIDES):
        return ensemble(df, pair, payout=payout, gaps=gaps, x=x, use_gate=False)

# ---------- BUG / GLITCH WATCHER (EXPERIMENTAL) ----------
def _binom_p(k, n):
    """One-sided P(X >= k) for X ~ Bin(n, 0.5)."""
    if n <= 0: return 1.0
    return sum(math.comb(n, j) for j in range(k, n+1)) / 2**n

def bug_patterns(x):
    """Yield (key, description, cond_mask[np.bool], prediction[+1 green/-1 red or 0 = 'test both']) over closed bars of x.
    The outcome is the NEXT bar's colour; dojis are excluded."""
    o, c = x.open.values, x.close.values; col = np.sign(c - o).astype(int); n = len(col)
    body = np.abs(c - o); atrv = x.atr.values
    for k in (3, 4, 5):
        for X, nm in ((1, "greens"), (-1, "reds")):
            m = np.ones(n, bool)
            for j in range(k): m &= np.r_[np.zeros(j, bool), col[:n-j] == X] if j else (col == X)
            yield f"run{k}{'G' if X == 1 else 'R'}", f"after {k} {nm}", m, -X
    alt = np.zeros(n, bool)
    alt[3:] = (col[3:] != 0) & (col[3:] == -col[2:-1]) & (col[2:-1] == -col[1:-2]) & (col[1:-2] == -col[:-3])
    yield "alt4", "after 4 alternating (RGRG/GRGR) → alternation continues", alt, None   # pred = -col (continue)
    big = body > C.BUG_BIG_ATR * np.nan_to_num(atrv, nan=np.inf)
    yield "bigrev", f"after a big candle (> {C.BUG_BIG_ATR} ATR) → next reverses", big & (col != 0), None   # pred = -col
    mins = x.index.minute.values; hrs = x.index.hour.values
    if not C.BUG_TIME_PATTERNS: return
    for m in range(10):
        yield f"min{m}", f"bars opening at minute :x{m}", (mins % 10) == m, 0
    for h in range(24):
        yield f"hour{h:02d}", f"bars in hour {h:02d}:00 UTC", hrs == h, 0

def bug_scan(x, min_samples=None):
    ms = min_samples or C.BUG_MIN_SAMPLES
    """Return [(key, desc, pred_rule, rate, n)] for patterns beating BUG_MIN_RATE with >= BUG_MIN_SAMPLES and p < BUG_MAX_P/tests.
    Colour patterns test the bar itself for minute/hour buckets (pred 0) and the NEXT bar otherwise."""
    o, c = x.open.values, x.close.values; col = np.sign(c - o).astype(int); n = len(col)
    nxt = np.r_[col[1:], 0]; cands = []; tests = 0
    for key, desc, mask, pred in bug_patterns(x):
        mask = mask.copy(); mask[-1] = False if pred != 0 else mask[-1]
        if pred == 0:            # bucket colour bias: outcome = the bar's own colour
            out = col[mask]; out = out[out != 0]; nn = len(out)
            if nn < ms: continue
            g = int((out == 1).sum()); k, d = (g, 1) if g >= nn - g else (nn - g, -1)
        else:
            want = (-col if pred is None else np.full(n, pred))[mask]; out = nxt[mask]
            ok = out != 0; want, out = want[ok], out[ok]; nn = len(out)
            if nn < ms: continue
            hit = int((out == want).sum()); k, d = (hit, 1) if hit >= nn - hit else (nn - hit, -1)
        rate = k/nn; tests += 1
        if rate >= C.BUG_MIN_RATE: cands.append((key, desc, (pred, d), round(rate, 3), nn, _binom_p(k, nn)))
    # Bonferroni: ~50 patterns are tested per pair, so each must beat BUG_MAX_P / tests (stops random data from alerting)
    found = [c_[:5] for c_ in cands if c_[5] < C.BUG_MAX_P / max(tests, 1)]
    # identical consecutive bodies (glitch, info only)
    rel = np.abs(np.diff(np.abs(c - o))) <= 1e-9*np.maximum(np.abs(c[1:]), 1)
    nz = (np.abs(c - o)[1:] > 0)
    same = int((rel & nz).sum())
    if same >= C.BUG_MIN_SAMPLES and same/max(n-1, 1) >= C.BUG_SAME_BODY_FRAC:
        found.append(("samebody", f"identical consecutive candle bodies on {same/(n-1):.0%} of bars", None, round(same/(n-1), 3), same))
    return found

def bug_prediction(x, key, rule):
    """For an active pattern: predicted colour (+1/-1) for the NEXT bar if the latest closed bar matches, else None."""
    if rule is None: return None
    pred, d = rule
    for k2, _, mask, p2 in bug_patterns(x.iloc[-30:]):
        if k2 != key: continue
        if p2 == 0:              # bucket pattern: predict the next bar's colour if the next bar falls in the bucket
            nt = x.index[-1] + pd.Timedelta("60s")
            inb = (nt.minute % 10 == int(key[3:])) if key.startswith("min") else (nt.hour == int(key[4:]))
            return d if inb else None
        if not mask[-1]: return None
        last = int(np.sign(x.close.iloc[-1] - x.open.iloc[-1]))
        base = -last if p2 is None else p2
        return base * d
    return None

# ---------- NAMED BUG STRATEGIES B1/B2/B3 (EXPERIMENTAL). variant = "improved" (default) | "original" ----------
def demarker(x, n=14):
    """DeMarker(n) from closed bars only (causal SMAs)."""
    dmax = (x.high - x.high.shift()).clip(lower=0); dmin = (x.low.shift() - x.low).clip(lower=0)
    a, b = dmax.rolling(n).mean(), dmin.rolling(n).mean()
    return (a / (a + b).replace(0, np.nan)).fillna(0.5)

def _swing_levels(x, i, side):
    """Confirmed swing highs/lows (2 bars each side) in bars i-LOOKBACK .. i-3, clustered within 0.2 ATR; returns [(level, touches, last_j)]."""
    a = float(x.atr.iloc[i]); lo = max(2, i - C.B2_LOOKBACK); col = x.high if side == "res" else x.low
    piv = []
    for j in range(lo, i - 2):
        w = col.iloc[j-2:j+3]
        if (side == "res" and col.iloc[j] == w.max()) or (side == "sup" and col.iloc[j] == w.min()): piv.append((float(col.iloc[j]), j))
    out = []
    for lv, j in piv:
        near = [(l2, j2) for l2, j2 in piv if abs(l2 - lv) <= 0.2*a]
        if len(near) >= C.B2_TOUCHES: out.append((max(l for l, _ in near) if side == "res" else min(l for l, _ in near), len(near), max(j2 for _, j2 in near)))
    return out

def bug_B2(x, i, variant=None):
    """'1m Momentum Gap to S/R': strong candle closing just short of a multi-touch level -> continuation into the level."""
    v = variant or C.BUG_STRAT_VARIANT; r = x.iloc[i]; a = float(r.atr); rng = r.high - r.low
    if not np.isfinite(a) or a <= 0 or rng <= 0: return None
    body = abs(r.close - r.open)
    if body < C.B2_BODY_ATR*a or body/rng < C.B2_BODY_FRAC: return None
    green = r.close > r.open; side = "res" if green else "sup"
    lv = [(l, t, j) for l, t, j in _swing_levels(x, i, side) if (0 < l - r.close <= C.B2_DIST_ATR*a if green else 0 < r.close - l <= C.B2_DIST_ATR*a)]
    if not lv: return None
    level, touches, lastj = min(lv, key=lambda t: abs(t[0] - r.close))
    d = "CALL" if green else "PUT"
    if v == "improved":
        pos = (r.close - r.low)/rng
        if (green and pos < 0.8) or (not green and pos > 0.2): return None                       # close in the top/bottom 20%
        q = x.iloc[i-1]; prev_strong = (q.close > q.open) == green and abs(q.close - q.open) >= 0.5*a
        if not (prev_strong or a > 1.1*float(x.atr.iloc[i-5])): return None                      # momentum: 2 strong bodies or ATR expansion
        for j in range(max(0, i-15), i):                                                          # recent sharp rejection at that level -> skip
            b = x.iloc[j]; br = b.high - b.low
            if br <= 0: continue
            if green and b.high >= level - 0.1*a and (b.high - max(b.open, b.close))/br >= 0.6 and b.close < b.open: return None
            if not green and b.low <= level + 0.1*a and (min(b.open, b.close) - b.low)/br >= 0.6 and b.close > b.open: return None
        t, _ = v_trend(x, i)
        if (t > 0) != green and not ((r.ema_f > r.ema_s) == green): return None                  # trend side must agree
    return d, f"strong {'green' if green else 'red'} {body/a:.1f} ATR, {abs(level - r.close)/a:.2f} ATR short of {'resistance' if green else 'support'} ({touches} touches)"

def bug_B3(x, i, variant=None):
    """'3-Point Indicator Desync': confirmed pivot after a >=3-candle push + DeMarker/RSI extreme hooking back -> fade the push.
    Uses only bars <= i (no repaint): the pivot bar j is in i-3..i-1 and is confirmed by the retrace up to bar i."""
    v = variant or C.BUG_STRAT_VARIANT; a = float(x.atr.iloc[i])
    if i < 40 or not np.isfinite(a) or a <= 0: return None
    dem = x["dem"] if "dem" in x else demarker(x.iloc[:i+1], 14)
    rsi14 = x["rsi14"] if "rsi14" in x else rsi(x.close.iloc[:i+1], 14)
    win = x.iloc[i - C.B3_ZZ_DEPTH + 1:i + 1]
    for side in ("top", "bot"):
        j = int(np.argmax(win.high.values) if side == "top" else np.argmin(win.low.values)) + i - C.B3_ZZ_DEPTH + 1
        if not (i - 3 <= j <= i - 1): continue
        ext = x.high.iloc[j] if side == "top" else x.low.iloc[j]
        retr = (ext - x.low.iloc[j+1:i+1].min()) if side == "top" else (x.high.iloc[j+1:i+1].max() - ext)
        if retr < C.B3_ZZ_DEV_ATR * a: continue                                                   # zigzag pivot not confirmed
        cols = np.sign(x.close.iloc[j - C.B3_PUSH + 1:j + 1].values - x.open.iloc[j - C.B3_PUSH + 1:j + 1].values)
        if not (cols == (1 if side == "top" else -1)).all(): continue                              # >= 3-candle push into the pivot
        dz, rz = dem.iloc[j-1:i].max() if side == "top" else dem.iloc[j-1:i].min(), rsi14.iloc[j-1:i].max() if side == "top" else rsi14.iloc[j-1:i].min()
        extreme = (dz > 0.7 or rz > 70) if side == "top" else (dz < 0.3 or rz < 30)
        hook = (dem.iloc[i] < dem.iloc[i-1] or rsi14.iloc[i] < rsi14.iloc[i-1]) if side == "top" else (dem.iloc[i] > dem.iloc[i-1] or rsi14.iloc[i] > rsi14.iloc[i-1])
        if not (extreme and hook): continue
        if v == "improved":
            if float(x.adx.iloc[i]) > C.ADX_RANGE + 2: continue                                   # only fade pushes in ranges
            touch = (x.bb_z.iloc[j] >= 1.8 or x.high.iloc[j] >= x.res.iloc[j-1] - 0.1*a) if side == "top" else \
                    (x.bb_z.iloc[j] <= -1.8 or x.low.iloc[j] <= x.sup.iloc[j-1] + 0.1*a)
            div = False
            for k in range(max(5, j - 30), j - 4):                                                # earlier swing with stronger RSI = divergence
                if side == "top" and x.high.iloc[k] == x.high.iloc[k-2:k+3].max() and x.high.iloc[k] < ext and rsi14.iloc[k] > rsi14.iloc[j]: div = True; break
                if side == "bot" and x.low.iloc[k] == x.low.iloc[k-2:k+3].min() and x.low.iloc[k] > ext and rsi14.iloc[k] < rsi14.iloc[j]: div = True; break
            if not (touch or div): continue
        return ("PUT" if side == "top" else "CALL"), f"{'top' if side == 'top' else 'bottom'} pivot after {C.B3_PUSH}-candle push, DeMarker {dz:.2f}/RSI {rz:.0f} hooking"
    return None

def bars5(ticks, end_ts):
    """5-second OHLC bars from [(t, price)] up to (excluding) end_ts; only fully completed 5 s buckets."""
    b = {}
    for t, p in ticks:
        if t >= end_ts: break
        k = int(t // 5) * 5; o = b.get(k)
        if o is None: b[k] = [p, p, p, p]
        else: o[1] = max(o[1], p); o[2] = min(o[2], p); o[3] = p
    last_full = int(end_ts // 5) * 5
    return [(k, *v) for k, v in sorted(b.items()) if k + 5 <= last_full]

def bug_B1(ticks, now_ts, x1=None, variant=None):
    """'5s Gap Rejection' (stream pairs only): spike/gap or long rejection wick in the last completed 5 s bar -> fade it now."""
    v = variant or C.BUG_STRAT_VARIANT; sec = now_ts % 60
    if sec > 50: return None                                                                       # never within 10 s of the candle close
    bs = bars5(ticks, now_ts)
    if len(bs) < 36: return None                                                                   # need >= 3 min of 5 s bars
    hist = bs[-61:-1]; k, o, h, l, c = bs[-1]
    if hist[-1][0] != k - 5: return None                                                           # tick gap -> insufficient data
    med = float(np.median([b[2] - b[3] for b in hist]))
    if med <= 0: return None
    rng = h - l; gap = o - hist[-1][4]; up = h - max(o, c); lo = min(o, c) - l; d = why = None
    if rng > 0 and max(up, lo) >= C.B1_WICK_FRAC*rng and max(up, lo) >= C.B1_WICK_X*med:
        d, why = ("PUT", f"upper rejection wick {up/med:.1f}× median") if up >= lo else ("CALL", f"lower rejection wick {lo/med:.1f}× median")
    elif rng >= C.B1_SPIKE_X*med or abs(gap) >= C.B1_SPIKE_X*med:
        mv = (c - hist[-1][4]); d = "PUT" if mv > 0 else "CALL"; why = f"5 s spike {max(rng, abs(gap))/med:.1f}× median"
    if d is None: return None
    if v == "improved":
        if any((b[2] - b[3]) >= 1.5*med for b in bs[-4:-1]): return None                         # spike must be isolated
        if x1 is not None and len(x1):
            r = x1.iloc[-1]
            if float(r.adx) >= C.ADX_TREND: return None                                             # not inside a 1-min trend/breakout
            if (d == "PUT" and c <= r.ema_f) or (d == "CALL" and c >= r.ema_f): return None         # fade only spikes away from EMA9
    return d, why, c

# ---------- backtest ----------
def load_csv(path, pair=None):
    df = pd.read_csv(path); df.columns = [c.lower() for c in df.columns]
    if pair and "pair" in df.columns: df = df[df.pair == pair]
    for t in ("time", "timestamp", "datetime"):
        if t in df.columns:
            df[t] = pd.to_datetime(df[t], utc=True) if df[t].dtype == object else pd.to_datetime(df[t], unit="s", utc=True)
            df = df.set_index(t); break
    return df[["open", "high", "low", "close"]].astype(float)

def backtest(df, pair="TEST", payout=C.DEFAULT_PAYOUT, use_gate=False, log_path=None,
             scan_mode=None, precheck_mode=None):
    """Timing mirrors the live bot.
    closed : signal from closed bar i (posted at :02 of bar i+1) -> entry open of bar i+2.
    early52: signal at :52 of bar i -> entry open of bar i+1. APPROXIMATION: uses bar i's final OHLC as the
             :52 snapshot (8s look-ahead, optimistic).
    Precheck at :57 of the bar before entry: APPROXIMATION - that bar's close is the proxy for the :57 price.
    Expiry 1 bar; ties count as losses; cancelled trades are not counted as wins or losses."""
    sm = scan_mode or C.SCAN_MODE; pm = precheck_mode or C.PRECHECK_MODE
    x = compute(df); n = len(x); E = C.EXPIRY_MIN; lag = 1 if sm == "early52" else 2; tracker.reset()
    trades, rej, nxt = [], defaultdict(int), 0
    for i in range(C.MIN_BARS, n-lag-E):
        if i < nxt: continue
        p = float(payout.iloc[i]) if isinstance(payout, pd.Series) else payout
        s = analyze(None, pair, p, x, i, use_gate=use_gate)
        if not s["signal"]: rej[s["reject"].split(" (")[0]] += 1; continue
        e = i + lag; f = x.iloc[e-1]
        pc_ok, pc_why = precheck(s, f.open, f.close) if pm != "off" else (None, "")
        ent, ex = x.open.iloc[e], x.close.iloc[e+E-1]
        win = bool(ex > ent if s["signal"] == "CALL" else ex < ent)
        cancelled = pm == "confirm" and pc_ok is False
        flipped = pm == "direction" and switch_kind(s, f.open, f.close, pc_ok, pc_why) is not None
        if flipped: win = bool(ex < ent if s["signal"] == "CALL" else ex > ent) and ex != ent
        if not cancelled: tracker.record(pair, win)
        trades.append({"time": str(x.index[i]), "dir": s["signal"], "regime": s["regime"], "payout": p, "win": win,
                       "overlap": s.get("overlap"), "precheck_ok": pc_ok, "precheck_reason": pc_why, "cancelled": cancelled, "flipped": flipped})
        nxt = max(e + E + C.RESULT_COOLDOWN_MIN, i + C.SIGNAL_COOLDOWN_MIN)
    t = pd.DataFrame(trades); out = {"pair": pair, "bars": n, "scan_mode": sm, "precheck_mode": pm, "raw_signals": len(t)}
    if len(t):
        def summ(g):
            if not len(g): return {"n": 0}
            wr = g.win.mean(); se = math.sqrt(wr*(1-wr)/len(g)); be = breakeven(g.payout.mean())
            return {"n": len(g), "win_rate": round(wr, 4), "ci95": [round(wr-1.96*se, 4), round(wr+1.96*se, 4)],
                    "breakeven": round(be, 4), "ev_per_1_stake": round(float(np.where(g.win, g.payout, -1).mean()), 4),
                    "beats_breakeven": bool(wr > be), "significant": bool(wr-1.96*se > be)}
        out["without_precheck"] = summ(t)
        traded = t[~t.cancelled]
        out["traded"] = summ(traded); out["signals"] = len(traded)
        out.update(win_rate=out["traded"].get("win_rate"), breakeven=out["traded"].get("breakeven"))
        if pm != "off":
            out["precheck"] = {"approximate": True, "cancel_rate": round(float((t.precheck_ok == False).mean()), 4),
                               "confirmed": summ(t[t.precheck_ok == True]), "would_cancel": summ(t[t.precheck_ok == False]),
                               "cancel_reasons": t[t.precheck_ok == False].precheck_reason.str.split(":").str[0].str.split(" \\(").str[0].value_counts().to_dict()}
        if pm == "direction":
            out["direction_mode"] = {"approximate": True, "confirmed": summ(t[~t.flipped]), "switched": summ(t[t.flipped])}
        out["by_regime"] = {k: [round(v.win.mean(), 3), len(v)] for k, v in traded.groupby("regime")}
        if t.overlap.notna().any():
            out["overlap_comparison"] = {"overlap_agreed": summ(traded[traded.overlap == True]),
                                         "overlap_disagreed": summ(traded[traded.overlap == False]), "all": summ(traded)}
        if log_path: t.to_csv(log_path, index=False)
    out["top_rejects"] = dict(sorted(rej.items(), key=lambda kv: -kv[1])[:6])
    return out

def backtest_csv(path, pair="TEST", payout=C.DEFAULT_PAYOUT, **kw): return backtest(load_csv(path, pair), pair, payout, **kw)

def synthetic(n=6000, seed=7, start=83.0, t0="2026-01-01"):
    g = np.random.default_rng(seed)
    vol = 0.00012*np.exp(np.cumsum(g.normal(0, 0.02, n)).clip(-1.5, 1.5))
    close = start*np.exp(np.cumsum(g.normal(0, 1, n)*vol)); op = np.r_[start, close[:-1]]
    hi = np.maximum(op, close)*(1+np.abs(g.normal(0, .5, n))*vol); lo = np.minimum(op, close)*(1-np.abs(g.normal(0, .5, n))*vol)
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close},
                        index=pd.date_range(t0, periods=n, freq="1min", tz="UTC", name="time"))

if __name__ == "__main__":
    for p in (0.77, 0.80, 0.85, 0.90, 0.95): print(f"breakeven @ {p:.0%}: {breakeven(p):.2%}")
    df = synthetic(); df.to_csv("synthetic_m1.csv")
    print("analyze(last bar):", json.dumps(analyze(df, "USDINR", 0.85), default=str))
    print("analyze @76% payout:", analyze(df, "USDINR", 0.76)["reject"])
    pay = pd.Series(np.random.default_rng(1).uniform(0.77, 0.95, len(df)))
    print(json.dumps(backtest_csv("synthetic_m1.csv", "TEST", pay, use_gate=True, log_path="synthetic_trades.csv"), indent=1))
    print("Random walk => expect ~50% win rate (no edge). Materially higher would indicate lookahead.")
