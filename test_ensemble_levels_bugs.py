"""Levels + auto adjust, ensemble agreement rules, bug watcher (biased data detected, random data mostly silent)."""
import os, tempfile, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv")
import numpy as np, pandas as pd, config as C, analyzer as A, bot

# ---- levels ----
for lv in (1, 2, 3, 4, 5):
    A.apply_level(lv); assert C.MIN_CONF == C.LEVELS[lv]["MIN_CONF"] and C.CURRENT_LEVEL == lv
    assert C.SHADOW_OVERRIDES["MIN_CONF"] == C.LEVELS[min(4, lv + 1)]["MIN_CONF"] and C.SHADOW_OVERRIDES["OVERLAP_MODE"] == "off"
    assert (C.SHADOW_OVERRIDES.get("ENSEMBLE_ONLY") == "BC" and C.SHADOW_OVERRIDES["ENSEMBLE_MIN_AGREE"] == 1) == (lv >= 4), lv
assert C.LEVEL_AUTO_MAX == 4 and C.ENSEMBLE_ONLY == "" and C.STRAT_PRIOR == {"B": 1.2, "C": 1.2}
A.apply_level(4); assert C.MIN_PAYOUT == 0.77 and C.MAX_GAP_RUNS == 0 and C.SWITCH_MIN_BODY_ATR == 0.8
assert all(C.LEVELS[3][k] == v for k, v in dict(MIN_CONF=0.62, ADX_TREND=23, ADX_RANGE=19, ER_TREND=0.30).items())
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); msgs = []; b.tg.admin = lambda t: msgs.append(t)
b.command("/level"); assert msgs[-1].startswith("🎚 Level 4") and "L5:" in msgs[-1] and b.level_auto; print(msgs[-1].split("\n")[0])
b.command("/level 5"); b.apply_level_queue(); assert b.level == 5 and C.MIN_CONF == 0.45 and not b.level_auto
b.command("/level 9"); assert msgs[-1].startswith("⚠️")
b.command("/level 4"); b.apply_level_queue(); b.command("/level auto"); b.apply_level_queue(); assert b.level_auto and b.level == 4, (b.level, b.level_auto)
assert json.load(open(C.LEVEL_FILE))["auto"] is True
# auto: 20 trades at 40% WR -> stricter
b.level_trades = []
for k in range(20): b.level_after_trade(k % 5 < 2, 0.85)
print("after 40% WR:", [m for m in msgs if "auto:" in m][-1]); assert b.level == 3
# auto: 20 trades at 75% WR with low signal rate -> looser
b.active_log = [[None, 0]] * 120
for k in range(20): b.level_after_trade(k % 4 != 0, 0.85)
print("after 75% WR, 0 signals/h:", [m for m in msgs if "auto:" in m][-1]); assert b.level == 4
# signal-rate target: 0 signals in 60 active min -> loosen to 5 only while WR >= breakeven
b.level_trades = [[1, .85]] * 20; b.active_log = [[None, 0]] * 60; b.rate_check_n = C.LEVEL_RATE_CHECK_MIN - 1; b.level_rate_check()
assert b.level == 4, "auto capped at 4 (never 5)"; print("rate check at L4: stays 4 (auto cap)")
b.command("/level 5"); b.apply_level_queue(); assert b.level == 5 and "manual only" in msgs[-1]; print(msgs[-1][-90:])
b.command("/level auto"); b.apply_level_queue(); b.set_level(4, "test")
b.active_log = [[None, 1 if k % 10 == 0 else 0] for k in range(60)]; b.command("/status"); assert "signals/hour" in msgs[-1]; print([l for l in msgs[-1].split("\n") if "signals/hour" in l][0])
b.command("/level"); assert "Signals/hour" in msgs[-1]
for _ in range(10):
    for k in range(20): b.level_after_trade(False, 0.85)
assert b.level == 1, "auto floor is 1"
assert C.DEFAULT_LEVEL == 'auto' and C.LEVEL_AUTO_START == 4 and C.MIN_SIGNALS_PER_HOUR == 4; print("auto bounds 1..4 OK")
b2 = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); assert b2.level == 1 and b2.level_auto; print("level persisted:", b2.level, "auto")

# ---- ensemble rules (stub the strategies) ----
A.apply_level(3)
df = A.synthetic(n=400, seed=5); x = A.compute(df)
real = (A.strat_trend_pullback, A.strat_range_reversion, A.strat_sr_rejection, A.strat_voting)
def stub(a, b_, c, d):
    A.strat_trend_pullback = lambda x, i: a; A.strat_range_reversion = lambda x, i: b_; A.strat_sr_rejection = lambda x, i: c
    A.strat_voting = lambda x, i, pair, p: (d, {"regime": "range", "votes": {}})
C.ENTRY_CHECK = False; C.ATR_PCTL_MIN, C.ATR_PCTL_MAX = 0, 100
cases = [(("CALL", .7), ("CALL", .6), None, None, "CALL"), (("CALL", .7), None, None, None, None),
         (("CALL", .7), ("CALL", .6), ("PUT", .65), None, None), (("CALL", .7), ("CALL", .6), ("PUT", .3), None, "CALL"),
         (None, ("PUT", .9), ("PUT", .8), ("PUT", .7), "PUT")]
for a, b_, c, d, want in cases:
    stub(a, b_, c, d); s = A.ensemble(None, "USDINR", 0.85, x=x, use_gate=False)
    print("ensemble", [v and v[0][0] + str(v[1]) for v in (a, b_, c, d)], "->", s["signal"] or s["reject"]); assert s["signal"] == want
stub(("CALL", .7), ("CALL", .6), None, None); assert A.ensemble(None, "X", 0.76, x=x)["signal"] is None   # payout floor
assert A.ensemble(None, "X", 0.85, x=x, gaps=1)["signal"] is None                                      # gap rejection
A.apply_level(1); assert A.ensemble(None, "X", 0.85, x=x, use_gate=False)["signal"] is None             # L1 needs 3 agreeing
A.apply_level(3)
A.strat_trend_pullback, A.strat_range_reversion, A.strat_sr_rejection, A.strat_voting = real; C.ENTRY_CHECK = True

# ---- bug watcher ----
def biased(n=1500, seed=1):     # after 3 reds -> next green 80%
    g = np.random.default_rng(seed); col = []
    for k in range(n):
        if len(col) >= 3 and col[-3:] == [-1, -1, -1]: c = 1 if g.random() < 0.8 else -1
        else: c = 1 if g.random() < 0.5 else -1
        col.append(c)
    o = 50 + np.cumsum(g.normal(0, .01, n)); body = np.abs(g.normal(0, .01, n)) + 1e-4
    c_ = o + np.array(col)*body; hi = np.maximum(o, c_) + .002; lo = np.minimum(o, c_) - .002
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c_}, index=pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC"))
A.apply_level(3)
f = A.bug_scan(A.compute(biased())); print("biased data found:", [(k, r, n) for k, _, _, r, n in f])
assert any(k == "run3R" for k, *_ in f)
print("text:", bot.Bot.bug_text("USDBRL", *[(k, d, r, ra, n) for k, d, r, ra, n in f if k == "run3R"][0]))
hits = 0
for sd in range(20):
    hits += bool(A.bug_scan(A.compute(A.synthetic(n=1500, seed=100 + sd))))
print(f"random data: {hits}/20 series triggered"); assert hits <= 3
same = biased(); same["close"] = same.open + 0.003*np.sign(same.close - same.open)
assert any(k == "samebody" for k, *_ in A.bug_scan(A.compute(same))); print("identical bodies detected")
# bot-level: alert once, rate-limited, forward tracking, /bugs, never group
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); msgs = []; grp = []
b.tg.admin = lambda t: msgs.append(t); b.tg.group = lambda t, chats=None: grp.append(t)
xb = A.compute(biased()); now = xb.index[-1] + pd.Timedelta("62s")
b.bug_feed("USDBRL", xb, now); b.bug_feed("USDBRL", xb, now + pd.Timedelta(minutes=11))
al = [m for m in msgs if m.startswith("🐞")]; print("alerts:", len(al), al[:1]); assert 1 <= len(al) <= C.BUG_MAX_ALERTS_PER_HOUR and not grp
b.command("/bugs"); print(msgs[-1].split("\n")[0]); assert "EXPERIMENTAL" in msgs[-1]
assert json.load(open(C.BUG_FILE))["patterns"]
b.command("/strategies"); print(msgs[-1].split("\n")[0]); b.command("/commands"); assert "/level" in msgs[-1] and "/bugs" in msgs[-1] and "/strategies" in msgs[-1]
# forward prediction fires on the biased pattern
k0 = [k for k in b.bugs if k.endswith("run3R")][0]; preds = 0
for j in range(200, 1500, 7): preds += A.bug_prediction(xb.iloc[:j], "run3R", tuple(b.bugs[k0]["rule"])) is not None
print("forward predictions issued on biased data:", preds); assert preds > 0
# daily review: promote, demote, drop
b.bugs[k0]["fw"] = [16, 4]; b.bugs["USDBRL:fake"] = {"pair": "USDBRL", "key": "fake", "desc": "x", "rule": [-1, 1], "rate": .7, "n": 30, "fw": [8, 14], "status": "watch"}
b.bug_review(); rv = msgs[-1]; print(rv)
assert b.bugs[k0]["status"] == "candidate" and "USDBRL:fake" not in b.bugs and "promoted to CANDIDATE" in rv and not grp
b.bugs[k0]["fw"] = [16, 30]; b.bug_review(); assert b.bugs[k0]["status"] == "watch"
b.bugs[k0]["fw"] = [30, 5]; b.bug_review(); b3 = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); assert b3.bugs[k0]["status"] == "candidate"; print("candidate persisted")
b.command("/bugs"); assert "Candidates:" in msgs[-1]
print("\nALL LEVEL / ENSEMBLE / BUG TESTS PASSED")
