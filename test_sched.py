import os; os.environ.setdefault("BUG_GATE", "off"); os.environ.setdefault("HOLDOUT", "off")   # legacy test of the pre-gate promotion rules (gate: test_buggate.py)
"""Scheduled posts + event hooks: UTC+3 time triggers (daily/once/weekdays), capture of sticker/photo/forward, forward/copy/pin
API calls, rate limiting, error reporting (pin permission), session start/close + daily target + first bug signal hooks."""
import os, tempfile, json, time
os.environ.update(DOUBLE_CONFIRM="off", NEWS_FILTER="off", BLACKOUT="off", HIST_GATE="off", STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv",
                  PUBLIC_CHANNEL_ID="-100111", VIP_CHANNEL_ID="-100222", BUG_CHANNEL_ID="-100333", GROUP_FILTER="all", SCHED_PER_CHAT_GAP_S="0", SCHED_MIN_GAP_S="0")
import pandas as pd, config as C, bot
from datetime import timedelta
CLK = [pd.Timestamp("2026-01-05 06:59:02", tz="UTC")]     # Monday 09:59 UTC+3
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); b.d.now = lambda: CLK[0]; ADM, GRP = [], []
b.tg.admin = lambda t: ADM.append(t); b.tg.group = lambda t, chats=None: GRP.append((t, [c[0] for c in chats or []]))
S = b.sched; calls = S.calls
def run(text): b.tg.raw_hook({"text": text, "from": {"id": C.TELEGRAM_ADMIN_ID}, "chat": {"id": 1}, "message_id": 5}); return ADM[-1]
# capture
assert b.sched_hook({"sticker": {"file_id": "STK1"}, "chat": {"id": 1}, "message_id": 6}) and "STK1" in ADM[-1]
b.sched_hook({"photo": [{"file_id": "small"}, {"file_id": "PH_BIG"}], "chat": {"id": 1}, "message_id": 7}); assert "PH_BIG" in ADM[-1]
b.sched_hook({"forward_origin": {"type": "channel", "chat": {"id": -100999, "title": "DB"}, "message_id": 42}, "text": "hi", "chat": {"id": 1}, "message_id": 8})
assert "-100999:42" in ADM[-1] and "copy last" in ADM[-1]
# add items (case preserved in HTML text)
print(run("/sched add 10:00 public daily text <b>Good Morning</b> Traders"))
assert "Added #1" in ADM[-1]
run("/sched add 10:00 vip mon,wed sticker last"); run("/sched add 10:01 all once 2026-01-05 copy last")
run("/sched add 10:01 bug daily forward last"); run("/sched add 10:02 public daily pin 42:silent"); run("/sched add 10:02 vip daily repin 42")
run("/sched add 25:00 public daily text x"); assert ADM[-1].startswith("⚠️")
run("/sched add 10:00 public daily sticker"); assert ADM[-1].startswith("⚠️")
print(run("/sched"))
assert "#6" in ADM[-1] and "Good Morning" in ADM[-1]
# time triggers (09:59 UTC+3 → nothing; 10:00 → text+sticker)
b.tick(); assert not calls
CLK[0] = pd.Timestamp("2026-01-05 07:00:02", tz="UTC"); b.tick()
assert calls[0] == ("sendMessage", {"chat_id": "-100111", "text": "<b>Good Morning</b> Traders", "parse_mode": "HTML", "disable_web_page_preview": True})
assert calls[1] == ("sendSticker", {"chat_id": "-100222", "sticker": "STK1"}); n = len(calls); b.tick(); assert len(calls) == n, "once per minute"
CLK[0] += timedelta(minutes=1); b.tick()
cp = [c for c in calls if c[0] == "copyMessage"]; assert len(cp) == 3 and cp[0][1]["from_chat_id"] == -100999 and cp[0][1]["message_id"] == 42
assert [c for c in calls if c[0] == "forwardMessage"][0][1]["chat_id"] == "-100333"
CLK[0] += timedelta(minutes=1); b.tick()
assert ("pinChatMessage", {"chat_id": "-100111", "message_id": 42, "disable_notification": True}) in calls
assert calls[-2][0] == "unpinChatMessage" and calls[-1] == ("pinChatMessage", {"chat_id": "-100222", "message_id": 42, "disable_notification": False})
assert S.d["items"][2]["paused"], "once item done"
CLK[0] = pd.Timestamp("2026-01-06 07:00:02", tz="UTC"); n = len(calls); b.tick()   # Tuesday: no sticker (mon,wed)
assert [c[0] for c in calls[n:]] == ["sendMessage"], calls[n:]
# test/pause/resume/del
run("/sched pause 1"); CLK[0] = pd.Timestamp("2026-01-07 07:00:02", tz="UTC"); n = len(calls); b.tick(); assert [c[0] for c in calls[n:]] == ["sendSticker"]
run("/sched resume 1"); n = len(calls); run("/sched test 1"); assert calls[n][0] == "sendMessage" and "Test #1 sent" in ADM[-1]
run("/sched del 6"); assert "Deleted #6" in ADM[-1]
# error report: missing pin permission
S.api = lambda m, p: (False, "Bad Request: not enough rights to manage pinned messages in the chat") if "pin" in m.lower() else (True, None)
run("/sched test 5"); assert any("Pin messages" in m for m in ADM[-3:]), ADM[-3:]
del S.api
# rate limiting
C.SCHED_PER_CHAT_GAP_S = 0.2; t0 = time.monotonic(); S.api("sendMessage", {"chat_id": "x"}); S.api("sendMessage", {"chat_id": "x"}); assert time.monotonic() - t0 >= 0.19
C.SCHED_PER_CHAT_GAP_S = 0
# events
run("/sched on on_public_session_start public sticker last"); run("/sched on on_daily_target public text 🎯 <b>Done</b>")
run("/sched on on_bug_signal_first_of_day bug text 🐞 Bug hunting started"); run("/sched on on_fake public text x"); assert ADM[-1].startswith("⚠️")
b.tg.chats = b.chats_for("both"); CLK[0] = pd.Timestamp("2026-01-08 07:30:30", tz="UTC"); n = len(calls); b.session_start_check()
assert ("sendSticker", {"chat_id": "-100111", "sticker": "STK1"}) in calls[n:] and any("session started" in m for m in ADM)
n = len(calls); b.session_start_check(); assert len(calls) == n, "start once per day"
g0 = len(GRP); pub = b.chats_for("public"); b.pubcap_posted(pub); b.pubcap_result(pub, "WIN"); b.pubcap_posted(pub); b.pubcap_result(pub, "WIN")
assert any(c == ("sendMessage", {"chat_id": "-100111", "text": "🎯 <b>Done</b>", "parse_mode": "HTML", "disable_web_page_preview": True}) for c in calls)
assert not any("Daily target reached" in t for t, _ in GRP[g0:]), "configured hook replaces the default text"
# default texts when no hooks: next day, delete start hook → built-in start text
for e in list(S.d["events"]):
    if e["event"] == "on_public_session_start": S.d["events"].remove(e)
S.save()
CLK[0] = pd.Timestamp("2026-01-09 07:30:30", tz="UTC"); b.session_start_check(); assert GRP[-1] == (C.PUBLIC_START_TEXT, ["public"])
# first bug signal of the day
import analyzer as A, numpy as np
df = pd.DataFrame(dict(open=np.ones(80), high=np.ones(80)*1.001, low=np.ones(80)*0.999, close=np.ones(80)), index=pd.date_range("2026-01-09", periods=80, freq="1min", tz="UTC"))
xx = A.compute(df); n = len(calls)
b.bugs["*:B5m"]["fw"] = [12, 2]; b.bugs["*:B6_14"]["fw"] = [12, 2]
b.bug_fire("*:B5m", list(C.PAIRS)[0], "CALL", "t", CLK[0], .9, x=xx); b.bug_last_pair.clear(); b.bug_last_pat.clear()
b.bug_fire("*:B6_14", list(C.PAIRS)[1], "CALL", "t", CLK[0], .9, x=xx)
hook = [c for c in calls[n:] if c[1].get("text") == "🐞 Bug hunting started"]; assert len(hook) == 1 and hook[0][1]["chat_id"] == "-100333"
# persistence
S2 = bot.Scheduler(lambda t: None, lambda: CLK[0]); assert len(S2.d["items"]) == 5 and len(S2.d["events"]) == 2 and S2.d["last"]["sticker"] == "STK1"
assert "/sched" in [c for _, c, _ in b.COMMANDS]
print("SCHEDULER TESTS PASSED")
