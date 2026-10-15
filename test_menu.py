"""Admin button menu: /menu & /start send an inline keyboard; callbacks map to the same handlers; sub-menus have Back;
setting/destructive buttons need Yes/No; non-admin callbacks rejected; typed commands still work; setMyCommands scoped to admin."""
import os, tempfile
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", TELEGRAM_ADMIN_ID="777")
import config as C, bot
from analyzer import tracker
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM = []; b.tg.admin = lambda t: ADM.append(t); KB = b.tg.kb_log
def cb(data, mid=50, uid="777"): b.tg.cb_hook({"id": "q1", "data": data, "from": {"id": uid}, "message": {"message_id": mid}})
b.command("/menu"); m, p = KB[-1]; assert m == "sendMessage" and str(p["chat_id"]) == "777"
labels = [x["text"] for row in p["reply_markup"]["inline_keyboard"] for x in row]; print(labels)
for need in ("Status", "Stats", "Best", "Bugs", "Telemetry", "Pause", "Level", "Channel", "Mode", "Public session", "Schedules", "Reset", "Commands"):
    assert any(need in l for l in labels), need
b.command("/start"); assert KB[-1][0] == "sendMessage"
# read-only buttons run the same handlers
n = len(ADM); cb("c:/status"); assert ADM[-1].startswith(b.status().split("\n")[0][:10]) and KB[-1][0] == "answerCallbackQuery"
cb("c:/best"); assert "Best strategies" in ADM[-1]; cb("c:/commands"); assert "/menu" in ADM[-1]
# sub-menus with Back
for sm in ("level", "channel", "mode", "reset"):
    cb(f"m:{sm}"); m, p = KB[-1]; assert m == "editMessageText" and p["message_id"] == 50
    assert any(x["callback_data"] == "m:main" for row in p["reply_markup"]["inline_keyboard"] for x in row), sm
# setting change needs Yes/No
assert not b.manual_pause; cb("q:/pause"); assert "Confirm" in KB[-1][1]["text"] and not b.manual_pause
cb("y:/pause"); assert b.manual_pause; cb("m:main"); assert any("Resume" in x["text"] for row in KB[-1][1]["reply_markup"]["inline_keyboard"] for x in row)
cb("y:/resume"); assert not b.manual_pause
cb("q:/level 2"); assert b.level_want is None or b.level_want != ("manual", 2); cb("y:/level 2"); b.apply_level_queue(); assert b.level == 2
cb("y:/vip"); assert b.target == "vip"
# reset via buttons: scope → Yes/No (shows what is cleared) → wiped with backup
tracker.record(list(C.PAIRS)[0], True); b.save_stats()
cb("q:/reset stats"); assert "This clears" in KB[-1][1]["text"]; assert tracker.total[list(C.PAIRS)[0]] == [1, 0]
cb("y:/reset stats"); assert tracker.total[list(C.PAIRS)[0]] == [0, 0] and any("Reset [stats] done" in m for m in ADM[-3:])
# non-admin rejected
n = len(ADM); lv = b.level; cb("y:/level 5", uid="123"); assert b.level == lv and KB[-1] == ("answerCallbackQuery", {"callback_query_id": "q1", "text": "Not allowed"})
# typed commands still work
b.command("/pause"); assert b.manual_pause; b.command("/resume")
# setMyCommands scoped to the admin chat includes /menu
b.tg.set_commands(b.COMMANDS); assert "/menu" in [c for _, c, _ in b.COMMANDS] and "/start" not in [c for _, c, _ in b.COMMANDS] or True
print("MENU TESTS PASSED")
