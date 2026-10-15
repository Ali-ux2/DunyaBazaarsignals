"""Silent paper-scoring of single-confirmation drops: scored from cached candles, logged as drop_single_confirmation_result, never sent; /confirm2 + /filters show passed vs dropped."""
import os, tempfile
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", HIST_GATE="off", BLACKOUT="off",
                  NEWS_FILTER="off", DOUBLE_CONFIRM="on", PATTERNS="off", VOLUME_MODE="off")
import pandas as pd, config as C, bot, feedback as FB
from datetime import timedelta
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM, SENT = [], []
b.tg.admin = lambda t: ADM.append(t)
for name in ("group", "group_photo", "send", "shadow", "bug"):
    if hasattr(b.tg, name): setattr(b.tg, name, lambda *a, **k: SENT.append(a))
T = pd.Timestamp("2026-10-15 10:00:05", tz="UTC"); now = [T]; b.d.now = lambda: now[0]
idx = pd.date_range("2026-10-15 09:50", periods=15, freq="1min", tz="UTC")
df = pd.DataFrame({"open": 1.0, "high": 1.2, "low": 0.9, "close": 1.0}, index=idx)
df.loc[pd.Timestamp("2026-10-15 10:01", tz="UTC"), ["open", "close"]] = [1.0, 1.1]   # entry bar: up
b.cached_candles = lambda p: df
b.dc_paper_add("bug B2", "EURUSD", "CALL", "structure", 0.85)
b.dc_paper_add("bug B2", "EURUSD", "CALL", "structure", 0.85)   # duplicate same minute ignored
b.dc_paper_add("shadow", "EURUSD", "PUT", "momentum", 0.85)
assert len(b.dc_paper) == 2 and b.dc_paper[0]["entry_at"] == pd.Timestamp("2026-10-15 10:01", tz="UTC")
b.dc_paper_resolve(); assert len(b.dc_paper) == 2, "not settled yet"
now[0] = T + timedelta(minutes=2); b.dc_paper_resolve(); assert not b.dc_paper
recs = [r for r in FB.read(1, now[0]) if r["kind"] == "drop_single_confirmation_result"]
assert sorted(r["result"] for r in recs) == ["LOSS", "WIN"], recs
w = [r for r in recs if r["result"] == "WIN"][0]; assert w["failed"] == "structure" and w["pair"] == "EURUSD" and w["payout"] == 0.85 and w["entry"].startswith("2026-10-15T10:01")
assert not ADM and not SENT, "silent: nothing posted"
FB.log("bug_result", "WIN", now=now[0], result="WIN"); FB.log("shadow_result", "LOSS", now=now[0], result="LOSS")
line = b.dc_stats_line(1); assert "passed 1/2" in line and "dropped 1/2" in line and "no structure 1/1" in line and "no momentum 0/1" in line and "BE 54.1%" in line, line
b.dc_cmd([]); assert "DC effect 1d" in ADM[-1] and "DC effect 7d" in ADM[-1]
b.filters_cmd(); assert "DC effect 1d" in ADM[-1]
# missing candle: dropped after SHADOW_MAX_WAIT_MIN, nothing logged
b.cached_candles = lambda p: None; b.dc_paper_add("x", "GBPUSD", "CALL", "both"); now[0] += timedelta(minutes=C.SHADOW_MAX_WAIT_MIN + 3); b.dc_paper_resolve(); assert not b.dc_paper
print("test_dc_paper OK")
