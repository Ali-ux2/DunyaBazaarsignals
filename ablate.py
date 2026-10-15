import sys, os, pickle, json, pandas as pd
sys.path.insert(0, "/workspace/otc_bot_v2"); os.environ.update(TELEGRAM_ENABLED="0")
import ablation as AB
rows = pickle.load(open(sys.argv[1], "rb"))
ev = [(pd.Timestamp(e["date"]).tz_convert("UTC"), e["country"]) for e in json.load(open("/workspace/sim/ff_week.json")) if e.get("impact") == "High"]
for pay in (0.80, 0.85):
    res = AB.table(rows, pay, ev); print(f"\npayout {pay:.0%}\n" + AB.fmt(res)); print("proposal:", AB.propose(res)[:1], AB.propose(res)[3])
