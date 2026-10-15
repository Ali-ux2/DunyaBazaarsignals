"""/reset: usage, two-step confirm with 60 s expiry, scopes, backup JSON written first, config kept."""
import os, tempfile, json, time
SD = tempfile.mkdtemp(); os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv")
import config as C, bot, analyzer as A
from analyzer import tracker
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM = []; b.tg.admin = lambda t: ADM.append(t); P = list(C.PAIRS)[0]
def seed():
    tracker.record(P, True); b.day.setdefault(P, [0, 0, 0])[0] += 1; b.strat_record({"strategies": {"C": ("CALL", .9)}}, 1, 2)
    b.bugs["*:B2"]["fw"] = [3, 1]; b.save_bugs(); b.save_stats(); b.tel.on_tick(P, 1e9, 1.0, 1e9)
    open(os.path.join(SD, "telemetry_2026-10-15.csv"), "w").write("tick,x\n"); b.set_channel = None
seed(); json.dump({"mode": "vip"}, open(C.CHANNEL_TARGET_FILE, "w")) if hasattr(C, "CHANNEL_TARGET_FILE") else None
b.command("/reset"); assert "then /reset confirm" in ADM[-1]
b.command("/reset confirm"); assert "No pending reset" in ADM[-1]
b.command("/reset stats"); assert "Confirm reset of [stats]" in ADM[-1] and tracker.total[P] == [1, 0], "nothing wiped before confirm"
b._reset_want = ("stats", time.time() - 61); b.command("/reset confirm"); assert "expired" in ADM[-1] and tracker.total[P] == [1, 0]
b.command("/reset today"); b.command("/reset confirm"); assert "Reset [today] done" in ADM[-1] and b.day[P][:2] == [0, 0] and tracker.total[P] == [1, 0]
seed(); b.command("/reset stats"); b.command("/reset confirm")
assert tracker.total[P] == [0, 0] and b.strat_total == {} and b.bugs["*:B2"]["fw"] == [3, 1], "stats only"
bk = [f for f in os.listdir(os.path.join(SD, "backups")) if f.startswith("reset_stats_")]; assert bk
j = json.load(open(os.path.join(SD, "backups", bk[0]))); assert j["files"]["pair_stats.json"][P]["wins"] == 1 or P in json.dumps(j["files"]), "backup has the old stats"
b.command("/reset bugs"); b.command("/reset confirm"); assert b.bugs["*:B2"]["fw"] == [0, 0] and "bug watcher" in ADM[-1]
b.command("/reset telemetry"); b.command("/reset confirm"); assert not [f for f in os.listdir(SD) if f.startswith("telemetry_")] and any(f.endswith("telemetry_2026-10-15.csv") for f in os.listdir(os.path.join(SD, "backups")))
seed(); b.rotated_out = {"USDBRL"}; b.save_bench(); b.sched.d["items"].append({"id": 1}); b.sched.save(); b.save_level()
b.command("/reset all"); b.command("/reset confirm")
assert tracker.total[P] == [0, 0] and b.bugs["*:B2"]["fw"] == [0, 0] and not b.rotated_out and "Backup: backups/reset_all_" in ADM[-1]
assert os.path.exists(os.path.join(SD, "schedule.json")) and os.path.exists(C.LEVEL_FILE), "config kept"
assert "/reset" in [c for _, c, _ in b.COMMANDS]
print(ADM[-1]); print("RESET TESTS PASSED")
