"""Holdout validation for the bug pattern scanner. Discovery runs ONLY on the oldest HO_DISCOVERY_FRAC (70%) of a pair's
stored candles; each discovered pattern must then beat break-even on the newest 30% (never seen by discovery) with at
least HO_MIN_N samples before it may enter paper trading. Rejections are returned so the caller can log them."""
import os
import numpy as np, pandas as pd
from analyzer import bug_scan, bug_patterns
FRAC = float(os.environ.get("HO_DISCOVERY_FRAC", "0.70")); MIN_N = int(os.environ.get("HO_MIN_N", "30"))
ON = os.environ.get("HOLDOUT", "on") == "on"; MIN_BARS = int(os.environ.get("HO_MIN_BARS", "300"))

def split(x, frac=None):
    cut = int(len(x) * (frac or FRAC)); return x.iloc[:cut], x.iloc[cut:]
def evaluate(xh, key, rule):
    """(hits, n) of a discovered rule on holdout bars xh (same outcome definition as bug_scan)."""
    if rule is None: return 0, 0
    pred, d = rule; o, c = xh.open.values, xh.close.values; col = np.sign(c - o).astype(int); nxt = np.r_[col[1:], 0]
    for k, _, mask, p in bug_patterns(xh):
        if k != key: continue
        mask = mask.copy()
        if pred != 0: mask[-1] = False
        if pred == 0: predicted = np.full(len(col), d)[mask]; actual = col[mask]
        else: predicted = ((-col if pred is None else np.full(len(col), pred)) * d)[mask]; actual = nxt[mask]
        ok = (actual != 0) & (predicted != 0); return int((predicted[ok] == actual[ok]).sum()), int(ok.sum())
    return 0, 0
def scan(x, be, min_samples=None):
    """→ (accepted [(key, desc, rule, rate_disc, n_disc, rate_hold, n_hold)], rejected [(key, desc, why)]).
    Too little data for a split → nothing accepted (fail closed)."""
    if not ON: return [(k, d, r, rt, n, None, 0) for k, d, r, rt, n in bug_scan(x, min_samples)], []
    if len(x) < MIN_BARS: return [], []
    xd, xh = split(x); acc, rej = [], []
    for key, desc, rule, rate, n in bug_scan(xd, min_samples):
        if rule is None: rej.append((key, desc, "info-only pattern (no rule)")); continue
        h, nh = evaluate(xh, key, rule)
        if nh < MIN_N: rej.append((key, desc, f"holdout only {nh} samples (< {MIN_N})")); continue
        rh = h / nh
        if rh <= be: rej.append((key, desc, f"holdout {h}/{nh} = {rh:.1%} ≤ break-even {be:.1%}")); continue
        acc.append((key, desc, rule, rate, n, round(rh, 3), nh))
    return acc, rej

def matches(x, key, rule):
    """[(ts_iso, win)] for every historical match of a rule in x (outcome = next candle, same definition as bug_scan)."""
    if rule is None: return []
    pred, d = rule; o, c = x.open.values, x.close.values; col = np.sign(c - o).astype(int); nxt = np.r_[col[1:], 0]
    for k, _, mask, p in bug_patterns(x):
        if k != key: continue
        mask = mask.copy()
        if pred != 0: mask[-1] = False
        if pred == 0: predicted, actual, ts = np.full(len(col), d), col, x.index
        else: predicted, actual, ts = (-col if pred is None else np.full(len(col), pred)) * d, nxt, x.index + pd.Timedelta("60s")
        idx = np.where(mask & (actual != 0) & (predicted != 0))[0]
        return [(ts[i].isoformat(), bool(predicted[i] == actual[i])) for i in idx]
    return []
