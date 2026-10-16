"""Quant guards: correlation cap, hour block, ATR throttle, Bayesian pause, decay retirement, BH promotion,
regime labels, reproducibility + features.jsonl, walk-forward guard, weekly report sections."""
import os, tempfile, json
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", HIST_GATE="off", BLACKOUT="off",
                  NEWS_FILTER="off", DOUBLE_CONFIRM="off", PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003",
                  SHADOW_CHANNEL_ID="-1004", SHADOW_RELAXED="1", GROUP_FILTER="all", TELEGRAM_ADMIN_ID="42")
import numpy as np, pandas as pd, config as C, bot, analyzer as A, quant as Q, audit as AU, feedback as FB, adminjobs as AJ
from datetime import timedelta

# stats helpers
assert abs(Q.beta_prob_above(10, 10, 0.5) - 0.5) < 0.01
assert Q.beta_prob_above(10 + 5, 10 + 20, 0.55) < 0.1 and Q.beta_prob_above(10 + 30, 10 + 10, 0.55) > 0.5
assert abs(Q.binom_p_greater(5, 10, 0.5) - 0.623) < 0.01
assert Q.bh_select({"a": 0.001, "b": 0.02, "c": 0.5}, 0.1) == {"a", "b"} and Q.bh_select({"a": 0.2, "b": 0.3}, 0.1) == set()
be = lambda k: 0.55
assert Q.promoted_set({"good": (80, 100), "lucky": (13, 20)}, be) == {"good"}, "BH rejects small lucky samples"
Q.MT_METHOD = "wilson"; assert Q.promoted_set({"good": (80, 100), "meh": (60, 100)}, be) == {"good"}; Q.MT_METHOD = "bh"

# correlation clusters
r = np.random.default_rng(1); base = np.cumsum(r.normal(0, 1, 200)); idx = pd.date_range("2026-10-15", periods=200, freq="1min")
cl = Q.corr_clusters({"A": pd.Series(100 + base + r.normal(0, .05, 200), idx), "B": pd.Series(100 + base + r.normal(0, .05, 200), idx),
                      "C": pd.Series(100 + np.cumsum(r.normal(0, 1, 200)), idx)})
assert cl["A"] == cl["B"] and cl["C"] != cl["A"], cl

# regime + ATR pct
x = pd.DataFrame({"adx": [30.0], "er": [0.1], "atr": [1.0]})
assert Q.regime_label(x, 95) == "volatile" and Q.regime_label(x, 10) == "quiet" and Q.regime_label(x, 50) == "trending"
x2 = pd.DataFrame({"adx": [10.0], "er": [0.1]}); assert Q.regime_label(x2, 50) == "ranging"
ref = np.sort(np.arange(1, 1001, dtype=float)); assert Q.atr_percentile(100, ref) == 10 and Q.atr_percentile(950, ref) == 95 and Q.atr_percentile(5, ref[:50]) is None

# bot integration
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADMIN = []; b.tg.admin = lambda t: ADMIN.append(t)
T = pd.Timestamp("2026-10-15 10:00:02", tz="UTC"); b.d.now = lambda: T
# Bayesian pause + resume
for i in range(25): b.q_record("P8", i % 5 == 0, T)
assert "P8" in b.q["paused"] and any("paused" in a for a in ADMIN)
assert "paused" in b.q_block("USDBRL", ["P8"], T)
for i in range(60): b.q_record("P8", True, T)
assert "P8" not in b.q["paused"]
# decay retirement (old 60% → last 14 days 30%)
old = T - timedelta(days=30)
b.q["pat"]["P3"] = [[(old + timedelta(minutes=i)).isoformat(), 1 if i % 10 < 6 else 0] for i in range(100)]
for i in range(40): b.q_record("P3", i % 10 < 3, T)
assert "P3" in b.q["retired"] and "retired" in b.q_block("USDBRL", ["P3"], T)
ADMIN.clear(); b.block_cmd(["list"]); assert "Retired" in ADMIN[-1] and "P3" in ADMIN[-1]
b.block_cmd(["remove", "p3"]); assert "P3" not in b.q["retired"]
# hour block from stored results (30 VIP losses-heavy at 13:00 UTC+3 = 10:00 UTC)
for i in range(35): FB.log("vip_result", "x", pair="USDBRL", result="WIN" if i < 8 else "LOSS", now=T - timedelta(days=1 + i // 5))
b._hours["at"] = 0; hb = b.q_hour_blocked(T); h = C.local(T).hour
assert h in hb and "blocked" in b.q_block("USDBRL", [], T)
b.hours_cmd(["unblock", str(h)]); assert h not in b.q_hour_blocked(T) and b.q_block("USDBRL", [], T) is None
assert "Guards:" in b.status()
# ATR throttle needs ≥3 days history; skipped otherwise
b.hist.store.coverage_days = lambda p: 1.0; assert b.q_atr_pct("USDBRL", 1.0) is None
b.hist.store.coverage_days = lambda p: 10.0
b._atr_ref["USDBRL"] = (1e18, np.sort(np.linspace(1, 100, 1000)))
xa = pd.DataFrame({"atr": [99.5]}); assert "ATR percentile" in b.q_block("USDBRL", [], T, xa)
xa = pd.DataFrame({"atr": [50.0]}); assert b.q_block("USDBRL", [], T, xa) is None
# correlation cap: second pair in same cluster, same minute, same channel → no channel left
b._corr = {"at": 1e18, "map": {"USDBRL": "L", "USDMXN": "L", "USDINR": "USDINR"}}
E = T.replace(second=0) + timedelta(minutes=1); ch = [("vip", "-1002")]
assert b.q_corr_take("USDBRL", E, ch) == ch and b.q_corr_take("USDMXN", E, ch) == [] and b.q_corr_take("USDINR", E, ch) == ch
assert b.q_corr_take("USDMXN", E + timedelta(minutes=1), ch) == ch
b.corrcap_cmd(["off"]); assert b.q_corr_take("USDMXN", E, ch) == ch; b.corrcap_cmd(["on"])
# features + reproducibility
r = np.random.default_rng(5); c = 1.1 + np.cumsum(r.normal(0, 1e-4, 200)); o = np.r_[c[0], c[:-1]]
df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c), index=pd.date_range("2026-10-15 06:40", periods=200, freq="1min", tz="UTC"))
xx = A.compute(df); t = {"id": "DBS-20261015-ABCD", "pair": "USDBRL", "signal": "CALL", "confidence": 70, "payout": 0.9, "strategies": {"A": (1, "x"), "B": (-1, "y")}}
f = b.q_features(t, xx, df, "ENS:A")
assert f["analyzer_version"] == Q.ANALYZER_VERSION and len(f["candles_sha256"]) == 64 and f["votes"] == {"A": 1, "B": -1} and f["regime"] in ("trending", "ranging", "volatile", "quiet")
assert Q.candles_sha(df) == Q.candles_sha(df.copy()) and Q.candles_sha(df) != Q.candles_sha(df.iloc[:-1])
b.q_feature_log(dict(f, type="signal", channel="vip")); b.q_outcome(t, "WIN", "vip")
rows = [json.loads(l) for l in open(os.path.join(SD, "features.jsonl"))]; assert rows[0]["config_version"] and rows[1]["result"] == "WIN"
# shadow signal gets regime/atr tag + features audit event
b.dc_ok = lambda *a, **k: True; b.payout_gate = lambda p: (True, 0.9, "ok")
bot.analyze_relaxed_ensemble = lambda d_, pair, **k: {"signal": "CALL", "payout": .9, "confidence": 60, "regime": "trend", "agree": ["A"]}
b.tg.group = lambda *a, **k: None; b.shadow_eval(df, xx, C.PAIRS[0], 0.9, 0, T)
st = b.shadow_open[-1]; assert "regime_label" in st and "features" in [e["event"] for e in AU.trail(st["id"])]
fe = [e for e in AU.trail(st["id"]) if e["event"] == "features"][0]["details"]; assert fe["analyzer_version"] and fe["candles_sha256"]
# weekly report sections
txt = AJ.weekly_text(T + timedelta(hours=1)); assert "Seasonality per channel" in txt and "By regime" in txt and "by weekday" in txt, txt
# walk-forward guard
rowsW = [{"t": pd.Timestamp("2026-10-01") + timedelta(minutes=i)} for i in range(1500)]
def tbl(rr, pay, news):
    late = rr[0]["t"] > pd.Timestamp("2026-10-01 17:00")
    return [("baseline", {}, {"n": 100, "exp": 0.0}), ("cand", {"dc": False}, {"n": 100, "exp": -0.1 if late else 0.2})]
prop = lambda res: max(((n, c, m, {}) for n, c, m in res), key=lambda r: r[2]["exp"])
ok, why, _ = Q.walk_forward_ok(rowsW, tbl, prop, 0.8, []); assert not ok and "held-out" in why, "overfit rejected"
def tbl2(rr, pay, news): return [("baseline", {}, {"n": 100, "exp": 0.0}), ("cand", {"dc": False}, {"n": 100, "exp": 0.1})]
assert Q.walk_forward_ok(rowsW, tbl2, prop, 0.8, [])[0]
assert not Q.walk_forward_ok(rowsW[:500], tbl2, prop, 0.8, [])[0]
# guards never raise
b.hist.store.get = lambda p: (_ for _ in ()).throw(RuntimeError("x")); b._atr_ref.clear(); assert b.q_atr_pct("USDBRL", 1.0) is None
print("test_quant OK")
