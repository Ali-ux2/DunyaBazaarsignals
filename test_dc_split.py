"""Per-channel double-confirmation split: DC ON for VIP/public/bug, OFF for shadow/paper by default.
/confirm2 on|off = main switch; /confirm2 shadow on|off = shadow; both persisted in STATE_DIR/filters.json; nothing reset."""
import os, json, tempfile
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv",
                  HIST_GATE="off", BLACKOUT="off", NEWS_FILTER="off", DOUBLE_CONFIRM="on", PATTERNS="on", VOLUME_MODE="off",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", SHADOW_CHANNEL_ID="-1004",
                  SHADOW_RELAXED="1", GROUP_FILTER="all", TELEGRAM_ADMIN_ID="42")
os.environ.pop("DOUBLE_CONFIRM_SHADOW", None)
import numpy as np, pandas as pd, config as C, bot, analyzer as A, bugstrats, feedback as FB
assert C.DOUBLE_CONFIRM and not C.DOUBLE_CONFIRM_SHADOW, "defaults: main ON, shadow OFF"
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); SENT, ADM = [], []
b.tg.group = lambda t, chats=None: SENT.extend((c[0], t) for c in (chats or []))
b.tg.group_photo = lambda *a, **k: False
b.tg.admin = lambda t: ADM.append(t)
b.tg.send = lambda chat, text, label=None: SENT.append((label or str(chat), text))
T = pd.Timestamp("2026-10-15 10:00:02", tz="UTC"); b.d.now = lambda: T
r = np.random.default_rng(5); c = 1.1 + np.cumsum(r.normal(0, 1e-4, 200)); o = np.r_[c[0], c[:-1]]
df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c),
                  index=pd.date_range("2026-10-15 06:40", periods=200, freq="1min", tz="UTC"))
xx = A.compute(df); PAIRS = list(C.PAIRS)
b.payout_gate = lambda p: (True, 0.9, "ok")
labels = lambda: {l for l, _ in SENT}
def reset_cd(): b.bug_last_pair.clear(); b.bug_last_pat.clear(); SENT.clear()

# --- force DC failure / pass
bot.double_confirm = lambda *a, **k: (True, False, "no structure")
# 1) proven B (would go to bug group): DC fails → never bug; shadow DC off → shadow paper; drop paper-scored
reset_cd(); b.bugs["*:B6_14"]["fw"] = [20, 5]; n_paper = len(b.dc_paper)
s = b.bug_fire("*:B6_14", PAIRS[0], "PUT", "t", T, 0.9, x=xx)
assert s and [x[0] for x in s["chats"]] == ["shadow"] and s["dc"] == "fail" and s["paper"], s
assert "bug" not in labels() and "public" not in labels() and "vip" not in labels(), SENT
assert len(b.dc_paper) == n_paper + 1, "drop still paper-scored as evidence"
fw0 = list(b.bugs["*:B6_14"]["fw"])
# 2) shadow DC ON → DC failure is dropped everywhere (old behaviour)
b.command("/confirm2 shadow on"); assert C.DOUBLE_CONFIRM_SHADOW
reset_cd(); assert b.bug_fire("*:B6_14", PAIRS[1], "PUT", "t", T, 0.9, x=xx) is None and not SENT
# 3) DC passes → proven B goes to bug as before
bot.double_confirm = lambda *a, **k: (True, True, "ok")
reset_cd(); s = b.bug_fire("*:B6_14", PAIRS[2], "PUT", "t", T, 0.9, x=xx)
assert s and [x[0] for x in s["chats"]] == ["bug"] and s["dc"] == "on", s
# 4) main DC OFF → failure no longer checked for bug (global toggle still works)
b.command("/confirm2 off"); assert not C.DOUBLE_CONFIRM
bot.double_confirm = lambda *a, **k: (False, False, "none")
reset_cd(); s = b.bug_fire("*:B6_14", PAIRS[3], "PUT", "t", T, 0.9, x=xx)
assert s and [x[0] for x in s["chats"]] == ["bug"] and s["dc"] == "off", s
b.command("/confirm2 on"); b.command("/confirm2 shadow off")
# 5) persistence in filters.json
j = json.load(open(os.path.join(SD, "filters.json"))); assert j["dc"] is True and j["dc_shadow"] is False, j
C.DOUBLE_CONFIRM_SHADOW = True; b.load_filters(); assert C.DOUBLE_CONFIRM_SHADOW is False
# 6) shadow engine uses the shadow switch: DC failing + shadow off → still posted to shadow only, tagged dc=off
bot.analyze_relaxed_ensemble = lambda *a, **k: {"signal": "CALL", "payout": .9, "confidence": 61, "regime": "trend", "agree": []}
reset_cd(); b.shadow_open.clear(); b.shadow_next.clear()
b.shadow_eval(df, xx, PAIRS[4], 0.9, 0, T)
assert b.shadow_open and b.shadow_open[-1]["dc"] == "off" and labels() == {"shadow"}, SENT
C.DOUBLE_CONFIRM_SHADOW = True; reset_cd(); b.shadow_open.clear(); b.shadow_next.clear()
b.shadow_eval(df, xx, PAIRS[5], 0.9, 0, T); assert not b.shadow_open and not SENT, "shadow DC on → dropped"
C.DOUBLE_CONFIRM_SHADOW = False
# 7) groups/shadow got only clean cards (no DC/feedback text)
for l, t in SENT: assert "DC" not in t and "confirmation" not in t, t
# 8) /confirm2 status line + menu
ADM.clear(); b.command("/confirm2")
assert "VIP/public/bug: ON" in ADM[-1] and "shadow: OFF" in ADM[-1] and "DC effect 1d" in ADM[-1] and "DC effect 7d" in ADM[-1], ADM[-1]
_, rows = b.menu_view("main"); ds = [d for r in rows for _, d in r]
assert "q:/confirm2 shadow on" in ds and "q:/confirm2 off" in ds, ds
# 9) DC stats: "passed" excludes shadow DC-off results; they're shown separately
FB.log("bug_result", "WIN", now=T, result="WIN", dc="on"); FB.log("shadow_result", "LOSS", now=T, result="LOSS", dc="off")
FB.log("bug_result", "LOSS", now=T, result="LOSS", dc="fail")
line = b.dc_stats_line(1); assert "passed 1/1" in line and "shadow DC-off 0/2" in line, line
print("test_dc_split OK")
