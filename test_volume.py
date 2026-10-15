"""Volume mode (Tier 3): pacing 1/10 min, cap 6/h, history only 'not clearly negative', hard blocks, admin-only tier tag,
safeguard alert after ≥40 trades below breakeven (no auto-disable), /volume + menu + persistence, /stats /best weekly lines."""
import os, tempfile, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", GROUP_FILTER="all",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", HIST_GATE="all", BLACKOUT="off", NEWS_FILTER="off", DOUBLE_CONFIRM="off", PATTERNS="off", VOLUME_MODE="on")
import pandas as pd, config as C, bot
from datetime import timedelta
assert C.VOLUME_MODE and (C.VOLUME_TARGET, C.VOLUME_MAX_HOUR, C.VOLUME_SLOT_MIN, C.VOLUME_GUARD_N) == (4, 6, 10, 40)
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM = []; b.tg.admin = lambda t: ADM.append(t)
T = lambda m: pd.Timestamp(f"2026-10-15 10:{m:02d}:02", tz="UTC")
b.tier_sent = []; assert not b.volume_due(T(1)) and b.volume_due(T(4)), "slot start reserved for Tier 1/2"
b.tier_sent = [str(T(12))]; assert not b.volume_due(T(15)) and b.volume_due(T(24)), "max 1 per 10-min slot"
b.tier_sent = [str(T(k)) for k in (3, 13, 23, 33, 43, 53)]; assert not b.volume_due(T(55)), "cap 6/h"
b.tier_sent = [str(T(44))]; b.blocked_min = [T(k).floor("min") for k in range(50)]; assert not b.volume_due(T(54)), "no catch-up for blocked minutes"
b.blocked_min = []; assert b.volume_due(T(54))
b.blocked_min = []; b.tier_sent = []
b.streaks.s["live"]["until"] = (T(30) + timedelta(minutes=10)).isoformat(); assert not b.volume_due(T(34)), "hard blocks stay mandatory"
b.streaks.s["live"]["until"] = None
# selection: history below BE (Tier 2 refuses) but Wilson UB ≥ BE → Tier 3; clearly negative → nothing
P = list(C.PAIRS)
mk = lambda p, conf: {"pair": p, "signal": "CALL", "agree": ["B", "C"], "payout": .9, "atr": 1e-4, "confidence": conf, "strategies": {}}
b.hist.check = lambda p, nd, comp, now, pay: (setattr(b.hist, "last", {"n30": 20, "w30": 9, "n7": 0, "w7": 0}) or (False, "LB low", "seen"))
b.tier_sent = [str(T(1)), str(T(16))]   # Tier 2 not due at :18 (slot served) → volume
c1, c2 = mk(P[0], 70), mk(P[1], 85); best = b.select_tier([c1, c2], T(24))
assert best is c2 and best["tier"] == 3 and best["hist_line"] == "📙 volume" and "Tier-3 volume" in ADM[-1]
b.fill_pair_last.clear(); b.tier_sent = [str(T(1)), str(T(16))]; b.fill_pair_last[P[1]] = T(20)
assert b.select_tier([c1, c2], T(24)) is c1, "max 1 per pair per 10 min"
b.hist.check = lambda p, nd, comp, now, pay: (setattr(b.hist, "last", {"n30": 40, "w30": 10, "n7": 0, "w7": 0}) or (False, "bad", "seen"))
b.fill_pair_last.clear(); assert b.select_tier([c1, c2], T(24)) is None, "clearly negative never used"
C.VOLUME_MODE = False; b.hist.check = lambda p, nd, comp, now, pay: (setattr(b.hist, "last", {"n30": 20, "w30": 9, "n7": 0, "w7": 0}) or (False, "x", "y"))
b.fill_pair_last.clear(); assert b.select_tier([c1, c2], T(24)) is None, "volume off → no Tier 3"; C.VOLUME_MODE = True
# group card unchanged, tier tag only in the admin mirror
GRP = []; b.tg.group = lambda t, chats=None: GRP.append(t); b.tg.group_photo = lambda *a, **k: False
import analyzer as A
df = pd.read_csv("replay_m1.csv", parse_dates=["time"]); df["time"] = pd.to_datetime(df.time, utc=True); g = df[df.pair == df.pair.iloc[0]].set_index("time")[["open", "high", "low", "close"]]
x = A.compute(g); sig = None
for i in range(200, len(x)):
    with A._overrides(A.level_overrides(4)): r = A.ensemble(g.iloc[:i + 1], df.pair.iloc[0], payout=.9, x=x, i=i, use_gate=False)
    if r.get("signal"): sig = r; break
assert sig, "no replay signal"
sig.update(tier=3, hist_line="📙 volume", _df=g.iloc[:i + 1]); e = x.index[i] + timedelta(minutes=2)
sig.update(entry_at=e, settle_at=e + timedelta(minutes=1), precheck_at=e - timedelta(seconds=3), chats=b.chats_for("both"), level=4)
b.tg.chats = b.chats_for("both"); b.post_signal(sig); assert GRP, "card posted"
assert not any("Tier" in g or "volume" in g for g in GRP) and any("· Tier 3 · 📙 volume" in m for m in ADM)
# safeguard
for k in range(39): b.tier_record({"tier": 3, "payout": .9}, k % 2 == 0)
assert not any("below breakeven" in m for m in ADM)
b.tier_record({"tier": 3, "payout": .9}, False); assert sum("Tier 3 (volume) is below breakeven" in m and "/volume off" in m for m in ADM) == 1
b.tier_record({"tier": 3, "payout": .9}, False); assert sum("Tier 3 (volume) is below breakeven" in m for m in ADM) == 1, "alert once"
assert C.VOLUME_MODE, "never auto-disabled"
assert "Tier 3 📙 volume" in b.summary_text() and "Tier 3 📙 volume" in b.best_text()
b.weekly_report(); assert "Tier 3 📙 volume" in ADM[-1]
# command / menu / persistence
b.command("/volume off"); assert not C.VOLUME_MODE and "OFF" in ADM[-1] and json.load(open(C.TIERS_FILE))["volume"] is False
b2 = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); assert not C.VOLUME_MODE and b2.tier_total["3"] == [20, 21]
b.command("/volume on"); assert C.VOLUME_MODE
_, rows = b.menu_view("main"); assert any(d == "q:/volume off" for r in rows for _, d in r)
print("VOLUME TESTS PASSED")
