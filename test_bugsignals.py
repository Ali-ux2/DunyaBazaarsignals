"""Bug signals: planted triggers for B1-B6/E/F, random data mostly silent, no lookahead, rate limits, routing per
/bugsignals mode, resolution + auto-demote, telemetry, leaderboard. Synthetic data only: proves mechanics, NOT edge."""
import os, tempfile, json
os.environ.update(DOUBLE_CONFIRM="off", NEWS_FILTER="off", BLACKOUT="off", STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv")
import numpy as np, pandas as pd, config as C, analyzer as A, bot, bugstrats as S
from datetime import timedelta
from telemetry import Telemetry
rng = np.random.default_rng(7)

def walk(n=600, seed=1, vol=1e-4):
    r = np.random.default_rng(seed); c = 1.1 + np.cumsum(r.normal(0, vol, n)); o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + abs(r.normal(0, vol/2, n)); l = np.minimum(o, c) - abs(r.normal(0, vol/2, n))
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c), index=pd.date_range("2026-01-05", periods=n, freq="1min", tz="UTC"))

# ---------- B1 tick spike ----------
T0 = 1_767_600_000.0 - (1_767_600_000.0 % 60)
ticks = [(T0 + k, 1.1 + 1e-5 * np.sin(k)) for k in range(300)]          # 5 min of calm 1 s ticks
now = T0 + 300 + 30; base = 1.1
ticks += [(T0 + 300 + k, base + (5e-4 if 20 <= k < 25 else 0)) for k in range(30)]   # spike in the 5 s bar :20-:25
ticks += [(now - 4, base)]
r = A.bug_B1(ticks, now - 5, None, "original"); print("B1 planted:", r); assert r and r[0] == "PUT"
calm = [(T0 + k, 1.1 + 1e-5 * np.sin(k)) for k in range(330)]
assert A.bug_B1(calm, T0 + 330, None, "original") is None
# ---------- B4 frozen candle ----------
x1 = pd.DataFrame(dict(adx=[30.0] * 40, ema_f=[1.2] * 40, ema_s=[1.1] * 40))
up = [(T0 + k * 0.5, 1.1 + k * 2e-6 + 1e-6 * (k % 3)) for k in range(500)]
last = up[-1]; tnow = last[0] + 3.0
r = S.bug_B4(up, tnow, x1, "original"); print("B4 planted (pause):", r); assert r and r[0] == "CALL" and r[3] == "pause"
flat = up + [(last[0] + 0.5 * k, last[1]) for k in range(1, 6)]
r = S.bug_B4(flat, flat[-1][0] + 0.2, x1, "original"); print("B4 planted (flat):", r); assert r and r[3] == "flat"
assert S.bug_B4(up, last[0] + 0.5, x1) is None, "no freeze yet"
# ---------- B5 sub-minute ----------
t5 = []
for b_ in range(5):
    for k in range(10): t5.append((T0 + 70 + b_ * 10 + k, 1.1 + b_ * 1e-4 + k * (1e-5 * (1 - 0.1 * b_))))
t5 = [(T0 + k, 1.1) for k in range(0, 70, 2)] + t5
r = S.bug_B5_sub(t5, T0 + 120.5, 10, None, "improved"); print("B5 10s planted:", r); assert r and r[0] == "PUT"
# ---------- 1m strategies on planted candles ----------
df = walk(200, 3)
o = df.close.iloc[149]
for k in range(150, 155):   # 5 full-bodied green candles, shrinking
    df.iloc[k] = [o, o + (6 - (k - 150)) * 1e-4 + 1e-6, o - 1e-6, o + (6 - (k - 150)) * 1e-4]; o = df.close.iloc[k]
x = A.compute(df.iloc[:155]); r = S.bug_B5_1m(x, 154, "original"); print("B5 1m planted:", r); assert r and r[0] == "PUT"
df = walk(200, 4); c = df.close.iloc[148]
df.iloc[149] = [c + 30e-4, c + 31e-4, c + 29.5e-4, c + 30.5e-4]          # gap candle entirely above the bands
x = A.compute(df.iloc[:150])
for n_ in (14, 20):
    r = S.bug_B6(x, 149, n_, 2.0, "improved"); print(f"B6 BB{n_} planted:", r); assert r and r[0] == "PUT"
# ---------- random data: mostly silent; no lookahead ----------
x = S.ensure_cols(A.compute(walk(1500, 9)))
fns = {"B2": A.bug_B2, "B3": A.bug_B3, "B5m": S.bug_B5_1m, "B6_14": lambda x, i, variant=None: S.bug_B6(x, i, 14, 2.0, variant),
       "B6_20": lambda x, i, variant=None: S.bug_B6(x, i, 20, 2.0, variant), "E": S.strat_E, "F": S.strat_F}
counts = {}
for v in ("original", "improved"):
    for k, f in fns.items():
        x2 = x.copy(); counts[(k, v)] = sum(1 for i in range(60, len(x2)) if f(x2, i, variant=v))
print("random-walk fires per 1440 bars (original/improved):", {k: (counts[(k, 'original')], counts[(k, 'improved')]) for k in fns})
for k in fns: assert counts[(k, "improved")] <= counts[(k, "original")] + 2 and counts[(k, "improved")] < 0.08 * 1440, k
df = walk(400, 11); full = S.ensure_cols(A.compute(df))
for k, f in fns.items():
    for i in range(80, 400, 7):
        trunc = S.ensure_cols(A.compute(df.iloc[:i + 1]))
        assert f(full.copy(), i) == f(trunc, i), f"lookahead in {k} at {i}"
print("no-lookahead OK:", list(fns))

# ---------- pipeline: rate limits, routing, resolution, demotion ----------
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); adm, grp = [], []
b.tg.admin = lambda t: adm.append(t); b.tg.group = lambda t, chats=None: grp.append(t); b.tg.group_photo = lambda *a, **k: False
PS = list(C.PAIRS); T = pd.Timestamp("2026-01-05 10:00:02", tz="UTC"); b.d.now = lambda: T
P = PS[0]; xx = A.compute(walk(120, 2))
f1 = b.bug_fire("*:B2", P, "CALL", "test", T, 0.85, x=xx); assert f1 and not f1["silent"] and "BUG SIGNAL" in adm[-1] and not grp
f2 = b.bug_fire("*:B3", P, "PUT", "test", T + timedelta(minutes=1), 0.85, x=xx); assert f2["silent"], "pair cooldown 3 min"
f3 = b.bug_fire("*:B2", PS[1], "PUT", "t", T + timedelta(minutes=2), 0.85, x=xx); assert f3["silent"], "pattern cooldown 5 min"
assert b.bug_fire("*:B2", P, "CALL", "t", T, 0.70, x=xx) is None, "77% floor"
b.pending = {"pair": PS[2]}; assert b.bug_fire("*:B6_14", PS[2], "CALL", "t", T, 0.9, x=xx) is None, "pending group trade"; b.pending = None
C.GROUP_FILTER = "all"
for mode, cand, expect in (("admin", False, 0), ("group", False, 1), ("candidates", False, 0), ("candidates", True, 1), ("off", False, 0)):
    b.bug_last_pair.clear(); b.bug_last_pat.clear(); b.bugsignals_cmd([mode]); g0, a0 = len(grp), len(adm)
    b.bugs["*:F"]["status"] = "candidate" if cand else "watch"
    s = b.bug_fire("*:F", PS[3], "CALL", "t", T + timedelta(minutes=30), 0.9, x=xx)
    assert len(grp) - g0 == expect, (mode, cand); assert (mode == "off") == s["silent"]
    if expect: assert grp[-1].startswith("🐞 BUG"), grp[-1]
    b.bug_sigs.remove(s)
assert json.load(open(C.BUG_SIGNALS_FILE))["mode"] == "off"; b.command("/status"); assert "Bug signals: OFF" in adm[-1]
b.bugsignals_cmd(["admin"])
# resolution (next-candle) + demotion
b.bug_sigs = []; b.bugs["*:B2"]["fw"] = [3, 6]; b.bugs["*:B2"]["status"] = "watch"
e = f1["entry_at"]; bar = pd.DataFrame(dict(open=[1.0], high=[1.0], low=[0.9], close=[0.95]), index=[e])
b.bug_sigs = [f1]; b.cached_candles = lambda p: bar; T = e + timedelta(minutes=1, seconds=5)
b.bug_resolve(); assert any("Bug result" in m and "LOSS" in m for m in adm); assert b.bugs["*:B2"]["status"] == "demoted"
assert any("demoted" in m for m in adm); print("resolve + demote OK:", [m for m in adm if "demoted" in m][-1][:80])
# tick-resolved (B1/B4/B5s 'now' kind)
class Book: ticks = [(e.timestamp() + k, 1.0 + k * 1e-5) for k in range(80)]
b.d.books = {P: Book()}
g = {"k": "*:B1", "pair": P, "dir": "CALL", "traded": "CALL", "entry_at": e, "settle_at": e + timedelta(seconds=60), "kind": "now",
     "entry_price": 1.0, "payout": .9, "silent": False, "group": False, "atr": None, "prechecked": True, "desc": ""}
b.bug_sigs = [g]; w0 = b.bugs["*:B1"]["fw"][0]; b.bug_resolve(); assert b.bugs["*:B1"]["fw"][0] == w0 + 1; print("tick resolve OK")
assert "Signals: ADMIN" in b.bug_report() and "B6_20" in b.bug_report()

# ---------- live-feedback fixes: time patterns off, statistical patterns need >=10 forward at >= BE ----------
assert not any(k.startswith(("hour", "min")) for k, *_ in A.bug_patterns(A.compute(walk(200, 1)))) and C.BUG_MIN_SAMPLES == 40 and C.BUG_MIN_RATE == 0.68
b.bugs["X:run3G"] = {"pair": PS[0], "key": "run3G", "desc": "after 3 greens", "rule": [-1, 1], "status": "watch", "rate": .7, "n": 50, "fw": [3, 2], "alerted": None}
b.bug_last_pair.clear(); b.bug_last_pat.clear(); assert b.bug_fire("X:run3G", PS[0], "PUT", "t", T + timedelta(minutes=200), .9, x=xx)["silent"], "unproven"
b.bugs["X:run3G"]["fw"] = [8, 2]; b.bug_last_pair.clear(); b.bug_last_pat.clear()
assert not b.bug_fire("X:run3G", PS[0], "PUT", "t", T + timedelta(minutes=210), .9, x=xx)["silent"], "proven forward"
del b.bugs["X:run3G"]; b.bug_sigs = []
# ---------- leaderboard ----------
b.strat_total = {"A": [40, 10], "B": [12, 28], "E": [5, 5]}
b.lb_apply(announce=False); print("weights", A.STRAT_WEIGHTS, "muted", A.STRAT_MUTED)
assert A.STRAT_WEIGHTS["A"] > 1 and "B" in A.STRAT_MUTED and "E" not in A.STRAT_WEIGHTS
assert all(C.LB_W_MIN <= w <= C.LB_W_MAX for w in A.STRAT_WEIGHTS.values())
bt = b.best_text(); print(bt.split("\n")[2]); assert "Top 5" in bt and "A trend-pullback" in bt
A.STRAT_WEIGHTS.clear(); A.STRAT_MUTED.clear()

# ---------- GROUP_FILTER=best ----------
C.GROUP_FILTER = "best"; b.strat_total = {}; b.lb_live = False
for k in b.bugs: b.bugs[k]["fw"] = [0, 0]
sig = {"pair": PS[0], "signal": "CALL", "agree": ["A", "B"], "payout": .9, "atr": 1e-4,
       "strategies": {"A": ("CALL", .8), "B": ("CALL", .7), "C": None, "D": ("PUT", .6), "E": None, "F": None}}
C.BEST_BOOTSTRAP = "ensemble"; assert b.group_allowed(sig)[0], "bootstrap ensemble posts"
C.BEST_BOOTSTRAP = "silent"; assert not b.group_allowed(sig)[0], "bootstrap silent"
b.strat_total = {"D": [40, 10]}; n0 = len(adm)
ok, why = b.group_allowed(sig); print("best=D, D opposes:", ok, why); assert not ok and any("filter is LIVE" in m for m in adm[n0:])
b.strat_total = {"A": [40, 10]}; assert b.group_allowed(sig)[0], "best member agrees"
n1 = len(adm); b.group_allowed(sig); assert not any("LIVE" in m for m in adm[n1:]), "announce once"
b.bugsignals_cmd(["group"]); b.bug_last_pair.clear(); b.bug_last_pat.clear(); g0 = len(grp)
s1 = b.bug_fire("*:B6_14", PS[4], "PUT", "t", T + timedelta(minutes=60), .9, x=xx); assert len(grp) == g0, "bug not on best list -> admin only"
b.bugsignals_cmd(["candidates"]); b.bugs["*:B6_14"]["fw"] = [45, 5]; b.bug_last_pair.clear(); b.bug_last_pat.clear()
s2 = b.bug_fire("*:B6_14", PS[5], "PUT", "t", T + timedelta(minutes=70), .9, x=xx); assert len(grp) == g0 + 1, "best-list bug posts"
assert "Allowed to post" in b.best_text() and "B6_14" in b.best_text()
b.paper_ensemble(sig, T); assert b.bug_sigs[-1]["k"] == "*:ENS" and b.bug_sigs[-1]["votes"]
print("best-only group filter OK"); C.GROUP_FILTER = "best"; C.BEST_BOOTSTRAP = "ensemble"
# ---------- dedicated bug group (BUG_CHANNEL_ID) ----------
C.BUG_CHANNEL_ID = "-100333"; C.GROUP_FILTER = "best"; cap = []
b.tg.group = lambda t, chats=None: cap.append((t, [c[0] for c in (chats or [])]))
b.tg.chats = [("public", "-1"), ("vip", "-2")]
b.bugsignals_cmd(["admin"]); b.bug_last_pair.clear(); b.bug_last_pat.clear(); b.bugs["*:B5m"]["fw"] = [0, 0]
s1 = b.bug_fire("*:B5m", PS[6], "CALL", "internal-why", T + timedelta(minutes=90), .9, x=xx)
assert cap[-1][1] == ["bug"] and cap[-1][0].startswith("🐞 BUG SIGNAL") and "internal-why" not in cap[-1][0], cap[-1]
assert "internal-why" in adm[-1] or any("internal-why" in m for m in adm[-3:])
b.bug_last_pair.clear(); b.bug_last_pat.clear(); n0 = len(cap)
s1b = b.bug_fire("*:B1", PS[7], "PUT", "x", T + timedelta(minutes=95), .9, kind="now", entry_price=1.0, x=xx)
assert cap[-1][1] == ["bug"] and "ENTER NOW" in cap[-1][0]
b.bugsignals_cmd(["candidates"]); b.bugs["*:B6_14"]["fw"] = [45, 5]; b.bug_last_pair.clear(); b.bug_last_pat.clear()
s2 = b.bug_fire("*:B6_14", PS[8], "PUT", "t", T + timedelta(minutes=100), .9, x=xx); assert sorted(cap[-1][1]) == ["bug", "public", "vip"], cap[-1]
b.bugsignals_cmd(["off"]); b.bug_last_pair.clear(); b.bug_last_pat.clear(); n0, a0 = len(cap), len(adm)
b.bug_fire("*:B5m", PS[9], "CALL", "t", T + timedelta(minutes=110), .9, x=xx); assert cap[-1][1] == ["bug"], "mode off can't disable bug group"
b.manual_pause = True; b.bug_last_pair.clear(); b.bug_last_pat.clear(); n0 = len(cap)
b.bug_fire("*:B5m", PS[10 % len(PS)], "CALL", "t", T + timedelta(minutes=120), .9, x=xx); assert len(cap) == n0, "/pause stops it"
b.manual_pause = False; b.bugsignals_cmd(["admin"]); assert "ON (always)" in b.bug_group_line() and "/buggroup" not in str(b.COMMANDS)
# :57 + result to the bug group, then daily summary
s1["prechecked"] = False; CLK = s1["entry_at"] - timedelta(seconds=3); b.d.now = lambda: CLK
b.d.candles = lambda p, include_forming=False: (pd.DataFrame(dict(open=[1.0], high=[1.0], low=[1.0], close=[1.0]), index=[s1["entry_at"] - timedelta(minutes=1)]), 0, True)
b.bug_sigs = [s1]; b.bug_precheck_due(); assert "CONFIRMED" in cap[-1][0] and cap[-1][1] == ["bug"]
bar = pd.DataFrame(dict(open=[1.0], high=[1.1], low=[1.0], close=[1.05]), index=[s1["entry_at"]]); b.cached_candles = lambda p: bar
CLK = s1["settle_at"] + timedelta(seconds=5); b.bug_resolve(); assert "WIN" in cap[-1][0] and cap[-1][1] == ["bug"]
b.bug_day_summary(); assert cap[-1][0].startswith("🐞 BUG SIGNAL daily results: 1W/0L") and cap[-1][1] == ["bug"]
b.command("/status"); assert "bug group ON (always)" in adm[-1]
print("bug group routing OK"); C.BUG_CHANNEL_ID = None; b.tg.chats = None

# ---------- telemetry ----------
tel = Telemetry(tempfile.mkdtemp(), max_mb=0.01)
t = T0
for k in range(400):
    t += 0.5 if k != 200 else 3.0
    tel.on_tick("EURUSD", t, 1.1 + (k // 2) * 1e-6, t + 0.3)
tel.on_bar_seen("EURUSD", T0, T0 + 61.2); tel.on_precheck("EURUSD", T0 + 60, T0 + 57)
assert tel.pauses["EURUSD"] == 1 and tel.dups["EURUSD"] > 0 and abs(sorted(tel.lag_ev["EURUSD"])[0] - 0.3) < 1e-6
assert sum(os.path.getsize(os.path.join(tel.dir, f)) for f in os.listdir(tel.dir)) < 12_000, "CSV size cap"
# over-extension: plant reversals after big z
dfo = walk(3000, 5); dfo.index = pd.date_range("2026-01-05", periods=3000, freq="1min", tz="UTC")
xo = A.compute(dfo); tel.on_candles("EURUSD", xo.iloc[:40])
for i in range(40, 3000, 50): tel.on_candles("EURUSD", xo.iloc[:i])
print(tel.summary().split("\n")[0]); assert "EURUSD" in tel.summary() and "⏱" in tel.lag_tag("EURUSD")
print("ALL BUG SIGNAL TESTS PASSED")
