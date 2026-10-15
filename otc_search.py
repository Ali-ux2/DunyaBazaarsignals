"""Config search on the real OTC window: train = first 70% of the date range, holdout = last 30%.
Candidates: (a) every ablation config incl. volume tiers; (b) strategy/pattern whitelists chosen on train (members with train WR ≥ BE, n ≥ 10),
traded through the live constraints (one trade at a time, 3-min gap, ≤6/h, 1 per pair per 10 min, entry next bar, DC on/off)."""
import sys, os, json, math, collections, itertools, logging
sys.path.insert(0, "/workspace/otc_bot_v2"); os.environ.update(TELEGRAM_ENABLED="0"); logging.disable(logging.WARNING)
import pandas as pd, numpy as np
import config as C, analyzer as A, ablation as AB, patterns as P, bot
from history import wilson_lb
from bugstrats import ensure_cols, double_confirm
meta = json.load(open("otc_data/otc_meta.json")); df = pd.read_csv("otc_data/otc_live_window.csv", parse_dates=["time"]); df["time"] = pd.to_datetime(df.time, utc=True)
pays = {p: m["payout"] / 100 for p, m in meta.items() if m["payout"] and m["payout"] >= 77}; df = df[df.pair.isin(pays)]
PAY = float(np.mean(list(pays.values()))); BE = 1 / (1 + PAY)
t0, t1 = df.time.min(), df.time.max(); cut = t0 + (t1 - t0) * 0.7
print(f"train {t0} → {cut} · holdout {cut} → {t1} · BE {BE:.1%}")
# every member fire: (t, pair, key, side, win(entry bar i+2 like live), dc)
det = {k: v for k, v in bot.hist_detectors().items()}; fires = []
for p, g in df.groupby("pair"):
    x = A.compute(g.set_index("time")[["open", "high", "low", "close"]]); ensure_cols(x); b = P.bars_from(x); o, c = x.open.values, x.close.values
    for i in range(60, len(x) - 3):
        hits = [(k, fn(x, i)) for k, fn in det.items() if not k.startswith("P")]
        hits += [(k, d) for k, d, _ in P.detect(b, i) + P.p13_on_1m(x, i)]
        for k, d in hits:
            if not d: continue
            hz = P.EXPIRY.get(k, 1); e = i + 2                                    # live: scan :02 → entry next :00 = bar i+2
            if e + hz - 1 >= len(x) or c[e + hz - 1] == o[e]: continue
            m, s, _ = double_confirm(x, i, d); dc = (P.p_double_confirm(x, i, k, d)[0] if k.startswith("P") else (m and s))
            fires.append((x.index[i], p, k, d, (c[e + hz - 1] > o[e]) == (d == "CALL"), dc, pays[p]))
F = pd.DataFrame(fires, columns=["t", "pair", "k", "d", "win", "dc", "pay"]); tr, ho = F[F.t < cut], F[F.t >= cut]
def run(sub, keys, dc):
    s = sub[sub.k.isin(keys)]
    if dc: s = s[s.dc]
    s = s.sort_values("t"); last = None; pl = {}; sent = []; W = L = 0; hours = collections.Counter()
    for t, grp in s.groupby("t"):
        now = t + pd.Timedelta(minutes=1)
        if last is not None and now - last < pd.Timedelta(minutes=3): continue
        h = now.floor("h")
        if hours[h] >= 6: continue
        grp = grp[[not (q in pl and now - pl[q] < pd.Timedelta(minutes=10)) for q in grp.pair]]
        if grp.empty: continue
        r = grp.iloc[0]; last = now; pl[r.pair] = now; hours[h] += 1; W += r.win; L += not r.win
    n = W + L; span_h = max(1e-9, (sub.t.max() - sub.t.min()).total_seconds() / 3600)
    full = [h for h in hours] ; return n, W, n / span_h, (sum(1 for v in hours.values() if v >= 4) / max(1, math.floor(span_h)))
stats = tr.groupby("k").win.agg(["sum", "count"])
good = [k for k, r in stats.iterrows() if r["count"] >= 10 and r["sum"] / r["count"] >= BE]
print("train members ≥ BE (n≥10):", {k: f"{int(stats.loc[k,'sum'])}/{int(stats.loc[k,'count'])}" for k in good})
out = []
for size in range(1, len(good) + 1):
    for keys in itertools.combinations(good, size):
        for dc in (True, False):
            n, w, ph, p4 = run(tr, keys, dc)
            if n >= 10 and w / n >= BE: out.append((p4, ph, keys, dc, n, w))
out.sort(reverse=True); print(f"\n{len(out)} whitelist configs ≥ BE on train; top 5 by share of hours ≥4 then signals/h:")
for p4, ph, keys, dc, n, w in out[:5]:
    hn, hw, hph, hp4 = run(ho, keys, dc)
    print(f"  {'+'.join(keys)} DC {'on' if dc else 'off'} · train n {n} WR {w/n:.0%} {ph:.1f}/h ≥4h {p4:.0%} · HOLDOUT n {hn} WR {(hw/hn if hn else 0):.0%} "
          f"LB {(wilson_lb(hw, hn) if hn else 0):.0%} {ph and hph:.1f}/h ≥4h {hp4:.0%} · validated: {bool(hn) and wilson_lb(hw, hn) >= BE}")
# decision-path configs on the holdout
rows = []
for p, g in df.groupby("pair"): rows += AB.stage1(p, g.set_index("time"), pay=PAY)
rho = [r for r in rows if r["t"] >= cut]; rtr = [r for r in rows if r["t"] < cut]
print("\nDecision-path configs: train / holdout")
for name, cfg in AB.GRID:
    a, b_ = AB.simulate(rtr, cfg, PAY), AB.simulate(rho, cfg, PAY)
    print(f"  {name:28} train n {a['n']:3d} WR {a['wr']:5.1%} · holdout n {b_['n']:3d} WR {b_['wr']:5.1%} LB {(wilson_lb(round(b_['wr']*b_['n']), b_['n']) if b_['n'] else 0):5.1%} {b_['per_hour']:.1f}/h")
