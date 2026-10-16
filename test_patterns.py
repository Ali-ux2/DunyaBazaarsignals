"""Pattern family P: detectors on hand-built candles, causality, history mandatory even with HIST_GATE=off, admin-only until
≥20 forward ≥ BE, then bug group; P13 2-minute expiry; P11 30s bars; double-confirm roles; /patterns + menu; /best lists P."""
import os, tempfile
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", GROUP_FILTER="all",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", HIST_GATE="off", BLACKOUT="off", NEWS_FILTER="off", PATTERNS="on")
import numpy as np, pandas as pd, config as C, bot, analyzer as A, patterns as P
from datetime import datetime, timezone, timedelta
UTC = timezone.utc
def mk(seq, base=None):
    """seq of (colour, body×median) → OHLC; 24 alternating warm-up bars with body 1."""
    warm = [(1 if k % 2 else -1, 1.0) for k in range(24)] if base is None else base
    o, h, l, c = [], [], [], []; px = 1.0
    for col, bd in warm + list(seq):
        op = px; cl = op + col * bd * 1e-3; o.append(op); c.append(cl); h.append(max(op, cl) + 2e-4); l.append(min(op, cl) - 2e-4); px = cl
    return P.Bars(o, h, l, c, atr=np.full(len(o), 2e-3))
keys = lambda b: {(k, d) for k, d, _ in P.detect(b, len(b.o) - 1)}
assert ("P1", "CALL") in keys(mk([(1, 1)] * 8)) and ("P1", "PUT") in keys(mk([(-1, 1)] * 8))
assert ("P7", "PUT") in keys(mk([(-1, 1), (1, 1), (1, 1)])) and ("P8", "CALL") in keys(mk([(1, 1), (-1, 1), (-1, 1)]))
assert ("P6", "PUT") in keys(mk([(1, 1), (-1, 1), (1, 1), (-1, 1)])) and ("P6b", "CALL") in keys(mk([(-1, 1), (1, 1), (-1, 1), (1, 1)]))
assert ("P16", "CALL") in keys(mk([(-1, 1), (1, 1), (1, 1), (1, 1), (-1, .5)]))
assert ("P22", "CALL") in keys(mk([(-1, 1), (1, 1), (1, 1), (1, 1), (-1, .5)])) and ("P23", "PUT") in keys(mk([(1, 1), (-1, 1), (-1, 1), (-1, 1), (1, .5)]))
assert ("P7", "PUT") not in keys(mk([(-1, 1), (1, 3), (1, 1)])), "P7 needs NORMAL bodies (0.4–1.5× median)"
b = mk([(-1, 1), (1, 1)]); b.lw[-2] = 5e-4; b.uw[-1] = 1e-4; assert ("P4", "CALL") in keys(b)
# causality: detect(i) unchanged when future bars are appended
b1 = mk([(1, 1)] * 8); b2 = mk([(1, 1)] * 8 + [(-1, 2)] * 3); assert keys(b1) == {(k, d) for k, d, _ in P.detect(b2, len(b1.o) - 1)}
# P11 on 30s bars, P13 on 2m bars
b = mk([(-1, 1), (-1, 1), (-1, 1), (1, .3)]); assert P.detect_p11(b, len(b.o) - 1) == [("P11", "CALL", P.NAMES["P11"])]
o = [1.0 + 0.001 * (k % 5) for k in range(40)]; h = [v + 0.0005 for v in o]; h[-21:-1] = [1.0045 if k % 3 == 0 else 1.002 for k in range(20)]
b13 = P.Bars(o, h, [v - 0.0005 for v in o], o[:-1] + [1.006], atr=np.full(40, 1e-3)); assert P.detect_p13(b13, 39)[0][:2] == ("P13", "PUT")
assert P.detector("P13").horizon == 2 and P.EXPIRY["P13"] == 2 and set(P.DC_ROLE) == set(P.NAMES)
# bot integration
bt = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM, GRP = [], []
bt.tg.admin = lambda t: ADM.append(t); bt.tg.group = lambda t, chats=None: GRP.append((t, [c[0] for c in chats or []])); bt.tg.group_photo = lambda *a, **k: False
bt.tg.chats = bt.chats_for("both"); PR = list(C.PAIRS)[0]; T = datetime(2026, 10, 15, 9, 0, 2, tzinfo=UTC); bt.d.now = lambda: T
assert all(f"*:{k}" in bt.bugs for k in P.NAMES) and all(bot._on(k) for k in P.NAMES)
C.DOUBLE_CONFIRM = False
xx = A.compute(pd.DataFrame(dict(open=np.ones(80), high=np.ones(80) * 1.001, low=np.ones(80) * .999, close=np.ones(80)),
                            index=pd.date_range("2026-10-15 07:00", periods=80, freq="1min", tz="UTC")))
assert C.HIST_GATE == "off"
s = bt.bug_fire("*:P7", PR, "PUT", "t", T, .9, x=xx); assert s["silent"] and not s["group"], "history mandatory for P even with HIST_GATE=off"
assert not GRP and bt.p_hist_day.get("P7") == 1 and not any("Filtered by history" in m for m in ADM), "counted, not spammed"
# history passes → admin alert only (unproven), never the group
bt.hist_gate = lambda *a, **k: (True, "ok", "seen 40×")
C.VERBOSE = True   # per-signal bug admin mirror is /verbose-only now
bt.bug_last_pair.clear(); bt.bug_last_pat.clear(); s = bt.bug_fire("*:P7", list(C.PAIRS)[2], "CALL", "t", T, .9, x=xx)
assert not s["silent"] and not s.get("group") and "admin only until proven" in ADM[-1] and not GRP
bt.bugs["*:P7"]["fw"] = [15, 6]; bt.bug_last_pair.clear(); bt.bug_last_pat.clear(); s = bt.bug_fire("*:P7", list(C.PAIRS)[4], "CALL", "t", T, .9, x=xx)
assert s["group"] and ("bug", "-1003") in s["chats"], "proven (21 fwd, 71% ≥ BE) → bug group"
bt.bugs["*:P7"]["fw"] = [10, 11]; bt.bug_last_pair.clear(); bt.bug_last_pat.clear(); assert not bt.bug_fire("*:P7", list(C.PAIRS)[1], "CALL", "t", T, .9, x=xx)["group"]
# P13 2-minute expiry
bt.bug_last_pair.clear(); bt.bug_last_pat.clear(); s = bt.bug_fire("*:P13", list(C.PAIRS)[2], "PUT", "t", T, .9, x=xx)
assert s["expiry"] == 2 and s["settle_at"] - s["entry_at"] == timedelta(minutes=2) and "2 min" in ADM[-1]
# double-confirm roles
C.DOUBLE_CONFIRM = True
ok, txt = P.p_double_confirm(xx, len(xx) - 1, "P7", "PUT"); assert "P7 gives momentum" in txt
C.VERBOSE = False
bt.bug_last_pair.clear(); bt.bug_last_pat.clear(); n0 = len(ADM); assert bt.bug_fire("*:P3", list(C.PAIRS)[3], "PUT", "t", T, .9, x=xx) is None and len(ADM) == n0 and bt.dc_day["by_dc"]["pattern P3"] == 1
C.VERBOSE = True
# commands / menu / leaderboard
bt.command("/patterns"); assert "Drops today: single-confirmation 1 (pattern P3 1)" in ADM[-1] and "filtered by history" in ADM[-1]; assert "Candle patterns P: ON" in ADM[-1] and "P13" in ADM[-1] and "2m expiry" in ADM[-1]
bt.command("/patterns off"); assert not C.PATTERNS_ON and not bot._on("P1"); bt.command("/patterns on"); assert C.PATTERNS_ON
_, rows = bt.menu_view("main"); assert "c:/patterns" in [d for r in rows for _, d in r]
assert any(r[2].startswith("P8 Pattern") for r in bt.leaderboard())
print("PATTERN TESTS PASSED")
