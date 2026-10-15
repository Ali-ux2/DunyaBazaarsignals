"""Loss-streak cooldown (live + bug separately, ties ignored, persisted), session blackouts (DST via zoneinfo), news blackout
(mocked ForexFactory feed, per-currency, cache + one-time alert), double confirmation, quota excludes blocked minutes, commands/menu."""
import os, tempfile, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), BLACKOUT="on", NEWS_FILTER="on", TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", GROUP_FILTER="all",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", HIST_GATE="off")
import numpy as np, pandas as pd, config as C, bot, analyzer as A
from datetime import datetime, timezone, timedelta
from filters import Streaks, News, session_blackout, windows_utc
UTC = timezone.utc
assert C.PUBLIC_SESSION_START == "10:30"
# ---- blackouts, DST ----
summer = datetime(2026, 7, 15, tzinfo=UTC).date(); winter = datetime(2026, 1, 15, tzinfo=UTC).date()
loc = lambda t: (t + timedelta(hours=3)).strftime("%H:%M")
ws = {tz: (loc(s), loc(e)) for s, e, tz in windows_utc(C.BLACKOUT_WINDOWS, summer) if s.date() == summer}
print("summer UTC+3:", ws); assert ws["Europe/London"] == ("09:45", "10:30") and ws["America/New_York"] == ("16:15", "16:45")
ws = {tz: (loc(s), loc(e)) for s, e, tz in windows_utc(C.BLACKOUT_WINDOWS, winter) if s.date() == winter}
print("winter UTC+3:", ws); assert ws["Europe/London"] == ("10:45", "11:30") and ws["America/New_York"] == ("17:15", "17:45")
# US/EU DST mismatch week (US already on DST, UK not): 2026-03-10
mm = {tz: (loc(s), loc(e)) for s, e, tz in windows_utc(C.BLACKOUT_WINDOWS, datetime(2026, 3, 10).date()) if s.date().isoformat() == "2026-03-10"}
assert mm["America/New_York"] == ("16:15", "16:45") and mm["Europe/London"] == ("10:45", "11:30"), mm
assert session_blackout(datetime(2026, 10, 15, 0, 0, tzinfo=UTC)) and session_blackout(datetime(2026, 10, 14, 23, 50, tzinfo=UTC))
assert not session_blackout(datetime(2026, 10, 15, 0, 20, tzinfo=UTC))
# ---- streaks ----
st = Streaks(); t0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
assert st.result("live", "LOSS", t0) is None and st.result("live", "TIE", t0) is None and st.result("live", "LOSS", t0) is None
st.result("live", "WIN", t0); assert st.s["live"]["n"] == 0, "win resets"
for r in ("LOSS", "LOSS"): st.result("live", r, t0)
u = st.result("live", "LOSS", t0); assert u == t0 + timedelta(minutes=30) and st.frozen("live", t0 + timedelta(minutes=29)) and not st.frozen("bug", t0)
assert Streaks().frozen("live", t0 + timedelta(minutes=5)), "persisted across restart"
assert st.expired("live", t0 + timedelta(minutes=31)) and not st.expired("live", t0 + timedelta(minutes=32))
# ---- news (mocked feed) ----
feed = [{"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-15T08:30:00-04:00", "impact": "High"},
        {"title": "German ZEW", "country": "EUR", "date": "2026-10-15T05:00:00-04:00", "impact": "Medium"},
        {"title": "BI Rate", "country": "IDR", "date": "2026-10-15T03:20:00-04:00", "impact": "High"}]
n = News(fetch=lambda: feed); n.refresh(force=True); assert len(n.events) == 2
nfp = datetime(2026, 10, 15, 12, 30, tzinfo=UTC)
assert n.blocking("USDINR", nfp - timedelta(minutes=14)) and n.blocking("USDINR", nfp + timedelta(minutes=14)) and not n.blocking("USDINR", nfp + timedelta(minutes=16))
assert not n.blocking("EURGBP", nfp) and n.blocking("USDIDR", datetime(2026, 10, 15, 7, 20, tzinfo=UTC))
al = []; n2 = News(fetch=lambda: (_ for _ in ()).throw(RuntimeError("down"))); n2.refresh(al.append, force=True); n2.refresh(al.append, force=True)
assert len(n2.events) == 2 and len(al) == 1, "cache used, alert once"
# ---- bot integration ----
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM, GRP = [], []
b.tg.admin = lambda t: ADM.append(t); b.tg.group = lambda t, chats=None: GRP.append((t, [c[0] for c in chats or []])); b.tg.group_photo = lambda *a, **k: False
b.tg.chats = b.chats_for("both"); P = "USDINR" if "USDINR" in C.PAIRS else list(C.PAIRS)[0]
T = datetime(2026, 10, 15, 9, 0, 2, tzinfo=UTC); b.d.now = lambda: T
C.DOUBLE_CONFIRM = False
xx = A.compute(pd.DataFrame(dict(open=np.ones(80), high=np.ones(80) * 1.001, low=np.ones(80) * .999, close=np.ones(80)),
                            index=pd.date_range("2026-10-15 07:00", periods=80, freq="1min", tz="UTC")))
# live freeze: 3 live losses → alert; quota fill off; bug group still posts
for _ in range(3): b.streak_result("live", "LOSS")
assert any("3 losses in a row, paused until" in m for m in ADM) and not GRP
assert b.live_block(T) and not b.fill_due(T.replace(minute=25))
b.bugsignals_cmd(["group"]); b.bugs["*:B6_14"]["fw"] = [0, 0]
s = b.bug_fire("*:B6_14", P, "PUT", "t", T, .9, x=xx); assert s and [c for c in s.get("chats", [])] == [("bug", "-1003")], s.get("chats")
# bug group's own streak
for _ in range(3): b.streak_result("bug", "LOSS")
b.bug_last_pair.clear(); b.bug_last_pat.clear(); s = b.bug_fire("*:B6_20", list(C.PAIRS)[2], "PUT", "t", T, .9, x=xx); assert not s.get("group"), "bug group cooling down"
T = T + timedelta(minutes=31); b.filters_tick(T); assert any("cooldown over (public/VIP)" in m for m in ADM) and any("cooldown over (bug group)" in m for m in ADM)
assert not b.live_block(T)
# session blackout start/end notes + bug group blocked; quota excludes blocked minutes
TB = datetime(2026, 10, 15, 6, 50, tzinfo=UTC); b.filters_tick(TB); assert "Session blackout" in ADM[-1]
b.bug_last_pair.clear(); b.bug_last_pat.clear(); assert not b.bug_fire("*:B5m", P, "CALL", "t", TB, .9, x=xx).get("group")
b.filters_tick(datetime(2026, 10, 15, 7, 31, tzinfo=UTC)); assert any("blackout over" in m for m in ADM[-3:]), ADM[-3:]
b.blocked_min = [datetime(2026, 10, 15, 11, m_, tzinfo=UTC) for m_ in range(30)]; b.tier_sent = []
assert b.fill_due(datetime(2026, 10, 15, 11, 40, tzinfo=UTC)), "10 active min → need ceil(4*10/60)=1, 0 sent"
b.tier_sent = [str(datetime(2026, 10, 15, 11, 35, tzinfo=UTC))]; assert not b.fill_due(datetime(2026, 10, 15, 11, 40, tzinfo=UTC)), "1 sent ≥ ceil(4*10/60)=1"
# news per pair blocks bug signal routing
b.news = n; TN = nfp - timedelta(minutes=5); b.bug_last_pair.clear(); b.bug_last_pat.clear()
assert not b.bug_fire("*:B5m", P, "CALL", "t", TN, .9, x=xx).get("group") and b.pair_news(P, TN)
# double confirmation
C.DOUBLE_CONFIRM = True
from bugstrats import double_confirm
df = pd.DataFrame(dict(open=np.linspace(1.2, 1.1, 60), close=np.linspace(1.198, 1.098, 60)), index=pd.date_range("2026-10-15", periods=60, freq="1min", tz="UTC"))
df["high"] = df[["open", "close"]].max(axis=1) + 1e-4; df["low"] = df[["open", "close"]].min(axis=1) - 1e-4
df.loc[df.index[-1], ["open", "high", "low", "close"]] = [1.0985, 1.0996, 1.0930, 1.0994]   # hammer (long lower wick) at the low
xd = A.compute(df[["open", "high", "low", "close"]]); m_, s_, txt = double_confirm(xd, len(xd) - 1, "CALL"); print("CALL at low:", txt); assert m_ and s_
m_, s_, txt = double_confirm(xd, len(xd) - 1, "PUT"); print("PUT at low:", txt); assert not (m_ and s_)
assert not b.dc_ok("test", P, "PUT", xd) and "single-confirmation" in ADM[-1]; assert b.dc_ok("test", P, "CALL", xd)
b.command("/confirm2 off"); assert not C.DOUBLE_CONFIRM; b.command("/confirm2 on")
for c_ in ("/cooldown", "/cooldown n 4", "/cooldown min 20", "/cooldown clear live", "/blackout", "/blackout off", "/blackout on", "/news", "/news off", "/news on"): b.command(c_)
assert C.LOSS_STREAK_N == 4 and C.STREAK_COOLDOWN_MIN == 20 and json.load(open(os.path.join(C.STATE_DIR, "filters.json")))["n"] == 4
_, rows = b.menu_view("main"); ds = [d for r in rows for _, d in r]; assert "c:/cooldown" in ds and "m:blackout" in ds and any(d.startswith("q:/confirm2") for d in ds)
assert "Double-confirm" in b.summary_text()
print("FILTER TESTS PASSED")
