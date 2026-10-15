"""Backtest every P pattern: n, win rate, vs breakeven (payout 80% → 55.6%; 77% → 56.5%), with and without double confirmation."""
import sys, os, collections, pandas as pd, numpy as np
sys.path.insert(0, "/workspace/otc_bot_v2"); os.environ.update(TELEGRAM_ENABLED="0")
import analyzer as A, patterns as P
from history import wilson_lb, wilson_ub
from bugstrats import ensure_cols
from multiprocessing import Pool
def run(args):
    pair, d = args
    d = d.set_index("time")[["open", "high", "low", "close"]].sort_index(); x = A.compute(d); ensure_cols(x)
    b = P.bars_from(x); idx = x.index; o, c = x.open.values, x.close.values; res = collections.Counter()
    for i in range(30, len(x) - 2):
        hits = P.detect(b, i) + P.p13_on_1m(x, i)
        for k, side, _ in hits:
            hz = P.EXPIRY.get(k, 1)
            if any(idx[i + j] - idx[i + j - 1] != pd.Timedelta("60s") for j in range(1, hz + 1)): continue
            if c[i + hz] == o[i + 1]: continue
            win = (c[i + hz] > o[i + 1]) == (side == "CALL")
            res[(k, "all", win)] += 1
            if P.p_double_confirm(x, i, k, side)[0]: res[(k, "dc", win)] += 1
    return res
if __name__ == "__main__":
    df = pd.read_csv(sys.argv[1], parse_dates=["time"]); df["time"] = pd.to_datetime(df.time, utc=True).dt.floor("min")
    df = df.drop_duplicates(["pair", "time"])
    with Pool(8) as p: rs = p.map(run, list(df.groupby("pair")))
    tot = collections.Counter()
    for r in rs: tot.update(r)
    BE = 1 / 1.8
    print(f"{sys.argv[1]}: {df.pair.nunique()} pairs, {len(df)} bars · breakeven {BE:.1%} (80% payout)")
    print(f"{'key':5} {'n':>6} {'WR':>6} {'WilsonLB':>8} {'vsBE':>6} | {'n dc':>6} {'WR dc':>6}  rule")
    for k in P.NAMES:
        w, l = tot[(k, "all", True)], tot[(k, "all", False)]; n = w + l
        wd, ld = tot[(k, "dc", True)], tot[(k, "dc", False)]; nd = wd + ld
        f = lambda w, n: f"{w/n:6.1%}" if n else "     —"
        print(f"{k:5} {n:6d} {f(w, n)} {(f'{wilson_lb(w, n):8.1%}' if n else '       —')} {(f'{(w/n-BE)*100:+5.1f}' if n else '    —')} | {nd:6d} {f(wd, nd)}  {P.NAMES[k]}")
