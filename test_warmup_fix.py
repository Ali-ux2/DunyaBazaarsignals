import os; os.environ.setdefault("BUG_GATE", "off"); os.environ.setdefault("HOLDOUT", "off")   # legacy test of the pre-gate promotion rules (gate: test_buggate.py)
"""Warm-up fix: (1) warm-up never overrides a below-BE live record, (2) n≥15 + ≥1 day stored, (3) BLOCKED_SETUPS → shadow paper only, /block persisted."""
import os, tempfile, json
SD = tempfile.mkdtemp()
os.environ.update(SHADOW_MAX_PER_MIN="0", DOUBLE_CONFIRM="off", NEWS_FILTER="off", BLACKOUT="off", STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay",
                  REPLAY_CSV="replay_m1.csv", HIST_GATE="all", PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003",
                  SHADOW_CHANNEL_ID="-1004", GROUP_FILTER="all", PATTERNS="off")
os.environ.pop("BLOCKED_SETUPS", None); os.environ.pop("BUG_WARMUP_MIN_N", None)
import pandas as pd, numpy as np, config as C, bot, analyzer as A
assert C.BUG_WARMUP_MIN_N == 15 and C.BUG_WARMUP_MIN_DAYS == 1 and C.BLOCKED_SETUPS == ["P8", "P14", "P6b"]
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); T = pd.Timestamp("2026-10-15 10:00:02", tz="UTC"); b.d.now = lambda: T
s = lambda n, w: {"n30": n, "w30": w, "n7": 0, "w7": 0}
NE = lambda d: f"not enough history yet ({d} days stored, need 7.0)"
assert b.warmup_ok(NE(1.0), s(15, 12), .9, T) and not b.warmup_ok(NE(0.9), s(15, 12), .9, T) and not b.warmup_ok(NE(2), s(14, 13), .9, T)
SENT = []; b.tg.send = lambda chat, text, label=None: SENT.append((str(chat), text))
b.tg.group = lambda t, chats=None: SENT.extend((str(c[1]), t) for c in (chats or []))
b.tg.group_photo = lambda *a, **k: False
def stub(stats, why):
    def f(p, nd, comp, now, pay): b.hist.last = stats; return False, why, "seen 15×"
    b.hist.check = f
r = np.random.default_rng(5); c = 1.1 + np.cumsum(r.normal(0, 1e-4, 200)); o = np.r_[c[0], c[:-1]]
xx = A.compute(pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c), index=pd.date_range("2026-10-15 06:40", periods=200, freq="1min", tz="UTC")))
PS = list(C.PAIRS); K = "*:B6_14"
def fire(i, fw):
    b.bug_last_pair.clear(); b.bug_last_pat.clear(); b.pending = None; b.warmup_sent = []
    b.bugs[K]["fw"] = list(fw); return b.bug_fire(K, PS[i], "PUT", "t", T, .9, x=xx)
stub(s(15, 12), NE(1.5))
for i, fw in enumerate([(1, 4), (1, 6)]):   # the real losers: E 1W/4L, P8-like 1W/6L
    sg = fire(i, fw); assert not sg.get("warmup") and [c[0] for c in sg["chats"]] == ["shadow"], (fw, sg.get("chats"))
sg = fire(2, (0, 0)); assert sg["warmup"] and [c[0] for c in sg["chats"]] == ["bug"], "no live record → warm-up may stand in"
sg = fire(3, (3, 1)); assert sg["warmup"] and [c[0] for c in sg["chats"]] == ["bug"], "75% ≥ BE"
sg = fire(4, (3, 3)); assert [c[0] for c in sg["chats"]] == ["shadow"], "50% < BE 52.6%"
stub(s(15, 12), NE(0.4)); sg = fire(5, (0, 0)); assert not sg.get("warmup") and [c[0] for c in sg["chats"]] == ["shadow"], "<1 day stored"
# blocklist: P8 proven live still never reaches bug/VIP/public; shadow paper only
b.hist_gate = lambda *a, **k: (True, "ok", "seen 40×"); b.hist.decide = lambda *a, **k: (True, "ok", "seen 40×")
def pfire(k, i):
    b.bug_last_pair.clear(); b.bug_last_pat.clear(); b.pending = None
    b.bugs[f"*:{k}"] = dict(b.bugs.get(f"*:{k}") or {"key": k, "desc": k, "n": 40, "rate": .7}, fw=[30, 5], key=k)
    return b.bug_fire(f"*:{k}", PS[i], "CALL", "t", T, .9, x=xx)
for i, k in enumerate(["P8", "P14", "P6b"], 6):
    sg = pfire(k, i); assert sg and [c[0] for c in sg["chats"]] == ["shadow"] and sg.get("blocked_setup"), (k, sg and sg.get("chats"))
b.command("/block remove P8"); assert "P8" not in C.BLOCKED_SETUPS
assert json.load(open(os.path.join(SD, "blocked_setups.json"))) == ["P14", "P6b"]
b.command("/block add P4"); b.command("/block list"); assert C.BLOCKED_SETUPS == ["P14", "P6b", "P4"]
C.BLOCKED_SETUPS = []; b.load_blocked(); assert C.BLOCKED_SETUPS == ["P14", "P6b", "P4"], "persisted across restart"
grp = [t for ch, t in SENT if ch in ("-1001", "-1002", "-1003")]
assert not any("warm" in t.lower() or "blocked" in t.lower() for t in grp), "groups show card only"
print("ALL WARM-UP FIX TESTS PASSED")
