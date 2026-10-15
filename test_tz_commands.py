"""Display time zone UTC+3 + /commands table vs handlers + setMyCommands payload."""
import os, tempfile, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv")
from datetime import datetime, timezone
import pandas as pd, config as C, chart, bot
assert C.DISPLAY_TZ_OFFSET_HOURS == 3 and C.TZ_LABEL == "UTC+3" and C.reset_local() == "03:00 UTC+3"
e = pd.Timestamp("2026-10-15 13:00", tz="UTC")
cap = chart.signal_caption("USDINR_otc", "CALL", e, 1, ("a", "b"), 57)
line = [l for l in cap.split("\n") if l.startswith("⏰")][0]; print("card:", line)
assert "16:00:00 UTC+3" in line and "ends 16:01" in line
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); out = []
b.tg.admin = lambda t: out.append(t); b.tg.group = lambda t, chats=None: out.append("G:" + t)
# every table entry has a handler and every handler is listed (aliases allowed)
h = b.handlers(); listed = {c for _, c, _ in b.COMMANDS}
missing = listed - set(h); unlisted = set(h) - listed - {"/help", "/testcard"}
print("listed:", len(listed), "handlers:", len(h), "missing:", missing, "unlisted:", unlisted); assert not missing and not (unlisted - {'/start'})   # /start = hidden alias of /menu
for c in sorted(listed - {"/public", "/vip", "/both", "/stream", "/poll", "/auto", "/pause", "/resume", "/menu", "/start"}): out.clear(); b.command(c); assert out, c
b.command("/commands"); txt = out[-1]; b.command("/help"); assert out[-1] == txt
assert all(f"<b>{g}</b>" in txt for g in ("Control", "Stats", "Channels", "Mode", "Test")); print(txt)
# setMyCommands payload (scoped to admin chat)
sent = {}
import requests
class R: ok = True; status_code = 200; text = ""
requests.post = lambda url, json=None, **k: (sent.update(url=url, body=json), R())[1]
C.TELEGRAM_ADMIN_ID = "12345"; tg = bot.Telegram(); tg.on = True; tg.set_commands(b.COMMANDS)
print("setMyCommands:", sent["url"].rsplit("/", 1)[1], sent["body"]["scope"], len(sent["body"]["commands"]))
assert sent["body"]["scope"] == {"type": "chat", "chat_id": "12345"} and all(not c["command"].startswith("/") for c in sent["body"]["commands"])
# summary header + a round line in local time
assert "03:00" in "\n".join(b.summary_text().split("\n")[:3]) and "UTC+3" in "\n".join(b.summary_text().split("\n")[:3]); print("summary:", b.summary_text().split("\n")[0])
out.clear(); b.command("/mode"); print("mode:", out[-1].split("\n")[0]); assert "UTC+3" in out[-1]
print("\nALL TZ / COMMANDS TESTS PASSED")
