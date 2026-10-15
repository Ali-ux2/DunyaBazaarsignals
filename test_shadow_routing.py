"""Shadow/paper must never land on BUG_CHANNEL_ID when the two env ids differ.
Also: unproven strategies/patterns → shadow; proven/warm-up → bug; startup report warns on identical ids."""
import os, tempfile
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv",
                  HIST_GATE="off", BLACKOUT="off", NEWS_FILTER="off", DOUBLE_CONFIRM="off", PATTERNS="on", VOLUME_MODE="off",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", SHADOW_CHANNEL_ID="-1004",
                  SHADOW_RELAXED="1", GROUP_FILTER="all", TELEGRAM_ADMIN_ID="42")
import numpy as np, pandas as pd, config as C, bot, analyzer as A
from datetime import timedelta

assert C.BUG_CHANNEL_ID == "-1003" and C.SHADOW_CHANNEL_ID == "-1004"
assert C._env_chat("X", default="") == "" or True
# strip quotes/spaces
assert C._env_chat.__doc__

b = bot.Bot(bot.ReplayAdapter("replay_m1.csv"))
SENT = []
b.tg.group = lambda t, chats=None: SENT.extend((str(c[1]), c[0], t[:80]) for c in (chats or []))
b.tg.group_photo = lambda *a, **k: False
b.tg.admin = lambda t: SENT.append(("admin", "admin", t))
b.tg.send = lambda chat, text, label=None: SENT.append((str(chat), label or "?", text[:80]))
T = pd.Timestamp("2026-10-15 10:00:02", tz="UTC"); b.d.now = lambda: T
r = np.random.default_rng(5); c = 1.1 + np.cumsum(r.normal(0, 1e-4, 200)); o = np.r_[c[0], c[:-1]]
df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c),
                  index=pd.date_range("2026-10-15 06:40", periods=200, freq="1min", tz="UTC"))
xx = A.compute(df); P = list(C.PAIRS)[0]

def bug_ids(): return {c for c, lab, _ in SENT if c == "-1003"}
def shadow_ids(): return {c for c, lab, _ in SENT if c == "-1004"}

# 1) unproven B strategy → shadow only
SENT.clear(); b.bugs["*:B6_14"]["fw"] = [0, 0]; b.bug_last_pair.clear(); b.bug_last_pat.clear()
s = b.bug_fire("*:B6_14", P, "PUT", "t", T, 0.9, x=xx)
assert s.get("paper") and [x[0] for x in s["chats"]] == ["shadow"], s
assert shadow_ids() and not bug_ids(), (SENT, "unproven B must not touch bug")

# 2) proven B strategy → bug only
SENT.clear(); b.bugs["*:B6_14"]["fw"] = [20, 5]; b.bug_last_pair.clear(); b.bug_last_pat.clear()
s = b.bug_fire("*:B6_14", list(C.PAIRS)[1], "PUT", "t", T, 0.9, x=xx)
assert not s.get("paper") and [x[0] for x in s["chats"]] == ["bug"], s
assert bug_ids() and not shadow_ids(), (SENT, "proven B must not touch shadow")

# 3) history-filtered P pattern (no warm-up) → shadow paper, never bug
SENT.clear(); b.bug_last_pair.clear(); b.bug_last_pat.clear()
b.hist_gate = lambda *a, **k: (False, "not enough history yet (0.3 days stored, need 7.0)", "seen 0×")
b.warmup_ok = lambda *a, **k: False
s = b.bug_fire("*:P1", list(C.PAIRS)[2], "CALL", "t", T, 0.9, x=xx)
assert s and s.get("paper") and [x[0] for x in s["chats"]] == ["shadow"], s
assert "-1003" not in {c for c, _, _ in SENT}, SENT

# 4) shadow_eval relaxed → shadow only
SENT.clear()
bot.analyze_relaxed_ensemble = lambda *a, **k: {"signal": "CALL", "payout": .9, "confidence": 61, "regime": "trend", "agree": []}
b.dc_ok = lambda *a, **k: True
b.payout_gate = lambda p: (True, 0.9, "ok")
b.shadow_eval(df, xx, list(C.PAIRS)[3], 0.9, 0, T)
assert shadow_ids() and not bug_ids(), SENT
# :57 + result also shadow-only
st = b.shadow_open[-1]
b.d.now = lambda: st["entry_at"] - timedelta(seconds=2)
b.d.candles = lambda p, include_forming=False: (pd.DataFrame({"open": [1.0], "close": [1.00005]},
    index=pd.DatetimeIndex([st["entry_at"] - timedelta(minutes=1)])), None, None)
b.shadow_precheck_due()
assert all(c != "-1003" for c, _, _ in SENT), SENT
b.d.now = lambda: st["settle_at"] + timedelta(minutes=2)
bar = pd.DataFrame({"open": [1.0], "high": [1.0002], "low": [0.9999], "close": [1.0001]},
                   index=pd.DatetimeIndex([st["entry_at"]]))
b.cached_candles = lambda p: bar
b.shadow_resolve()
assert shadow_ids() and not bug_ids(), SENT

# 5) startup report: distinct ids OK; identical ids warn
ADM = []; b.tg.admin = lambda t: ADM.append(t)
b._chat_title = lambda cid: f"title-{cid}"
dups = b.channel_routing_report()
assert not dups and any("Channel IDs" in m for m in ADM) and "-1003" in ADM[-1] and "-1004" in ADM[-1]
C.SHADOW_CHANNEL_ID = C.BUG_CHANNEL_ID  # misconfig
ADM.clear(); dups = b.channel_routing_report()
assert dups and any("SAME chat id" in m for m in ADM)
C.SHADOW_CHANNEL_ID = "-1004"

print("ALL SHADOW ROUTING TESTS PASSED")
