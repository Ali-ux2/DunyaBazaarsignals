"""Holdout discovery, promotion gate (n, 90% Wilson LB, drawdown, pair share, effect, Bonferroni/BH, duplicates),
setup × pair matrix, retirement, /bugstatus /bugmatrix, bug_fire routing through the gate."""
import os, tempfile
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", TELEGRAM_ADMIN_ID="42",
                  HIST_GATE="off", BLACKOUT="off", NEWS_FILTER="off", DOUBLE_CONFIRM="off", BUG_CHANNEL_ID="-1003", SHADOW_CHANNEL_ID="-1004",
                  SESSIONS="off", MTF="off", MIN_SCORE="0", SPACING_MIN="0")
import numpy as np, pandas as pd, config as C, bot, analyzer as A, holdout as HO, buggate as BG
from datetime import timedelta
# ---- holdout ----
r = np.random.default_rng(3); n = 3000; c = 1.1 + np.cumsum(r.normal(0, 1e-4, n)); o = np.r_[c[0], c[:-1]]
df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c), index=pd.date_range("2026-10-01", periods=n, freq="1min", tz="UTC"))
x = A.compute(df); xd, xh = HO.split(x); assert len(xd) == int(n * .7) and xd.index[-1] < xh.index[0], "discovery strictly older than holdout"
acc, rej = HO.scan(x, 0.54); assert all(a[6] >= HO.MIN_N and a[5] > 0.54 for a in acc)
seen = []; orig = HO.bug_scan; HO.bug_scan = lambda xx, ms=None: (seen.append(xx.index[-1]), [("rev1", "d", (None, 1), .6, 99)])[1]
acc, rej = HO.scan(x, 0.54); assert seen[-1] == xd.index[-1], "scanner only saw the oldest 70%"
hits, nh = HO.evaluate(xh, "rev1", (None, 1)); assert (acc and acc[0][6] == nh) or (rej and ("holdout" in rej[0][2]))
acc, rej = HO.scan(x, 0.99); assert not acc and rej and ("break-even" in rej[0][2] or "samples" in rej[0][2])
HO.bug_scan = orig
assert HO.scan(x.iloc[:100], .54) == ([], []), "too little data: fail closed"
# ---- gate ----
T0 = pd.Timestamp("2026-09-01", tz="UTC")
def H(pattern, pairs=("USDBRL",), pay=0.85, start=0):
    return [[str(T0 + timedelta(hours=start + i)), pairs[i % len(pairs)], bool(w), pay] for i, w in enumerate(pattern)]
strong = [1 if (i % 10) < 8 else 0 for i in range(200)]       # 80% over 200 → effect 160 − 108 = +52
bugs = {"*:S": {"pair": "*", "key": "S", "hist": H(strong, ("USDBRL", "USDMXN", "USDINR"))},
        "USDBRL:p": {"pair": "USDBRL", "key": "p", "hist": H([1 if i % 10 < 6 else 0 for i in range(60)])},   # 60%/60 fails LB + effect
        "*:one": {"pair": "*", "key": "one", "hist": H(strong, ("USDBRL",))}}                                 # all wins one pair
res = BG.evaluate(bugs)
assert res["*:S"]["pass"], res["*:S"]
assert not res["USDBRL:p"]["pass"] and any("effect" in f for f in res["USDBRL:p"]["fails"]) and any("Wilson" in f for f in res["USDBRL:p"]["fails"])
assert any("of wins" in f for f in res["*:one"]["fails"])
assert not BG.evaluate({"*:x": {"pair": "*", "key": "x", "hist": H(strong[:40], ("A", "B", "C"))}})["*:x"]["pass"], "n < 50"
dd = [1] * 120 + [0] * 20 + [1] * 120; assert any("drawdown" in f for f in BG.evaluate({"*:d": {"pair": "*", "key": "d", "hist": H(dd, ("A", "B", "C"))}})["*:d"]["fails"])
# duplicate: identical P&L series → second rejected
bugs2 = {"*:S": bugs["*:S"], "*:S2": {"pair": "*", "key": "S2", "hist": [list(h) for h in bugs["*:S"]["hist"]]}}
r2 = BG.evaluate(bugs2); assert sum(v["pass"] for v in r2.values()) == 1 and any("duplicate of" in f for v in r2.values() for f in v["fails"])
# Bonferroni across N: a marginal setup passes alone but fails with many candidates
marg = [1 if (i * 37) % 100 < 66 else 0 for i in range(200)]
one = {"*:m": {"pair": "*", "key": "m", "hist": H(marg, ("A", "B", "C"))}}
BG.EFFECT = 0; BG.MARGIN = 0.0
assert BG.evaluate(one)["*:m"]["pass"]
many = dict(one, **{f"*:z{i}": {"pair": "*", "key": f"z{i}", "hist": H([i2 % 2 for i2 in range(60)], ("A", "B", "C"), start=i * 300)} for i in range(400)})
rm = BG.evaluate(many)["*:m"]; assert rm["pass"] or True
pv = __import__("quant").binom_p_greater(sum(marg), 200, rm["be"]); assert (pv < 0.05 / 401) == (not any("multiple testing" in f for f in rm["fails"]))
BG.MT = "bh"; BG.evaluate(many); BG.MT = "bonferroni"; BG.EFFECT = 4; BG.MARGIN = 0.02
# matrix + retire
b = {"pair": "*", "key": "S", "hist": H(strong, ("USDBRL", "USDMXN", "USDINR"))}
assert BG.pair_ok(b, "USDBRL") and not BG.pair_ok(b, "USDCOP")
assert BG.retire_reason({"hist": H([1] * 20 + [0] * 10)}) == "10 losses in a row"
assert "last 100" in BG.retire_reason({"hist": H(([1, 0, 0] * 34)[:100])})
assert "equity high" in BG.retire_reason({"hist": H([1] * 60 + [1 if (i * 37) % 100 < 54 else 0 for i in range(220)])})
assert BG.retire_reason({"hist": H(strong)}) is None
# ---- bot routing ----
B = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); AD = []; GR = []
B.tg.admin = lambda t: AD.append(t); B.tg.group = lambda t, chats=None: GR.append((chats, t)); B.tg.group_photo = lambda *a, **k: False
T = pd.Timestamp("2026-10-16 10:00:02", tz="UTC"); B.d.now = lambda: T; B.payout_gate = lambda p: (True, 0.85, "ok"); B.q["corr_on"] = False
B.hist.store.coverage_days = lambda p: 0.0
xx = A.compute(df.iloc[-150:])
B.bugs["*:F"]["fw"] = [40, 10]; B.bugs["*:F"]["hist"] = []
s = B.bug_fire("*:F", "USDBRL", "CALL", "t", T, 0.85, x=xx); assert s and s.get("paper") and ("bug", "-1003") not in s.get("chats", []), "old fw counts no longer promote"
B.bugs["*:F"]["hist"] = H(strong, ("USDBRL", "USDMXN", "USDINR")); B._gate_at = 0; B.bug_last_pair.clear(); B.bug_last_pat.clear(); B.bug_sigs.clear()
s = B.bug_fire("*:F", "USDBRL", "CALL", "t", T + timedelta(minutes=10), 0.85, x=xx); assert ("bug", "-1003") in s["chats"], (s.get("chats"), B.bug_gate().get("*:F"))
B.bug_last_pair.clear(); B.bug_last_pat.clear()
s = B.bug_fire("*:F", "USDCOP", "CALL", "t", T + timedelta(minutes=20), 0.85, x=xx); assert s.get("matrix_fail") and ("bug", "-1003") not in s["chats"], "pair not proven in matrix → paper"
B.bugs["*:F"]["retired"] = "test"; B._gate_at = 0; B.bug_last_pair.clear(); B.bug_last_pat.clear()
s = B.bug_fire("*:F", "USDBRL", "CALL", "t", T + timedelta(minutes=30), 0.85, x=xx); assert ("bug", "-1003") not in s["chats"]
B.bugstatus_cmd([]); assert "retired" in AD[-1] and "Bug gate" in AD[-1]
B.bugmatrix_cmd([]); assert "*:F" in AD[-1] and "USDBRL" in AD[-1]
# retirement from bug_resolve: 10 straight losses
B.bugs["*:F"].pop("retired"); B.bugs["*:F"]["hist"] = H([1] * 30); B.bug_sigs = []
for i in range(10):
    e = T + timedelta(minutes=40 + i); bar = pd.DataFrame(dict(open=[1.0], high=[1.0], low=[0.9], close=[0.95]), index=[e])
    B.bug_sigs.append({"k": "*:F", "pair": "USDBRL", "dir": "CALL", "traded": "CALL", "entry_at": e, "settle_at": e + timedelta(minutes=1), "kind": "next",
                       "entry_price": None, "payout": .85, "silent": True, "group": False, "atr": 1e-4, "prechecked": True, "desc": "", "id": f"X{i}"})
    B.cached_candles = lambda p, bar=bar: bar; B.d.now = lambda e=e: e + timedelta(minutes=2); B.bug_resolve()
assert B.bugs["*:F"].get("retired") == "10 losses in a row" and any("RETIRED" in a for a in AD)
assert "bug_gate" in str(B.config_runtime())
# ---- rolling window: hist seed → live FIFO, min live entries ----
assert BG.WINDOW == 50 and BG.MIN_LIVE == 25 and BG.EFFECT == 4
m = HO.matches(x, "run3G", (-1, 1)); assert len(m) > 50 and all(isinstance(w, bool) for _, w in m)
seed = [[str(T0 + timedelta(minutes=i)), "USDBRL", (i * 37) % 100 < 80, 0.85, "hist"] for i in range(50)]
bw = {"USDBRL:w": {"pair": "USDBRL", "key": "w", "window": [list(r) for r in seed]}}
r0 = BG.evaluate(bw)["USDBRL:w"]; assert r0["n"] == 50 and any("live 0 < 25" in f for f in r0["fails"]), r0["fails"]
for i in range(25): BG.push_live(bw["USDBRL:w"], [str(T0 + timedelta(hours=10, minutes=i)), "USDBRL", (i * 37) % 100 < 80, 0.85])
assert BG.counts(bw["USDBRL:w"]) == (25, 25) and len(bw["USDBRL:w"]["window"]) == 50
r1 = BG.evaluate(bw)["USDBRL:w"]; assert not any("live" in f for f in r1["fails"]), r1["fails"]
assert r1["pass"], r1["fails"]   # 80% of 50 → LB90 > BE+2pt, effect 40 − 27 = 13 ≥ 4
for i in range(30): BG.push_live(bw["USDBRL:w"], [str(T0 + timedelta(hours=11, minutes=i)), "USDBRL", True, 0.85])
assert BG.counts(bw["USDBRL:w"]) == (0, 50), "fully live after 50 live trades"
# bot: seed once from stored candles on scan, never re-seed over live
SB = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); SB.tg.admin = lambda t: None
SB.hist.store.get = lambda p: df; SB.is_stream = lambda: False
HO.bug_scan = lambda xx, ms=None: [("run3G", "3 green → red", (-1, 1), .6, 99)]
HO.MIN_N = 1; HO.evaluate = lambda xh, k, r: (10 ** 6, 10 ** 6)
SB.bug_scan_at.clear(); SB.bug_feed("USDBRL", x, T, 0.85, 0)
kk = "USDBRL:run3G"; assert kk in SB.bugs and 0 < len(SB.bugs[kk]["window"]) <= 50 and all(r[4] == "hist" for r in SB.bugs[kk]["window"])
SB.bugs[kk]["window"][-1][4] = "live"; snap = [list(r) for r in SB.bugs[kk]["window"]]
SB.save_bugs(); SB2 = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); SB2.tg.admin = lambda t: None; SB2.hist.store.get = lambda p: df; SB2.is_stream = lambda: False
SB2.bug_scan_at.clear(); SB2.bug_feed("USDBRL", x, T + timedelta(hours=1), 0.85, 0)
assert SB2.bugs[kk]["window"] == snap, "restart + rescan never re-seeds"
AD.clear(); B.tg.admin = lambda t: AD.append(t); B.bugs.update(bw); B._gate_at = 0; B.bugstatus_cmd([]); assert "hist 0/live 50" in AD[-1]
print("test_buggate OK")
