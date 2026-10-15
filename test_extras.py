"""Daily stop, payout re-check at send time, filter precedence, weekly report + self-tune (ablation), commands and menu."""
import os, tempfile, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", GROUP_FILTER="all",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", HIST_GATE="off", BLACKOUT="off", NEWS_FILTER="off",
                  DOUBLE_CONFIRM="off", PATTERNS="off")
import pandas as pd, config as C, bot, ablation as AB
from datetime import datetime, timezone, timedelta
UTC = timezone.utc
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM, GRP = [], []
b.tg.admin = lambda t: ADM.append(t); b.tg.group = lambda t, chats=None: GRP.append(t); b.tg.group_photo = lambda *a, **k: False
T = datetime(2026, 10, 15, 12, 0, tzinfo=UTC); b.d.now = lambda: T
# daily stop: 5 net losses (wins offset), stop until 10:00 UTC+3 next day = 07:00 UTC
assert C.DAILY_STOP_NET == 5 and C.DAILY_STOP_RESUME == "08:00"
for r in ["LOSS", "LOSS", "WIN", "LOSS", "LOSS", "TIE", "LOSS"]: b.dstop_result(r)
assert not any("Daily stop" in m for m in ADM)
b.dstop_result("LOSS"); assert any("🛑 Daily stop (public/VIP): 5 net losses" in m for m in ADM)
assert "daily loss stop" in b.live_block(T) and b.live_block(datetime(2026, 10, 16, 4, 59, tzinfo=UTC)) and b.live_block(datetime(2026, 10, 16, 5, 1, tzinfo=UTC)) is None
assert not b.fill_due(T.replace(minute=50)), "no fill during daily stop"
b2 = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); b2.d.now = lambda: T; assert "daily loss stop" in b2.live_block(T), "persisted"
b.command("/dailystop"); assert "STOPPED" in ADM[-1] and "until 08:00" in ADM[-1]
b.command("/dailystop clear"); assert b.live_block(T) is None
b.command("/dailystop 3"); assert C.DAILY_STOP_NET == 3; b.command("/dailystop resume 09:30"); assert C.DAILY_STOP_RESUME == "09:30"
b.command("/dailystop off"); assert C.DAILY_STOP_NET == 0; b.dstop_result("LOSS")
b.command("/dailystop 5"); b.command("/dailystop resume 08:00"); assert json.load(open(os.path.join(C.STATE_DIR, "filters.json")))["dstop"] == 5
b.command("/dailystop clear")
# profit stop: +15 one-time alert, +20 close until 10:00 next day; public session closed with the normal closing post
assert (C.DAILY_PROFIT_ALERT, C.DAILY_PROFIT_CLOSE) == (15, 20)
TP = datetime(2026, 10, 15, 9, 0, tzinfo=UTC); b.d.now = lambda: TP; G0 = len(GRP)
b.tg.chats = b.chats_for("both"); pc = b.pubcap(); pc["closed"] = None; b._pc_save()
for _ in range(3): b.dstop_result("LOSS")
for _ in range(17): b.dstop_result("WIN"); b.dstop_result("TIE")
assert not any("You're +15" in m for m in ADM)
b.dstop_result("WIN"); assert sum("You're +15 today (18W/3L" in m for m in ADM) == 1
b.dstop_result("LOSS"); b.dstop_result("WIN"); assert sum("You're +15" in m for m in ADM) == 1, "alert once"
for _ in range(4): b.dstop_result("WIN")
assert not any("Daily profit close" in m for m in ADM); b.dstop_result("WIN")
assert any("Daily profit close (public/VIP): +20 today (24W/4L)" in m for m in ADM)
assert "profit stop" in b.live_block(TP) and b.live_block(datetime(2026, 10, 16, 5, 1, tzinfo=UTC)) is None
assert b.pubcap()["closed"] == "profit" and len(GRP) == G0 + 1, "one normal closing post"
assert b.dstop_block("bug", TP) is None, "bug group has its own counter"
b3 = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); b3.d.now = lambda: TP; assert "profit stop" in b3.live_block(TP), "persisted"
for _ in range(5): b.dstop_result("LOSS", "bug")
assert "daily loss stop" in b.dstop_block("bug", TP) and any("Daily stop (bug group)" in m for m in ADM)
TN = datetime(2026, 10, 16, 5, 5, tzinfo=UTC); b.d.now = lambda: TN; b.dstop_result("WIN"); assert b._ds("live", TN)["w"] == 1, "new trading day at 08:00 UTC+3"
b.command("/dailystop alert 12"); b.command("/dailystop close 18"); assert (C.DAILY_PROFIT_ALERT, C.DAILY_PROFIT_CLOSE) == (12, 18)
assert "profit alert +12" in ADM[-1] and "bug group" in ADM[-1]; b.command("/dailystop alert 15"); b.command("/dailystop close 20"); b.command("/dailystop clear")
b.d.now = lambda: T
# precedence: blackout reported before cooldown before daily stop
assert b.PRECEDENCE[:6] == ["pause", "news", "blackout", "cooldown", "daily stop", "payout floor"] and b.PRECEDENCE[-1].startswith(":57")
b.streaks.s["live"]["until"] = (T + timedelta(minutes=10)).isoformat(); assert b.live_block(T).startswith("loss-streak")
C.BLACKOUT_ON = True; TB = datetime(2026, 10, 15, 6, 50, tzinfo=UTC); b.streaks.s["live"]["until"] = (TB + timedelta(minutes=10)).isoformat()
assert b.live_block(TB).startswith("session blackout"); C.BLACKOUT_ON = False; b.streaks.s["live"]["until"] = None
b.command("/filters"); assert "Filter precedence" in ADM[-1] and "1. pause" in ADM[-1] and "11. :57" in ADM[-1]
# payout re-check at send time (ensemble path and bug group path)
b.d.payout_cache = {p: 0.70 for p in C.PAIRS}; sent = []
b.post_signal = lambda s: sent.append(s)
for _ in range(400):
    b.d.advance(); T2 = b.d.clock; b.d.now = lambda: T2; b._tick()
    if any("payout dropped" in m for m in ADM): break
assert any("payout dropped to 70%" in m for m in ADM) and not sent, "skipped when payout < 77% at send time"
import numpy as np
xx = __import__("analyzer").compute(pd.DataFrame(dict(open=np.ones(80), high=np.ones(80) * 1.001, low=np.ones(80) * .999, close=np.ones(80)),
                                    index=pd.date_range("2026-10-15 07:00", periods=80, freq="1min", tz="UTC")))
b.bugsignals_cmd(["group"]); s = b.bug_fire("*:B6_14", list(C.PAIRS)[0], "PUT", "t", T, .9, x=xx); assert s and not s["group"], "bug group skipped at 70%"
b.d.payout_cache = {}
# ablation + weekly self-tune (synchronous on replay rows)
df = pd.read_csv("replay_m1.csv", parse_dates=["time"]); df["time"] = pd.to_datetime(df.time, utc=True)
rows = []
for p, g in list(df.groupby("pair"))[:4]: rows += AB.stage1(p, g.set_index("time"))
res = AB.table(rows); txt = AB.fmt(res); print(txt)
assert len(res) == len(AB.GRID) and all(0 <= m["wr"] <= 1 and abs(m["exp"] - (m["wr"] * 0.8 - (1 - m["wr"]))) < 1e-9 for _, _, m in res if m["n"])
pr = AB.propose(res, min_n=10); assert pr is None or set(pr[3]) <= {"dc", "level", "switch"}
dc0 = C.DOUBLE_CONFIRM; b.weekly_tune(background=False, rows=rows); assert "Weekly self-tune" in ADM[-1] and "config" in ADM[-1]
assert C.BLACKOUT_ON is False and C.NEWS_FILTER is False   # untouched (test env) — hard blocks never changed by tune
C.BLACKOUT_ON = True; C.NEWS_FILTER = True; b.weekly_tune(background=False, rows=rows); assert C.BLACKOUT_ON and C.NEWS_FILTER and C.LOSS_STREAK_N == 3
b.weekly_tune(background=False, rows=rows[:10]); assert "not enough stored candles" in ADM[-1]
# weekly report + Sunday trigger at the 03:00 UTC+3 rollover
b.day = {p: [0, 0, 0] for p in b.all_pairs}; b.day[list(C.PAIRS)[0]] = [3, 1, 0]
TS = datetime(2026, 10, 18, 0, 0, 5, tzinfo=UTC); assert TS.weekday() == 6
tuned = []; b.weekly_tune = lambda background=True, rows=None: tuned.append(1)
b.day_date = (TS - timedelta(days=1)).date(); b.d.now = lambda: TS; b.maybe_daily_summary()
assert any(m.startswith("📅 Weekly report") and "3W/1L" in m for m in ADM) and tuned == [1]
b.command("/weekly"); assert ADM[-1].startswith("📅 Weekly report")
_, rows_ = b.menu_view("main"); ds = [d for r in rows_ for _, d in r]; assert {"c:/dailystop", "c:/filters", "c:/weekly"} <= set(ds)
print("EXTRAS TESTS PASSED")
