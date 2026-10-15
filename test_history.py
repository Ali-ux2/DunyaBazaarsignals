"""History gate: store merge/trim, bounded backfill, occurrence replay (no lookahead, incremental), thresholds (n, Wilson LB + margin,
7d veto, coverage), gate on ensemble/bug/shadow → admin 'filtered by history' + paper-tracked, card line, /stats count, /history + menu."""
import os, tempfile, json
os.environ.update(DOUBLE_CONFIRM="off", NEWS_FILTER="off", BLACKOUT="off", STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", HIST_GATE="all",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", GROUP_FILTER="all")
import numpy as np, pandas as pd, config as C, bot, analyzer as A
from history import HistGate, CandleStore, wilson_lb
def walk(n, seed, start="2026-09-10"):
    r = np.random.default_rng(seed); c = 1.1 + np.cumsum(r.normal(0, 1e-4, n)); o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c), index=pd.date_range(start, periods=n, freq="1min", tz="UTC"))
# store
st = CandleStore(); d = walk(3000, 1); st.add("P", d.iloc[:2000]); st.add("P", d.iloc[1500:]); assert len(st.get("P")) == 3000
st.flush(force=True); assert len(CandleStore().get("P")) == 3000; big = walk(31 * 1440 + 100, 2); st.add("Q", big); assert st.coverage_days("Q") <= 30.01
# bounded backfill: one call per pair per day, daily cap
C.HIST_BACKFILL_MAX_REQ = 2; g = HistGate(st); calls = []
f = lambda p, n: (calls.append(p), walk(100, 3))[1]
for _ in range(5): g.backfill_step(["X", "Y", "Z"], f, "2026-10-15")
assert calls == ["X", "Y"], calls
g.backfill_step(["X"], f, "2026-10-16"); assert calls[-1] == "X"
# occurrences: planted detector (every 10th bar → CALL) and next bar forced green → 100% wins; no lookahead; incremental
d = walk(12 * 1440, 4, "2026-10-01"); o = d.open.values.copy(); c = d.close.values.copy()
for i in range(70, len(d) - 1, 10): c[i + 1] = o[i + 1] + 2e-4
d["close"] = c; d["high"] = np.maximum(d.high, c); g2 = HistGate(CandleStore(tempfile.mkdtemp())); g2.store.add("P", d)
det = lambda x, i: "CALL" if i % 10 == 0 else None
now = d.index[-1] + pd.Timedelta("62s")
ok, why, line = g2.check("P", [("T", det)], A.compute, now, 0.9); print("planted:", ok, why, line); assert ok and "won (100%)" in line
n1 = len(g2.occ[("P", "T")]["rows"]); g2.check("P", [("T", det)], A.compute, now, 0.9); assert len(g2.occ[("P", "T")]["rows"]) == n1, "incremental, no double count"
assert all(t < now - pd.Timedelta(minutes=1) for t, _ in g2.occ[("P", "T")]["rows"]), "no lookahead"
# thresholds
dec = g2.decide
assert not dec({"n30": 10, "w30": 10, "n7": 0, "w7": 0}, .9, 30)[0], "n<15"
assert not dec({"n30": 40, "w30": 22, "n7": 0, "w7": 0}, .9, 30)[0], "LB too low"
assert dec({"n30": 40, "w30": 32, "n7": 0, "w7": 0}, .9, 30)[0]
assert not dec({"n30": 40, "w30": 32, "n7": 6, "w7": 2}, .9, 30)[0], "7d veto"
assert not dec({"n30": 40, "w30": 32, "n7": 0, "w7": 0}, .9, 3)[0], "coverage"
assert abs(wilson_lb(17, 23) - 0.57) < 0.03
# bot integration
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM, GRP = [], []
b.tg.admin = lambda t: ADM.append(t); b.tg.group = lambda t, chats=None: GRP.append(t); b.tg.group_photo = lambda *a, **k: False
P = list(C.PAIRS)[0]; T = pd.Timestamp("2026-10-15 10:00:02", tz="UTC"); b.d.now = lambda: T
xx = A.compute(walk(200, 5))
# not enough history → bug filtered (admin only), still paper-tracked
C.VERBOSE = True; s = b.bug_fire("*:B6_14", P, "PUT", "t", T, .9, x=xx); C.VERBOSE = False; assert s["silent"] and any("Filtered by history" in m and "not enough history" in m for m in ADM)
assert not GRP and b.hist_day["filtered"] == 1 and s in b.bug_sigs
# tick strategy: no replayable history → filtered
b.bug_last_pair.clear(); b.bug_last_pat.clear(); C.VERBOSE = True; s = b.bug_fire("*:B1", list(C.PAIRS)[1], "PUT", "t", T, .9, kind="now", entry_price=1.0, x=xx); C.VERBOSE = False
assert s["silent"] and "no replayable" in ADM[-1]
# gate passes (stub) → card shows the history line
b.hist.check = lambda p, nd, comp, now, pay: (True, "ok", "seen 23× in 30d, 17 won (74%)")
b.bug_last_pair.clear(); b.bug_last_pat.clear(); s = b.bug_fire("*:B6_14", list(C.PAIRS)[2], "PUT", "t", T, .9, x=xx)
assert not s["silent"] and GRP and not any("seen" in t or "history" in t for t in GRP), GRP[-1:]
assert any("📚 seen 23× in 30d, 17 won (74%)" in m for m in ADM), "history only in admin"
# ensemble: filtered → paper ENS + admin, nothing posted; passed → card has 📚 line
b.hist.check = lambda p, nd, comp, now, pay: (False, "only 4 past occurrences", "seen 4× in 30d, 3 won (75%)")
sig = {"pair": P, "signal": "CALL", "agree": ["B", "C"], "payout": .9, "atr": 1e-4, "confidence": 70, "strategies": {"B": ("CALL", .8), "C": ("CALL", .9)}}
ok, why, line = b.hist_gate(P, ["B", "C"], T, .9, "all"); assert not ok
C.HIST_GATE = "bugs"; assert b.hist_gate(P, ["B", "C"], T, .9, "all")[0], "bugs mode leaves strategies alone"; C.HIST_GATE = "all"
assert not b.hist_gate(P, ["D"], T, .9, "all")[0], "D-only not replayable"
# /stats count + /history + menu
assert "filtered by history today" in b.summary_text()
b.command("/history off"); assert C.HIST_GATE == "off" and "OFF" in ADM[-1]; assert b.hist_gate(P, ["B"], T, .9, "all")[0]
b.command("/history all"); assert C.HIST_GATE == "all"
_, rows = b.menu_view("main"); assert any(d == "m:history" for r in rows for _, d in r); _, rows = b.menu_view("history"); assert any(d == "q:/history off" for r in rows for _, d in r)
# ---------- tiers ----------
import math
from history import HistGate as HG
assert HG.fill_ok({}, .9)[0] and HG.fill_ok({"n30": 30, "w30": 17}, .9)[0]
assert not HG.fill_ok({"n30": 40, "w30": 10}, .9)[0], "clearly negative"
assert not HG.fill_ok({"n30": 20, "w30": 9}, .9)[0], "below BE with n>=15"
T40 = pd.Timestamp("2026-10-15 10:40:02", tz="UTC"); T20 = pd.Timestamp("2026-10-15 10:20:02", tz="UTC")
T01 = pd.Timestamp("2026-10-15 10:01:02", tz="UTC"); T05 = pd.Timestamp("2026-10-15 10:05:02", tz="UTC")
b.tier_sent = []; assert not b.fill_due(T01) and b.fill_due(T05) and b.fill_due(T20) and b.fill_due(T40), "paced: 1 per 15-min slot after a 3-min Tier-1 head start"
b.tier_sent = [str(pd.Timestamp("2026-10-15 10:16:02", tz="UTC"))]; assert not b.fill_due(T20 - pd.Timedelta(minutes=3)) and b.fill_due(T40), "slot 2 filled; :40 catches up slot 3"
b.tier_sent = [str(T40 - pd.Timedelta(minutes=k)) for k in (5, 10, 15)]; assert not b.fill_due(T40), "3 sent >= paced need 3"
assert b.fill_due(pd.Timestamp("2026-10-15 10:55:02", tz="UTC")), "need 4 by :55"
mk = lambda p, conf: {"pair": p, "signal": "CALL", "agree": ["B", "C"], "payout": .9, "atr": 1e-4, "confidence": conf, "strategies": {"B": ("CALL", .8), "C": ("CALL", .9)}}
c1, c2 = mk(P, 80), mk(list(C.PAIRS)[1], 70)
# tier 1: second candidate history-confirmed wins over the first unconfirmed
def chk(p, nd, comp, now, pay):
    b.hist.last = {"n30": 23, "w30": 17, "n7": 0, "w7": 0}
    return (p == c2["pair"], "x", "seen")
b.hist.check = chk; b.tier_sent = []
best = b.select_tier([c1, c2], T20); assert best is c2 and best["tier"] == 1 and best["hist_line"] == "✅ history-confirmed (17/23)"
# none confirmed, early in hour → nothing; at :40 behind → Tier-2 fill (best confidence with ok history)
b.hist.check = lambda p, nd, comp, now, pay: (setattr(b.hist, "last", {"n30": 20, "w30": 12, "n7": 0, "w7": 0}) or (False, "LB low", "seen"))
b.tier_sent = [str(pd.Timestamp("2026-10-15 10:16:02", tz="UTC"))]
assert b.select_tier([c1, c2], pd.Timestamp("2026-10-15 10:17:02", tz="UTC")) is None, "slot already served → no fill"
best = b.select_tier([c1, c2], T40); assert best is c1 and best["tier"] == 2 and best["hist_line"] == "📘 standard"
# clearly negative history never fills
b.hist.check = lambda p, nd, comp, now, pay: (setattr(b.hist, "last", {"n30": 40, "w30": 10, "n7": 0, "w7": 0}) or (False, "bad", "seen"))
assert b.select_tier([c1, c2], T40) is None
# fill toggle
b.command("/fill off"); assert not C.FILL_MIN_HOUR and not b.fill_due(T40); b.command("/fill on"); assert C.FILL_MIN_HOUR
_, rows = b.menu_view("main"); assert any(d.startswith("q:/fill") for r in rows for _, d in r)
# tier tracking in /stats and /best
b.tier_record({"tier": 1}, True); b.tier_record({"tier": 2}, False)
assert "Tier 1 ✅ history-confirmed: today 1W/0L" in b.summary_text() and "Tier 2 📘 standard" in b.best_text()
assert json.load(open(C.TIERS_FILE))["total"]["2"] == [0, 1]
print("TIER TESTS PASSED")
print("HISTORY GATE TESTS PASSED")
