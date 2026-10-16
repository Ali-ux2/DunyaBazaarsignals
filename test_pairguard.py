"""Pair auto-removal for VIP/public from 30d live record (Wilson UB < payout break-even), re-enable, override, /pairstats, weekly expectancy."""
import os, tempfile
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", TELEGRAM_ADMIN_ID="42", SESSIONS="off", MTF="off", MIN_SCORE="0", SPACING_MIN="0")
import pandas as pd, config as C, bot, pairguard as PG, feedback as FB, adminjobs as AJ, audit as AU
from datetime import timedelta
T = pd.Timestamp("2026-10-16 12:00", tz="UTC")
# USDNGN: 10W/25L live at 85% → excluded; USDBRL: 22W/13L → fine; shadow rows never decide
for i in range(35):
    FB.log("vip_result" if i % 2 else "bug_group_result", "x", pair="USDNGN", result="WIN" if i < 10 else "LOSS", payout=0.85, now=T - timedelta(hours=35 - i))
    FB.log("public_result", "x", pair="USDBRL", result="WIN" if i < 22 else "LOSS", payout=0.85, now=T - timedelta(hours=35 - i))
for i in range(40): FB.log("shadow_result", "x", pair="USDBRL", result="LOSS", payout=0.85, now=T - timedelta(hours=2, minutes=i))
m = PG.metrics(PG.records(30, T)[("live", "USDNGN")]); assert m["n"] == 35 and abs(m["be"] - 1 / 1.85) < 1e-9 and m["ub"] < m["be"]
assert abs(m["exp"] - (10 * 0.85 - 25) / 35) < 1e-9
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); A = []; b.tg.admin = lambda t: A.append(t); b.d.now = lambda: T
b.pg_check(force=True); assert b.pg_excluded("USDNGN") and not b.pg_excluded("USDBRL") and any("USDNGN removed" in a for a in A)
ch = [("vip", "-2"), ("public", "-1"), ("bug", "-3"), ("shadow", "-4")]
k, r = b.cf_apply({"pair": "USDNGN", "signal": "CALL", "confidence": 99}, ch, T); assert [c[0] for c in k] == ["bug", "shadow"] and not r["pair_guard"][0]
n = len(A); b.pg_check(force=True); assert len(A) == n, "alert only on change"
assert any(e["event"] == "pair_guard" for e in AU.read(30, T))
b.pairs_cmd(["enable", "usdngn"]); assert not b.pg_excluded("USDNGN"); b.pg_check(force=True); assert not b.pg_excluded("USDNGN"), "override sticks"
b.pairs_cmd(["auto", "usdngn"]); b.pg_check(force=True); assert b.pg_excluded("USDNGN")
# re-enable: 30 straight wins → last 30 above BE
for i in range(30): FB.log("vip_result", "x", pair="USDNGN", result="WIN", payout=0.85, now=T + timedelta(minutes=i))
b.d.now = lambda: T + timedelta(hours=1); b.pg_check(force=True); assert not b.pg_excluded("USDNGN") and "back in VIP/public" in A[-1]
b.pairstats_cmd([]); assert "USDBRL" in A[-1] and "BE 54.1%" in A[-1] and "shadow" in A[-1]
wk = AJ.weekly_text(T + timedelta(hours=2)); assert "Per-pair expectancy" in wk and "USDBRL E" in wk
assert "pair_guard" in str(b.config_runtime())
print("test_pairguard OK")
