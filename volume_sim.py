import sys, os, pickle, json, pandas as pd
sys.path.insert(0, "/workspace/otc_bot_v2"); os.environ.update(TELEGRAM_ENABLED="0")
import ablation as AB
rows = pickle.load(open(sys.argv[1], "rb"))
ev = [(pd.Timestamp(e["date"]).tz_convert("UTC"), e["country"]) for e in json.load(open("/workspace/sim/ff_week.json")) if e.get("impact") == "High"]
cfgs = [("volume OFF (T1+T2)", dict(volume=False)), ("volume ON (default)", {}), ("volume ON, level 4 strict", dict(level=4)),
        ("volume ON, no DC (reference only)", dict(dc=False))]
for pay in (0.80, 0.85):
    print(f"\npayout {pay:.0%} (breakeven {1/(1+pay):.1%})")
    for name, cfg in cfgs:
        m = AB.simulate(rows, cfg, pay, ev); t = m["tiers"]
        f = lambda k: f"T{k} {t[k][0]}/{t[k][1]}" + (f" {t[k][0]/t[k][1]:.1%}" if t[k][1] else "")
        print(f"{name:34} n {m['n']:4d} WR {m['wr']:.1%} E {m['exp']:+.3f}/sig total {m['total']:+.1f} · {m['per_hour']:.2f}/h · "
              f"≥4 {m['pct4']:.0%} · 4–6 {m['pct46']:.0%} · dist {m['dist']} · {f(1)} · {f(2)} · {f(3)}")
