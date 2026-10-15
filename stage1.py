"""Per pair: every closed bar → strict (L3, L4) ensemble decisions, fill-pool decision, double-confirm (strict/fill), member votes, outcome.
Indicators are causal (truncated-frame tests), so compute() runs once on the full series."""
import sys, os, pickle, numpy as np, pandas as pd
sys.path.insert(0, "/workspace/otc_bot_v2"); os.environ.update(TELEGRAM_ENABLED="0")
import config as C, analyzer as A
from bugstrats import double_confirm, ensure_cols
from multiprocessing import Pool
PAY = 0.80
def run(args):
    pair, d = args
    d = d.set_index("time")[["open", "high", "low", "close"]].sort_index()
    x = A.compute(d); ensure_cols(x); rows = []
    o, c = x.open.values, x.close.values
    for i in range(C.MIN_BARS, len(x) - 1):
        if (x.index[i + 1] - x.index[i]) != pd.Timedelta("60s"): continue      # gap → no next-bar outcome / gap rejection
        if i + 2 >= len(x) or (x.index[i + 2] - x.index[i + 1]) != pd.Timedelta("60s"): continue
        a_ = float(x.atr.iloc[i]) or 1e-12
        rec = {"t": x.index[i], "pair": pair, "out": int(np.sign(c[i + 1] - o[i + 1])),
               "out2": int(np.sign(c[i + 2] - o[i + 2])), "b1": float(c[i + 1] - o[i + 1]) / a_}   # live: entry bar i+2, :57 sees bar i+1
        for L in (3, 4):
            with A._overrides(A.level_overrides(L)): s = A.ensemble(d.iloc[:i + 1], pair, payout=PAY, x=x, i=i, use_gate=False)
            if s.get("signal"):
                m, st, _ = double_confirm(x, i, s["signal"]); mf, stf, _ = double_confirm(x, i, s["signal"], fill=True)
                rec[f"L{L}"] = (s["signal"], tuple(s["agree"]), s["confidence"], m and st, mf and stf)
            if L == 3:
                rec["votes"] = {k: v[0] for k, v in (s.get("strategies") or {}).items() if v and k != "D"}
        rows.append(rec)
    return rows
if __name__ == "__main__":
    df = pd.read_csv(sys.argv[1], parse_dates=["time"]); df["time"] = pd.to_datetime(df.time, utc=True)
    df["time"] = df.time.dt.floor("min"); df = df.drop_duplicates(["pair", "time"], keep="first")
    with Pool(min(8, os.cpu_count())) as p: res = p.map(run, list(df.groupby("pair")))
    pickle.dump([r for rr in res for r in rr], open(sys.argv[2], "wb")); print("rows", sum(map(len, res)))
