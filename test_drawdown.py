"""Drawdown per channel (actual payouts, 20u bank), alerts + optional auto-pause, /stats + weekly, score-6 bucket."""
import os, tempfile, json
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", TELEGRAM_ADMIN_ID="42",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002")
import pandas as pd, config as C, bot, drawdown as DD, feedback as FB, adminjobs as AJ
from datetime import timedelta
m = DD.curve([("1", "WIN", 0.8), ("2", "LOSS", 0.8), ("3", "LOSS", 0.8), ("4", "WIN", 0.9), ("5", "TIE", 0.8)])
assert abs(m["pnl"] - (0.8 - 2 + 0.9)) < 1e-9 and abs(m["max_dd"] + 2) < 1e-9 and abs(m["cur_dd"] + 1.1) < 1e-9 and m["n"] == 5
DD.WINDOW = 3; assert abs(DD.curve([("a", "LOSS", .8)] * 3 + [("b", "WIN", .8)])["worst_window"] + 3) < 1e-9; DD.WINDOW = 20
T = pd.Timestamp("2026-10-15 12:00", tz="UTC")
for i in range(25): FB.log("vip_result", "x", pair="USDBRL", result="LOSS" if i % 3 else "WIN", payout=0.85, now=T - timedelta(minutes=30 - i))
for i in range(5): FB.log("shadow_result", "x", pair="USDBRL", result="WIN", payout=0.9, now=T - timedelta(minutes=10 - i))
st = DD.stats(30, T); assert st["vip"]["n"] == 25 and DD.pct(st["vip"]["worst_window"]) <= -0.15 and st["shadow"]["pnl"] > 0
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); A = []; b.tg.admin = lambda t: A.append(t); b.d.now = lambda: T
b.dd_check(force=True); assert any("Drawdown alert VIP" in a for a in A) and not b.dd_paused.get("vip"), "alert only; autopause off by default"
n = len(A); b.dd_check(force=True); assert len(A) == n, "rate-limited"
DD.AUTOPAUSE = True; b._dd_alerted.clear(); b.dd_check(force=True); assert b.dd_paused.get("vip") and "AUTO-PAUSED" in A[-1]
k, r = b.cf_apply({"pair": "USDBRL", "signal": "CALL", "confidence": 99}, [("vip", "-1002"), ("bug", "-1003")], T)
assert [c[0] for c in k] == ["bug"], "paused channel dropped"
b.drawdown_cmd(["resume", "vip"]); assert not b.dd_paused.get("vip") and "max DD" in A[-1]; DD.AUTOPAUSE = False
# score buckets
with open(os.path.join(SD, "features.jsonl"), "a") as f:
    for i, (conf, res) in enumerate([(90, "WIN"), (88, "LOSS"), (70, "WIN")]):
        f.write(json.dumps({"type": "signal", "id": f"I{i}", "ts": T.isoformat(), "confidence": conf, "channel": "main"}) + "\n")
        f.write(json.dumps({"type": "outcome", "id": f"I{i}", "ts": T.isoformat(), "result": res}) + "\n")
sp = DD.score_split(T + timedelta(minutes=1), 1); assert sp["main"]["6–7"] == [1, 1] and sp["main"]["<6"] == [1, 0]
s = b.summary_text(); assert "Drawdown" in s and "6–7 1W/1L" in s, s[-600:]
w = AJ.weekly_text(T + timedelta(hours=1)); assert "<b>Drawdown</b>" in w and "By score" in w
for ch in ("-1001", "-1002"): pass
print("test_drawdown OK")
# ---- drawdown ladder (VIP/public only) ----
import drawdown as DD2
from datetime import datetime
assert [DD2.step_for(x) for x in (-0.05, -0.10, -0.16, -0.25, -0.35, -0.5)] == [0, 1, 2, 3, 4, 5]
os.remove(os.path.join(SD, "ddladder.json")) if os.path.exists(os.path.join(SD, "ddladder.json")) else None
b2 = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); A2 = []; b2.tg.admin = lambda t: A2.append(t); NOW = pd.Timestamp("2026-10-16 12:00", tz="UTC").to_pydatetime(); b2.d.now = lambda: NOW
mk = lambda u: {"vip": {"cur_dd": u, "n": 30}, "public": {"cur_dd": 0.0, "n": 5}, "bug": {"cur_dd": -10.0}, "shadow": {"cur_dd": -10.0}}
b2.ddl_check(mk(-2.0)); assert "1.5%" in A2[-1] and not b2.ddl_paused("vip"); n = len(A2)
b2.ddl_check(mk(-2.1)); assert len(A2) == n, "once per step"
b2.ddl_check(mk(-3.0)); assert "reduce stake to 1%" in A2[-1] and not b2.ddl_paused("vip")
b2.ddl_check(mk(-4.0)); assert b2.ddl_paused("vip") and not b2.ddl_paused("public") and "4h" in A2[-1]
assert not b2.ddl_paused("vip", NOW + timedelta(hours=4, minutes=1))
k, _ = b2.cf_apply({"pair": "USDBRL", "signal": "CALL", "confidence": 99}, [("vip", "-1002"), ("public", "-1001"), ("bug", "-1003"), ("shadow", "-1004")], NOW)
assert "vip" not in [c[0] for c in k] and "bug" in [c[0] for c in k] and "shadow" in [c[0] for c in k], k
b2.ddl_check(mk(-6.0)); u = datetime.fromisoformat(DD2.ladder_load()["vip"]["until"]); assert C.local(u).hour == 0 and C.local(u).date() > C.local(NOW).date(), "rest of day"
b2.ddl_check(mk(-8.0)); u = datetime.fromisoformat(DD2.ladder_load()["vip"]["until"]); assert C.local(u).weekday() == 0 and C.local(u).hour == 0, "until Monday"
assert "week" in A2[-1]
b2.ddladder_cmd([]); assert "vip" in A2[-1] and "PAUSED" in A2[-1]
b2.ddresume_cmd(["vip"]); assert not b2.ddl_paused("vip")
assert [e["event"] for e in __import__("audit").read(30, NOW)][-1] == "dd_resume"
b2.ddl_check(mk(-8.5)); assert not b2.ddl_paused("vip"), "same step after resume does not re-pause"
b2.ddl_check(mk(0.0)); assert DD2.ladder_load()["vip"]["step"] == 0
assert "DD ladder" in b2.status()
print("test_drawdown ladder OK")
