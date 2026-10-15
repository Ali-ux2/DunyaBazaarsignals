"""All evaluations on the real Quotex OTC live-window snapshot (sim/otc_data). Payout = live per-pair payout; pairs < 77% excluded."""
import sys, os, json, collections, logging
sys.path.insert(0, "/workspace/otc_bot_v2"); os.environ.update(TELEGRAM_ENABLED="0"); logging.disable(logging.WARNING)
import pandas as pd, numpy as np
import config as C, analyzer as A, ablation as AB, patterns as P, bot
from history import wilson_lb
from bugstrats import ensure_cols
meta = json.load(open("otc_data/otc_meta.json")); df = pd.read_csv("otc_data/otc_live_window.csv", parse_dates=["time"])
df["time"] = pd.to_datetime(df.time, utc=True)
pays = {p: m["payout"] / 100 for p, m in meta.items() if m["payout"] and m["payout"] >= 77}
df = df[df.pair.isin(pays)]; PAY = float(np.mean(list(pays.values()))); BE = 1 / (1 + PAY)
print(f"OTC data: {df.pair.nunique()} pairs ({', '.join(sorted(pays))}), {len(df)} bars, {df.time.min()} → {df.time.max()} UTC")
print(f"payouts: {', '.join(f'{p} {v:.0%}' for p, v in sorted(pays.items()))} · mean {PAY:.1%} → breakeven {BE:.1%} (excluded <77%: "
      f"{', '.join(p for p, m in meta.items() if p not in pays)})")
row = lambda name, w, n: f"  {name:34} n {n:5d} WR {(w/n if n else 0):6.1%} LB {(wilson_lb(w, n) if n else 0):6.1%} {'≥' if n and wilson_lb(w, n) >= BE else '<'} BE"
# --- per-strategy (A–F ensemble members at level 3, and the replayable bug strategies), outcome = next bar ---
print("\nStrategies (each fire, next-bar outcome):")
det = bot.hist_detectors(); res = collections.Counter()
for p, g in df.groupby("pair"):
    x = A.compute(g.set_index("time")[["open", "high", "low", "close"]]); ensure_cols(x); o, c = x.open.values, x.close.values
    for k, fn in det.items():
        if k.startswith("P"): continue
        for i in range(60, len(x) - 1):
            try: d = fn(x, i)
            except Exception: d = None
            if d and c[i + 1] != o[i + 1]: res[(k, (c[i + 1] > o[i + 1]) == (d == "CALL"))] += 1
for k in sorted({k for k, _ in res}): print(row(k, res[(k, True)], res[(k, True)] + res[(k, False)]))
# --- patterns ---
print("\nPatterns P (next-bar outcome; P13 2-minute):")
pr = collections.Counter()
for p, g in df.groupby("pair"):
    x = A.compute(g.set_index("time")[["open", "high", "low", "close"]]); ensure_cols(x); b = P.bars_from(x); o, c = x.open.values, x.close.values
    for i in range(30, len(x) - 2):
        for k, side, _ in P.detect(b, i) + P.p13_on_1m(x, i):
            hz = P.EXPIRY.get(k, 1)
            if c[i + hz] == o[i + 1]: continue
            win = (c[i + hz] > o[i + 1]) == (side == "CALL"); pr[(k, "all", win)] += 1
            if P.p_double_confirm(x, i, k, side)[0]: pr[(k, "dc", win)] += 1
for k in P.NAMES:
    w, l = pr[(k, "all", True)], pr[(k, "all", False)]; wd, ld = pr[(k, "dc", True)], pr[(k, "dc", False)]
    print(row(k, w, w + l) + f" · with DC n {wd+ld} WR {(wd/(wd+l if False else wd+ld) if wd+ld else 0):.1%}")
# --- ablation + volume (decision path) ---
rows = []
for p, g in df.groupby("pair"): rows += AB.stage1(p, g.set_index("time"), pay=PAY)
print(f"\nDecision-path ablation (payout {PAY:.0%}; history gate needs ≥2 days of data, so Tier 1 cannot fire in 6 h):")
res = AB.table(rows, PAY, []); print(AB.fmt(res))
for name, cfg in (("volume OFF", dict(volume=False)), ("volume ON (default)", {})):
    m = AB.simulate(rows, cfg, PAY, []); t = m["tiers"]
    print(f"  {name:20} n {m['n']} WR {m['wr']:.1%} E {m['exp']:+.3f} · {m['per_hour']:.2f}/h · 4–6/h in {m['pct46']:.0%} of {m['open_hours']} h · dist {m['dist']} · "
          + " · ".join(f"T{k} {t[k][0]}/{t[k][1]}" for k in (1, 2, 3)))
