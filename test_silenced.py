"""Silenced results: capped shadow, history-filtered, VIP-filter-blocked are scored from cached candles only, logged, never posted; /stats shows them."""
import os, tempfile
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", HIST_GATE="off", BLACKOUT="off",
                  NEWS_FILTER="off", DOUBLE_CONFIRM="on", PATTERNS="off", VOLUME_MODE="off", BUG_GATE="off", HOLDOUT="off")
import pandas as pd, config as C, bot, feedback as FB
from datetime import timedelta
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM, SENT = [], []
b.tg.admin = lambda t: ADM.append(t)
for name in ("group", "group_photo", "send", "shadow", "bug"):
    if hasattr(b.tg, name): setattr(b.tg, name, lambda *a, **k: SENT.append(a))
T = pd.Timestamp("2026-10-15 10:00:05", tz="UTC"); now = [T]; b.d.now = lambda: now[0]
idx = pd.date_range("2026-10-15 09:50", periods=15, freq="1min", tz="UTC")
df = pd.DataFrame({"open": 1.0, "high": 1.2, "low": 0.9, "close": 1.0}, index=idx)
df.loc[pd.Timestamp("2026-10-15 10:01", tz="UTC"), ["open", "close"]] = [1.0, 1.1]   # entry bar up
calls = []
def cc(p):
    calls.append(p); return df if p != "USDNGN" else None   # NGN not polled → UNKNOWN, never fetched
b.cached_candles = cc
def no_fetch(*a, **k): raise AssertionError("must not fetch candles")
b.d.candles = no_fetch
E = pd.Timestamp("2026-10-15 10:01", tz="UTC")
b.sil_add("shadow_capped_result", "shadow", "USDBRL", "CALL", E, payout=0.9, reason="shadow cap", id="s1", score=80)
b.sil_add("shadow_capped_result", "shadow", "USDBRL", "CALL", E, payout=0.9, reason="shadow cap", id="s1")   # dup ignored
b.sil_add("filtered_by_history_result", "ensemble", "USDMXN", "PUT", E, payout=0.8, reason="hist neg", id="h1")
b.sil_add("vip_filtered_result", "ensemble", "USDBDT", "CALL", E, payout=0.91, reason="VIP/public filters: session ✗; mtf ✗", id="v1",
          tier=2, score7=5.95, filters=["session", "min_score", "mtf"])
b.sil_add("vip_filtered_result", "ensemble", "USDINR", "PUT", E, payout=0.85, reason="correlation cap", id="v2", tier=1, filters=[bot.qb_tag("correlation cap (cluster)")])
b.sil_add("vip_filtered_result", "ensemble", "USDNGN", "PUT", E, payout=0.85, reason="VIP/public filters: session ✗", id="v3", filters=["session"])
assert len(b.sil_paper) == 5
b.sil_resolve(); assert len(b.sil_paper) == 5, "not settled yet"
now[0] = T + timedelta(minutes=2); b.sil_resolve(); assert len(b.sil_paper) == 1, "NGN waits for candle"
now[0] = T + timedelta(minutes=2 + C.SHADOW_MAX_WAIT_MIN + 1); b.sil_resolve(); assert not b.sil_paper
R = {r["id"]: r for r in FB.read(1, now[0]) if r["kind"].endswith("_result")}
assert R["s1"]["kind"] == "shadow_capped_result" and R["s1"]["result"] == "WIN" and R["s1"]["payout"] == 0.9 and R["s1"]["entry"].startswith("2026-10-15T10:01")
assert R["h1"]["kind"] == "filtered_by_history_result" and R["h1"]["result"] == "LOSS" and R["h1"]["open"] == 1.0 and R["h1"]["close"] == 1.1
assert R["v1"]["result"] == "WIN" and R["v1"]["filters"] == ["session", "min_score", "mtf"] and R["v1"]["tier"] == 2 and R["v1"]["reason"].startswith("VIP")
assert R["v2"]["filters"] == ["correlation"] and R["v2"]["result"] == "LOSS"
assert R["v3"]["result"] == "UNKNOWN"
assert not ADM and not SENT, "silent: nothing posted"
# export includes them
cp, jp, n = FB.export(1, now[0]); txt = open(cp).read()
for k in ("shadow_capped_result", "filtered_by_history_result", "vip_filtered_result"): assert k in txt
# /stats section
FB.log("drop_single_confirmation_result", "LOSS", now=now[0], result="LOSS", payout=0.85)
L = "\n".join(b.silenced_lines(1)); print(L)
assert "Silenced results today" in L and "capped shadow: 1W/0L" in L and "+0.90u" in L and "history-filtered: 0W/1L" in L and "-1.00u" in L
assert "VIP-filter-blocked: 1W/1L" in L and "1 unknown" in L and "session: 1W/0L" in L and "min_score: 1W/0L" in L and "correlation: 0W/1L" in L
assert "single-confirmation drops: 0W/1L" in L
card = b.summary_text("📊 Stats"); assert "Silenced results today" in card and "VIP-filter-blocked" in card
print("OK test_silenced")
