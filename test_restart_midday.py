"""Restart within the same UTC day must NOT reset stats: per-pair day W/L, kinds, prechecks, ladders, strategies, all-time
eligibility counts, bench/compound, shadow day. Shutdown reports but never resets. A real UTC rollover still summarises + resets.
Non-persistent STATE_DIR warning."""
import os, tempfile, json
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv")
import pandas as pd, config as C, bot
from analyzer import tracker
CLK = [pd.Timestamp("2026-10-15 03:30", tz="UTC")]
def mk():
    b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); b.d.now = lambda: CLK[0]; b.day_date = CLK[0].date(); b.load_day(); b.load_strat(); b.load_bench()
    ADM.clear(); b.tg.admin = lambda t: ADM.append(t); return b
ADM = []
b = mk(); P = list(C.PAIRS)[0]
for w in (1, 1, 1, 1, 0):
    tracker.record(P, bool(w)); b.day.setdefault(P, [0, 0, 0])[0 if w else 1] += 1; b.kind_day["confirmed"][0 if w else 1] += 1
b.strat_record({"strategies": {"B": ("CALL", .8), "C": ("CALL", .9)}}, 1.0, 1.1); b.prechecks_day = 5; b.kept_day = 4; b.ladder = {P: 2}
b.rotated_out = {"USDBRL"}; b.save_stats(); b.save_bench()
# shutdown at 04:04 UTC (same day): reports, does not reset
CLK[0] = pd.Timestamp("2026-10-15 04:04", tz="UTC"); b.maybe_daily_summary(force=True); b.save_stats(); b.save_strat(); b.save_bench()
assert any("NOT reset" in m and "4W / 1L" in m for m in ADM), ADM[-1:]
assert b.day[P][:2] == [4, 1] and b.strat_day.get("C") == [1, 0], "shutdown must not reset"
# restart at 04:11 UTC
tracker.reset(); CLK[0] = pd.Timestamp("2026-10-15 04:11", tz="UTC"); b2 = mk()
assert b2.day[P][:2] == [4, 1] and b2.kind_day["confirmed"] == [4, 1] and b2.prechecks_day == 5 and b2.kept_day == 4 and b2.ladder == {P: 2}
assert tracker.total[P] == [4, 1] and b2.n_trades(P) == 5, "eligibility counts persisted"
assert b2.strat_day.get("C") == [1, 0] and b2.strat_total.get("B") == [1, 0] and "USDBRL" in b2.rotated_out
b2.maybe_daily_summary(); assert b2.day[P][:2] == [4, 1], "no rollover same day"
s = b2.summary_text(); assert "4W / 1L" in s; print("after restart /stats:", [l for l in s.split("\n") if "Total" in l][0])
# real UTC rollover: summary of the old day, then reset
CLK[0] = pd.Timestamp("2026-10-16 00:00:02", tz="UTC"); b2.maybe_daily_summary()
assert any("Daily summary 2026-10-15" in m for m in ADM) and b2.day[P][:2] == [0, 0] and b2.day_date.isoformat() == "2026-10-16"
assert tracker.total[P] == [4, 1], "all-time eligibility never resets"
# restart on a NEW day with yesterday's day_state → rollover summary still sent once
CLK[0] = pd.Timestamp("2026-10-15 22:00", tz="UTC"); b3 = mk(); b3.day[P] = [2, 0, 0]; b3.save_day()
CLK[0] = pd.Timestamp("2026-10-16 01:00", tz="UTC"); b4 = mk(); assert str(b4.day_date) == "2026-10-15" and b4.day[P][:2] == [2, 0]
b4.maybe_daily_summary(); assert any("Daily summary 2026-10-15" in m for m in ADM) and b4.day[P][:2] == [0, 0]
# persistence warning
os.environ.pop("STATE_DIR"); assert b4.persistence_check() is False and "STATE_DIR not persistent" in ADM[-1]
os.environ["STATE_DIR"] = SD; assert b4.persistence_check() is True, "marker exists + STATE_DIR set → no warning"
os.remove(C.STATE_MARKER); assert b4.persistence_check() is False and "no marker" in ADM[-1]
print("RESTART MID-DAY TESTS PASSED")
