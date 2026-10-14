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
        flipped = pm == "direction" and precheck_flip_kind(pc_ok, pc_why) is not None
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
