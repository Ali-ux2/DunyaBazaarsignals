"""Smoke: every typed command (with typical args) and every menu button runs without error on mocked Telegram/OTCharts (replay)."""
import os, tempfile
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", TELEGRAM_ADMIN_ID="777")
import config as C, bot
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM = []; b.tg.admin = lambda t: ADM.append(t); b.tg.group = lambda *a, **k: None
b.request_mode = lambda m: ADM.append(f"mode {m}")      # no real stream connects in the smoke test
extra = {"/level": ["", "3", "auto"], "/bugsignals": ["", "admin", "candidates"], "/publiccap": ["", "target 2", "max 10", "start 10:00"],
         "/sched": ["", "help", "add 10:00 public daily text hi"], "/reset": ["", "today", "confirm"]}
fails = []
for cmd in sorted(b.handlers()):
    for a in extra.get(cmd, [""]):
        try: n = len(ADM); b.command(f"{cmd} {a}".strip()); b.apply_level_queue()
        except Exception as e: fails.append((cmd, a, repr(e)))
for sm in ("main", "level", "channel", "mode", "reset", "history"):
    _, rows = b.menu_view(sm)
    for row in rows:
        for label, data in row:
            for d in ([data, "y:" + data[2:]] if data.startswith("q:") else [data]):
                try: b.on_callback({"id": "1", "data": d, "from": {"id": "777"}, "message": {"message_id": 9}})
                except Exception as e: fails.append((label, d, repr(e)))
print("commands:", len(b.handlers()), "· fails:", fails); assert not fails
assert {c for _, c, _ in b.COMMANDS} <= set(b.handlers()), "every /commands entry has a handler"
print("ALL COMMANDS + BUTTONS OK")
