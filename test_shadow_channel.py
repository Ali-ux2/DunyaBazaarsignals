"""SHADOW_CHANNEL_ID: shadow/paper messages routed to the channel, :57 line, summary section moved, routing line, startup check, unset = admin."""
import os, tempfile
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", GROUP_FILTER="all",
                  HIST_GATE="off", BLACKOUT="off", NEWS_FILTER="off", DOUBLE_CONFIRM="off", PATTERNS="off", VOLUME_MODE="off", SHADOW_CHANNEL_ID="-100777")
import pandas as pd, config as C, bot
from datetime import timedelta
assert C.SHADOW_CHANNEL_ID == "-100777"
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); SENT = []
b.tg.send = lambda chat, text, label=None: SENT.append((str(chat), text))
b.tg.group_photo = lambda *a, **k: False
ch = lambda: [t for c, t in SENT if c == "-100777"]; adm = lambda: [t for c, t in SENT if c == str(C.TELEGRAM_ADMIN_ID)]
# :57 shadow precheck: public format, nothing to admin; stats direction unchanged
T = pd.Timestamp("2026-10-15 10:00:00", tz="UTC"); P = list(C.PAIRS)[0]
idx = pd.DatetimeIndex([T + timedelta(minutes=1)])
def run(o, c):
    SENT.clear(); t = {"pair": P, "signal": "CALL", "entry_at": T + timedelta(minutes=2), "atr": 1e-4, "prechecked": False}
    b.shadow_open = [t]; b.d.now = lambda: T + timedelta(minutes=1, seconds=C.PRECHECK_SECOND + 1)
    b.d.candles = lambda p, include_forming=False: (pd.DataFrame({"open": [o], "close": [c]}, index=idx), None, None)
    b.bug_precheck_due(); assert t["signal"] == "CALL"; return ch()[-1]
l1 = run(1.0, 1.00005); assert l1.startswith("✅ CONFIRMED") and "👻" not in l1
l2 = run(1.0, 0.9999); assert l2.startswith("🔄 SWITCH") and "PUT" in l2
assert not adm()
# result: public result caption only (no 👻 / feedback) in the channel, nothing to admin
SENT.clear(); t = b.shadow_open[0]; t.update(settle_at=t["entry_at"] + timedelta(minutes=1), confidence=60, regime="x", payout=.9)
df = pd.DataFrame({"open": [1.0], "high": [1.0002], "low": [0.9999], "close": [1.0001]}, index=pd.DatetimeIndex([t["entry_at"]]))
b.cached_candles = lambda p: df; b.d.now = lambda: t["settle_at"] + timedelta(minutes=2)
b.shadow_resolve(); print("result:", ch()); assert ch() and not any("👻" in m or "Shadow" in m for m in ch()) and not adm()
# routing line in /status, menu and summary; shadow section not in admin summary
assert "shadow channel -100777" in b.routing_line() and "shadow channel" in b.menu_view("main")[0]
assert "Routing" in b.summary_text()   # shadow stats stay in the admin summary
# startup check failure → admin alert + fallback to admin
SENT.clear(); b.tg.check_post = lambda chat: (True, "ok"); b.shadow_channel_check(); assert not SENT, "silent check posts nothing"
b.tg.check_post = lambda chat: (False, "status left"); b.shadow_channel_check(); assert not ch()
assert any("Cannot post to SHADOW_CHANNEL_ID" in m for m in adm()) and C.SHADOW_CHANNEL_ID == ""
assert "not set" in b.routing_line()
print("ALL SHADOW CHANNEL TESTS PASSED")
