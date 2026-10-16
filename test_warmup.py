"""Bug-group warm-up: while stored history < HIST_MIN_DAYS, n≥5 & win ≥ BE+5pp & 7d not negative → bug group only, ≤4/h,
'warm-up' tag in admin mirror only; ≥ HIST_MIN_DAYS stored → normal gate; /history days N."""
import os, tempfile
os.environ.update(DOUBLE_CONFIRM="off", NEWS_FILTER="off", BLACKOUT="off", STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay",
                  REPLAY_CSV="replay_m1.csv", HIST_GATE="all", PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", GROUP_FILTER="all", PATTERNS="off")
import pandas as pd, config as C, bot
from datetime import timedelta
assert C.BUG_WARMUP and C.HIST_MIN_COVER_DAYS == 7
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); T = pd.Timestamp("2026-10-15 10:00:02", tz="UTC")
NE = "not enough history yet (1.3 days stored, need 7.0)"
s = lambda n, w, n7=0, w7=0: {"n30": n, "w30": w, "n7": n7, "w7": w7}
assert b.warmup_ok(NE, s(15, 12), .9, T), "15×, 80% vs BE 53%"
assert not b.warmup_ok(NE, s(14, 14), .9, T), "n<15"
assert not b.warmup_ok(NE, s(5, 4), .9, T), "old n=5 rule gone"
assert not b.warmup_ok("not enough history yet (0.3 days stored, need 7.0)", s(20, 18), .9, T), "<1 day stored"
assert not b.warmup_ok(NE, s(20, 10), .9, T), "50% < BE+5pp"
assert not b.warmup_ok(NE, s(20, 16, 6, 2), .9, T), "7d clearly negative"
assert not b.warmup_ok("Wilson LB 40% < breakeven", s(10, 9), .9, T), "only while history short"
C.BUG_WARMUP = False; assert not b.warmup_ok(NE, s(15, 12), .9, T); C.BUG_WARMUP = True
b.warmup_sent = [T - timedelta(minutes=m) for m in (5, 15, 25, 35)]; assert not b.warmup_ok(NE, s(15, 12), .9, T), "≤4/h"
b.warmup_sent = [T - timedelta(minutes=m) for m in (5, 15, 25, 65)]; assert b.warmup_ok(NE, s(15, 12), .9, T)
b.warmup_sent = []
# integration: bug_fire with a stubbed history result → bug group only, tag only in admin mirror
SENT = []; b.tg.send = lambda chat, text, label=None: SENT.append((str(chat), text))
b.tg.group = lambda t, chats=None: SENT.extend((str(c[1]), t) for c in (chats or []))
b.tg.group_photo = lambda *a, **k: False; b.d.now = lambda: T
def stub(stats, why):
    def f(p, nd, comp, now, pay): b.hist.last = stats; return False, why, "seen 15× in 30d, 12 won (80%)"
    b.hist.check = f
import numpy as np, analyzer as A
r = np.random.default_rng(5); c = 1.1 + np.cumsum(r.normal(0, 1e-4, 200)); o = np.r_[c[0], c[:-1]]
xx = A.compute(pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c), index=pd.date_range("2026-10-15 06:40", periods=200, freq="1min", tz="UTC")))
P = list(C.PAIRS)[0]
C.VERBOSE = True   # per-signal bug admin mirror is /verbose-only now
stub(s(15, 12), NE); sig = b.bug_fire("*:B6_14", P, "PUT", "t", T, .9, x=xx)
print("warm-up sig:", sig and sig.get("warmup"), sig and sig.get("chats"))
assert sig["warmup"] and not sig["silent"] and [c[0] for c in sig["chats"]] == ["bug"], "bug group only"
grp = [t for c, t in SENT if c == "-1003"]; adm = [t for c, t in SENT if c == str(C.TELEGRAM_ADMIN_ID)]
assert grp and not any("warm" in t.lower() for t in grp) and any("warm-up" in t for t in adm), "tag only in admin mirror"
assert not any(c in ("-1001", "-1002") for c, _ in SENT) and len(b.warmup_sent) == 1
# history ≥ HIST_MIN_DAYS → normal gate (a different reason) → no warm-up
b.bug_last_pair.clear(); b.bug_last_pat.clear(); b.pending = None
stub(s(15, 12), "only 5 past occurrences in 30d (need 15)"); sig = b.bug_fire("*:B6_14", list(C.PAIRS)[1], "PUT", "t", T, .9, x=xx)
assert sig["silent"] and not sig.get("warmup")
# /history days N + warmup toggle
b.command("/history days 3"); assert C.HIST_MIN_COVER_DAYS == 3; b.command("/history warmup off"); assert not C.BUG_WARMUP
b.command("/history days 7"); b.command("/history warmup on"); assert C.BUG_WARMUP and C.HIST_MIN_COVER_DAYS == 7
print("ALL WARM-UP TESTS PASSED")
