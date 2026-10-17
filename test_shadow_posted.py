"""Shadow stats are posted-only: capped (one-per-minute losers) excluded from /stats shadow lines, drawdown, reports; shown on a 'capped (not sent)' line.
Migration splits old capped results via shadow_capped ids. Internal tracker (cap tie-break) unchanged."""
import os, tempfile, json
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", HIST_GATE="off", BLACKOUT="off",
                  NEWS_FILTER="off", PATTERNS="off", VOLUME_MODE="off", BUG_GATE="off", HOLDOUT="off", SHADOW_RELAXED="1", SHADOW_CHANNEL_ID="")
import pandas as pd, config as C, feedback as FB, drawdown as DD, adminjobs as AJ, pairguard as PG
from datetime import timedelta
from analyzer import shadow_tracker
T = pd.Timestamp("2026-10-17 06:00:05", tz="UTC")
# --- pre-deploy history: 3 posted + 2 capped (old rows: no capped flag, matched by id) ---
for i, (res, cap) in enumerate([("WIN", 0), ("WIN", 0), ("LOSS", 0), ("WIN", 1), ("LOSS", 1)]):
    if cap: FB.log("shadow_capped", "x", now=T, source="shadow", pair="USDINR", dir="CALL", id=f"o{i}")
    FB.log("shadow_result", res, now=T, source="shadow", pair="USDINR", dir="CALL", result=res, payout=0.85, id=f"o{i}")
json.dump({"tracker": {"USDINR": {"recent": [1, 1, 0, 1, 0], "wins": 3, "losses": 2}}, "payout": {"USDINR": [4.25, 5]},
           "day": {"USDINR": [3, 2]}, "day_date": "2026-10-17"}, open(C.SHADOW_STATS_FILE, "w"))
import bot
_now = bot.ReplayAdapter.now
bot.ReplayAdapter.now = lambda self: T
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); b.day_date = T.date(); now = [T]; b.d.now = lambda: now[0]
b.load_shadow()
assert b.shadow_day == {"USDINR": [2, 1]}, b.shadow_day
assert b.shadow_posted["USDINR"] == [2, 1] and b.shadow_capres_day[:2] == [1, 1], (b.shadow_posted, b.shadow_capres_day)
assert shadow_tracker.total["USDINR"] == [3, 2], "internal tracker untouched"
# --- live: one posted WIN, one capped LOSS ---
ADM = []; b.tg.admin = lambda t: ADM.append(t); b.tg.group = lambda *a, **k: (_ for _ in ()).throw(AssertionError("posted"))
E = pd.Timestamp("2026-10-17 06:01", tz="UTC")
idx = pd.date_range("2026-10-17 05:55", periods=10, freq="1min", tz="UTC")
df = pd.DataFrame({"open": 1.0, "high": 1.2, "low": 0.9, "close": 1.0}, index=idx); df.loc[E, ["open", "close"]] = [1.0, 1.1]
b.cached_candles = lambda p: df
base = dict(entry_at=E, settle_at=E + timedelta(minutes=1), payout=0.8, confidence=90, regime="r", atr=0.01, prechecked=True, dc="on", chan=False)
b.shadow_open = [dict(base, pair="USDMXN", signal="CALL", id="n1"), dict(base, pair="USDMXN", signal="PUT", id="n2", capped=True)]
now[0] = T + timedelta(minutes=3); b.shadow_resolve()
assert b.shadow_day["USDMXN"] == [1, 0] and b.shadow_capres_day[:2] == [1, 2], (b.shadow_day, b.shadow_capres_day)
L = "\n".join(b.shadow_lines(True)); print(L)
assert "Total 3W/1L" in L and "capped (not sent): 1W/2L · 33% · net -1.15u" in L, L
card = b.summary_text("📊 Stats"); assert "capped (not sent)" in card
# drawdown / reports / pairguard: posted-only (old capped matched by id, new by flag)
st = DD.stats(30, now[0]); assert st["shadow"]["n"] == 4, st
assert sum(1 for r in AJ.results(1, now[0]) if r[0] == "shadow") == 4
assert sum(len(v) for k, v in PG.records(30, now[0]).items() if k[0] == "shadow") == 4
# export still has everything
assert sum(1 for r in FB.read(1, now[0]) if r["kind"] == "shadow_result") == 7
# persistence round trip, no second migration
b.save_shadow(); j = json.load(open(C.SHADOW_STATS_FILE)); assert j["posted"]["USDINR"] == [2, 1] and j["capped_day"][:2] == [1, 2]
print("OK test_shadow_posted")
