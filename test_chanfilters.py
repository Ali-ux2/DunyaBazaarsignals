"""VIP/public filters: sessions, 15-min spacing, min score, M5/M15 alignment (fail-closed), ATR band 20-80, 120-min live freeze."""
import os, tempfile
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", TELEGRAM_ADMIN_ID="42",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", SHADOW_CHANNEL_ID="-1004")
import numpy as np, pandas as pd, config as C, bot, chanfilters as CF, quant as Q, filters as F
from datetime import timedelta
assert Q.ATR_HI == 80 and Q.ATR_LO == 20
W = CF.parse_windows("10:00-11:30,16:30-19:00"); loc = lambda h, m: pd.Timestamp(f"2026-10-15 {h:02d}:{m:02d}")
assert CF.in_windows(loc(10, 0), W) and CF.in_windows(loc(18, 59), W) and not CF.in_windows(loc(11, 30), W) and not CF.in_windows(loc(13, 0), W)
assert CF.in_windows(loc(23, 30), CF.parse_windows("23:00-01:00"))
assert CF.score7({"confidence": 100}) == 7 and CF.score7({"score": 6, "max_score": 7}) == 6 and CF.score7({}) is None
# MTF trend on synthetic up / down / short data
idx = pd.date_range("2026-10-15 00:00", periods=600, freq="1min", tz="UTC")
up = pd.Series(np.linspace(1.0, 1.1, 600) + np.sin(np.arange(600)) * 1e-4, idx)
mk = lambda c: pd.DataFrame(dict(open=c.shift(1).fillna(c.iloc[0]), high=c + 1e-4, low=c - 1e-4, close=c))
assert CF.tf_trend(mk(up), "5min") == "up" and CF.tf_trend(mk(up), "15min") == "up"
assert CF.tf_trend(mk(2.2 - up), "15min") == "down" and CF.tf_trend(mk(up).iloc[:200], "15min") is None
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); A = []; b.tg.admin = lambda t: A.append(t)
b.hist.store.get = lambda p: mk(up)
assert b.cf_mtf("USDBRL", "CALL")[0] and not b.cf_mtf("USDBRL", "PUT")[0]
b.hist.store.get = lambda p: None; ok, why = b.cf_mtf("USDBRL", "CALL"); assert not ok, "fail closed without candles"
b.hist.store.get = lambda p: mk(up)
ch = [("vip", "-1002"), ("public", "-1001"), ("bug", "-1003")]
inside = pd.Timestamp("2026-10-15 14:00", tz="UTC")    # 17:00 UTC+3 → ny_open (USDMXN, USDBRL, USDARS)
outside = pd.Timestamp("2026-10-15 10:00", tz="UTC")   # 13:00 UTC+3 → no window
sig = {"pair": "USDBRL", "signal": "CALL", "confidence": 90}
k, r = b.cf_apply(sig, ch, outside); assert [c[0] for c in k] == ["bug"] and not r["session"][0], "outside window → VIP/public to paper; bug unaffected"
k, r = b.cf_apply(dict(sig, pair="USDINR"), ch, inside); assert [c[0] for c in k] == ["bug"], "pair not listed in ny_open"
k, r = b.cf_apply(sig, ch, inside); assert [c[0] for c in k] == ["vip", "public", "bug"] and sig["window"] == "ny_open", r
k, r = b.cf_apply(sig, ch, inside + timedelta(minutes=5)); assert [c[0] for c in k] == ["bug"] and not r["spacing"][0], "spacing 15 min"
k, _ = b.cf_apply(sig, ch, inside + timedelta(minutes=15)); assert [c[0] for c in k] == ["vip", "public", "bug"]
b.spacing_cmd(["0"]); k, _ = b.cf_apply(sig, ch, inside + timedelta(minutes=16)); assert len(k) == 3; b.spacing_cmd(["15"])
k, r = b.cf_apply(dict(sig, confidence=70), ch, inside + timedelta(minutes=30)); assert [c[0] for c in k] == ["bug"] and not r["min_score"][0], "70% = 4.9/7 < 6"
k, r = b.cf_apply(dict(sig, signal="PUT"), ch, inside + timedelta(minutes=45)); assert [c[0] for c in k] == ["bug"] and not r["mtf"][0]
# asian off by default; enabling it allows USDINR at 04:00 UTC+3
asia = pd.Timestamp("2026-10-15 01:00", tz="UTC")
k, _ = b.cf_apply(dict(sig, pair="USDINR"), ch, asia); assert [c[0] for c in k] == ["bug"]
b.windows_cmd(["on", "asian"]); k, _ = b.cf_apply(dict(sig, pair="USDINR"), ch, asia + timedelta(minutes=20)); assert len(k) == 3
b.windows_cmd(["set", "asian", "02:00-03:00", "usdpkr,usdbdt"]); assert b.cf["schedule"]["asian"] == {"on": True, "time": "02:00-03:00", "pairs": ["USDPKR", "USDBDT"]}
assert "asian 02:00-03:00: USDPKR, USDBDT" in A[-1]
import chanfilters as CFm; assert CFm.DEFAULTS["schedule"]["asian"]["on"] is False, "defaults not mutated"
b.minscore_cmd(["4"]); b.mtf_cmd(["off"]); b.windows_cmd(["off"])
k, _ = b.cf_apply(dict(sig, signal="PUT", confidence=70), ch, outside + timedelta(minutes=60)); assert len(k) == 3, "filter off"
b.windows_cmd(["on"]); b.windows_cmd(["bug", "on"]); k, _ = b.cf_apply(sig, ch, outside + timedelta(minutes=90)); assert k == []
tg = b.cf_tags(dict(sig), inside + timedelta(minutes=120)); assert "in_window" in tg
b2 = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); assert b2.cf["schedule"]["asian"]["time"] == "02:00-03:00" and b2.cf["min_score"] == 4, "persisted"
# config version bumps on a window change
import configver as CV
v1 = b.config_check("x"); b.command("/windows off ny_late"); assert CV.current() != v1 and any("chanfilters" in k for k in CV.history(1)[-1]["diff"])
# weekly report per window / pair-in-window
import feedback as FB, adminjobs as AJ
for i in range(4): FB.log("vip_result", "x", pair="USDMXN", result="WIN" if i else "LOSS", payout=.85, now=inside + timedelta(minutes=i))
wk = AJ.weekly_text(inside + timedelta(hours=1)); assert "By pair window" in wk and "ny_open" in wk and "USDMXN 3-1" in wk, wk
assert "VIP/public filters" in b.status()

# freeze: live 120 min, bug 30 min
st = F.Streaks(os.path.join(SD, "s.json")); T = pd.Timestamp("2026-10-15 12:00", tz="UTC").to_pydatetime()
for _ in range(2): assert st.result("live", "LOSS", T) is None
assert st.result("live", "LOSS", T) == T + timedelta(minutes=120)
for _ in range(2): st.result("bug", "LOSS", T)
assert st.result("bug", "LOSS", T) == T + timedelta(minutes=30)
print("test_chanfilters OK")
