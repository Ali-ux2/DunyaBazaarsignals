"""Bug-group promotion gate (replaces the old forward-count / warm-up promotion):
  n ≥ GATE_MIN_N paper trades · 90% Wilson LB > break-even + GATE_MARGIN (BE from actual payouts) · max paper drawdown
  < GATE_MAX_DD units · multi-pair setups: no pair > 50% of wins · effect size wins − BE·n ≥ GATE_EFFECT (40 = very strict)
  · multiple-testing correction across the N setups evaluated (Bonferroni default, BH selectable) · not a duplicate
  (P&L correlation > 0.7 with an already promoted setup) · not retired · not blocked (P8/P14/P6b).
Per-pair matrix: a promoted setup posts only on pairs where setup×pair has ≥ 15 trades and Wilson LB ≥ BE.
Retire to shadow: rolling 100 WR < BE − 0.01, 200 trades without a new equity high, or 10 losses in a row."""
import os, math
from datetime import datetime
import numpy as np, pandas as pd
from quant import wilson, binom_p_greater, bh_select
E = os.environ.get
MIN_N = int(E("GATE_MIN_N", "50")); MARGIN = float(E("GATE_MARGIN", "0.02")); MAX_DD = float(E("GATE_MAX_DD", "15"))
PAIR_SHARE = float(E("GATE_PAIR_SHARE", "0.5")); EFFECT = float(E("GATE_EFFECT", "4")); MT = E("BUG_MT", "bonferroni")
ALPHA = float(E("BUG_MT_ALPHA", "0.05")); DUP_CORR = float(E("GATE_DUP_CORR", "0.7"))
MX_MIN_N = int(E("MATRIX_MIN_N", "15")); RET_WIN = int(E("RETIRE_WINDOW", "100")); RET_MARGIN = float(E("RETIRE_MARGIN", "0.01"))
RET_NOHIGH = int(E("RETIRE_NO_HIGH", "200")); RET_LOSSES = int(E("RETIRE_LOSSES", "10"))
Z90 = 1.2816
WINDOW = int(E("BUG_WINDOW", "50")); MIN_LIVE = int(E("BUG_MIN_LIVE", "25"))
def window(b): return [w for w in (b.get("window") or []) if len(w) >= 5]
def gate_rows(b):
    """Rows the gate judges: the rolling window (hist-seeded, rolled forward live) when present, else live history."""
    w = window(b); return w if w else hist(b)
def push_live(b, row, size=None):
    w = b.setdefault("window", []); w.append(list(row[:4]) + ["live"]); del w[:-(size or WINDOW)]
def counts(b):
    w = window(b); live = sum(1 for r in w if r[4] == "live"); return len(w) - live, live

def hist(b): return [h for h in (b.get("hist") or []) if len(h) >= 4]
def be_of(rows, default=0.8):
    pay = (sum(float(r[3]) for r in rows) / len(rows)) if rows else default
    return 1 / (1 + pay)
def equity(rows):
    eq = peak = 0.0; mdd = 0.0; since_high = 0
    for r in rows:
        eq += float(r[3]) if r[2] else -1.0
        if eq > peak: peak = eq; since_high = 0
        else: since_high += 1
        mdd = min(mdd, eq - peak)
    return eq, -mdd, since_high
def pnl_series(rows):
    s = pd.Series([float(r[3]) if r[2] else -1.0 for r in rows], index=pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601"))
    d = s.resample("1D").sum(); return d if (d != 0).sum() >= 10 else s.resample("1h").sum()
def corr(a, b):
    j = pd.concat([a, b], axis=1).fillna(0.0); j = j[(j != 0).any(axis=1)]
    if len(j) < 8 or j.iloc[:, 0].std() == 0 or j.iloc[:, 1].std() == 0: return None
    return float(j.corr().iloc[0, 1])

def evaluate(bugs, blocked=lambda b: False, payout_default=0.8):
    """→ {key: {"pass": bool, "fails": [str], "n", "w", "be", "lb90", ...}} for every setup with paper history."""
    out = {}; tested = {}
    for k, b in bugs.items():
        rows = gate_rows(b); n = len(rows); w = sum(1 for r in rows if r[2]); be = be_of(rows, payout_default)
        _, live_n = counts(b)
        lb90 = wilson(w, n, Z90)[0] if n else 0.0; _, mdd, _ = equity(rows); fails = []
        if b.get("retired"): fails.append("retired (decay)")
        if blocked(b): fails.append("blocked setup")
        if n < MIN_N: fails.append(f"n {n} < {MIN_N}")
        if b.get("window") is not None and live_n < MIN_LIVE: fails.append(f"live {live_n} < {MIN_LIVE} in window")
        if n and lb90 <= be + MARGIN: fails.append(f"90% Wilson LB {lb90:.1%} ≤ BE+{MARGIN:.0%} {be + MARGIN:.1%}")
        if mdd >= MAX_DD: fails.append(f"paper drawdown {mdd:.1f}u ≥ {MAX_DD:g}u")
        if b.get("pair") == "*" and w:
            cnt = {}
            for r in rows:
                if r[2]: cnt[r[1]] = cnt.get(r[1], 0) + 1
            top = max(cnt.items(), key=lambda kv: kv[1])
            if top[1] / w > PAIR_SHARE: fails.append(f"{top[0]} has {top[1] / w:.0%} of wins (> {PAIR_SHARE:.0%})")
        eff = w - be * n
        if eff < EFFECT: fails.append(f"effect {eff:+.1f} wins < {EFFECT:g}")
        out[k] = {"n": n, "w": w, "be": be, "lb90": lb90, "mdd": mdd, "effect": eff, "fails": fails}
        if n >= MIN_N: tested[k] = binom_p_greater(w, n, be)
    N = max(1, len(tested))
    if MT == "bh": sig = bh_select(tested, ALPHA)
    elif MT == "off": sig = set(tested)
    else: sig = {k for k, p in tested.items() if p < ALPHA / N}
    for k, r in out.items():
        if k in tested and k not in sig: r["fails"].append(f"multiple testing ({MT}, N={N}): p={tested[k]:.3g}")
    # duplicates: highest effect first; reject a setup whose P&L correlates > DUP_CORR with an already accepted one
    acc = []
    for k in sorted([k for k, r in out.items() if not r["fails"]], key=lambda k: -out[k]["effect"]):
        try:
            sk = pnl_series(gate_rows(bugs[k])); dup = None
            for a in acc:
                cc = corr(sk, pnl_series(gate_rows(bugs[a])))
                if cc is not None and cc > DUP_CORR: dup = (a, cc); break
            if dup: out[k]["fails"].append(f"duplicate of {dup[0]} (P&L corr {dup[1]:.2f})")
            else: acc.append(k)
        except Exception as e: out[k]["fails"].append(f"corr check error {e}")
    for r in out.values(): r["pass"] = not r["fails"]
    return out

def matrix(b):
    """{pair: (w, n, be, lb)} per setup × pair."""
    by = {}
    for r in gate_rows(b): by.setdefault(r[1], []).append(r)
    out = {}
    for p, rows in by.items():
        n = len(rows); w = sum(1 for r in rows if r[2]); be = be_of(rows); out[p] = (w, n, be, wilson(w, n)[0])
    return out
def pair_ok(b, pair):
    m = matrix(b).get(pair)
    return bool(m) and m[1] >= MX_MIN_N and m[3] >= m[2]
def retire_reason(b):
    rows = hist(b)
    if not rows: return None
    tail = rows[-RET_LOSSES:]
    if len(tail) >= RET_LOSSES and not any(r[2] for r in tail): return f"{RET_LOSSES} losses in a row"
    if len(rows) >= RET_WIN:
        last = rows[-RET_WIN:]; wr = sum(1 for r in last if r[2]) / RET_WIN; be = be_of(last)
        if wr < be - RET_MARGIN: return f"last {RET_WIN} WR {wr:.1%} < BE−{RET_MARGIN:.0%} {be - RET_MARGIN:.1%}"
    _, _, since = equity(rows)
    if since >= RET_NOHIGH: return f"{since} trades without a new equity high"
    return None
