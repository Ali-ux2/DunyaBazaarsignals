import os; os.environ.setdefault("BUG_GATE", "off"); os.environ.setdefault("HOLDOUT", "off")   # legacy test of the pre-gate promotion rules (gate: test_buggate.py)
"""Shadow channel cap: at most SHADOW_MAX_PER_MIN cards per entry minute, strongest wins; the rest are silent paper.
VIP/public/bug untouched; /shadowcap persisted in STATE_DIR and shown in /status."""
import os, tempfile, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv",
                  HIST_GATE="off", BLACKOUT="off", NEWS_FILTER="off", DOUBLE_CONFIRM="off", PATTERNS="on", VOLUME_MODE="off",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", SHADOW_CHANNEL_ID="-1004",
                  SHADOW_RELAXED="1", GROUP_FILTER="all", TELEGRAM_ADMIN_ID="42")
import numpy as np, pandas as pd, config as C, bot, analyzer as A
from datetime import timedelta

assert C.SHADOW_MAX_PER_MIN == 1
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv"))
SENT, ADMIN = [], []
b.tg.group = lambda t, chats=None: SENT.extend((str(c[1]), t) for c in (chats or []))
b.tg.group_photo = lambda *a, **k: False
b.tg.admin = lambda t: ADMIN.append(t)
T = pd.Timestamp("2026-10-15 10:00:02", tz="UTC"); b.d.now = lambda: T
r = np.random.default_rng(5); c = 1.1 + np.cumsum(r.normal(0, 1e-4, 200)); o = np.r_[c[0], c[:-1]]
df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c),
                  index=pd.date_range("2026-10-15 06:40", periods=200, freq="1min", tz="UTC"))
xx = A.compute(df); PAIRS = list(C.PAIRS)
b.dc_ok = lambda *a, **k: True
b.payout_gate = lambda p: (True, 0.9, "ok")
CONF = {}
bot.analyze_relaxed_ensemble = lambda d_, pair, **k: {"signal": "CALL", "payout": .9, "confidence": CONF[pair], "regime": "trend", "agree": []}
def shadow_cards(): return [t for ch, t in SENT if ch == "-1004" and ("⏰ Entry" in t or "ENTER NOW" in t)]

# 1) three relaxed candidates in one tick → only the strongest (conf 80) posted; others silent paper
CONF.update({PAIRS[0]: 60, PAIRS[1]: 80, PAIRS[2]: 70})
b._shadow_defer = True
for p in PAIRS[:3]: b.shadow_eval(df, xx, p, 0.9, 0, T)
assert not shadow_cards(), "deferred until flush"
b._shadow_defer = False; b.shadow_flush()
cards = shadow_cards(); assert len(cards) == 1, cards
lab = b.d.label(PAIRS[1]); assert any(w in cards[0] for w in (bot.display_name(lab), lab, PAIRS[1])), cards
opn = {t["pair"]: t for t in b.shadow_open}
assert opn[PAIRS[1]]["chan"] and not opn[PAIRS[1]].get("capped")
for p in (PAIRS[0], PAIRS[2]): assert opn[p].get("capped") and not opn[p]["chan"] and opn[p]["prechecked"], opn[p]
assert len(b.shadow_open) == 3, "all three still tracked as paper"

# 2) later candidate for the same entry minute → capped too (slot already used)
CONF[PAIRS[3]] = 99; SENT.clear()
b.shadow_eval(df, xx, PAIRS[3], 0.9, 0, T)
assert not shadow_cards() and b.shadow_open[-1].get("capped")

# 3) capped results: stats recorded, nothing in shadow, no admin spam
SENT.clear(); ADMIN.clear(); n0 = sum(sum(v) for v in b.shadow_day.values()); c0 = sum(b.shadow_capres_day[:2])
st = b.shadow_open[0]; b.d.now = lambda: st["settle_at"] + timedelta(seconds=5)
b.cached_candles = lambda p: df.copy().set_axis(pd.date_range(st["entry_at"] - timedelta(minutes=199), periods=200, freq="1min", tz="UTC"))
b.d.candles = lambda p, include_forming=False: (b.cached_candles(p), 0, True)
b.shadow_resolve()
# posted-only shadow stats: the 1 posted counts in shadow_day, the capped ones only on the 'capped (not sent)' line
assert sum(sum(v) for v in b.shadow_day.values()) - n0 + sum(b.shadow_capres_day[:2]) - c0 >= 3, (b.shadow_day, b.shadow_capres_day)
assert sum(sum(v) for v in b.shadow_day.values()) - n0 <= 1 and sum(b.shadow_capres_day[:2]) - c0 >= 2, (b.shadow_day, b.shadow_capres_day)
assert len([t for ch, t in SENT if ch == "-1004"]) == 1, SENT   # only the posted one's result
assert not any("Shadow result" in a for a in ADMIN), ADMIN
b.d.now = lambda: T

# 4) bug paper competes in the same cap; bug-group signal unaffected
b.shadow_slots.clear(); SENT.clear(); b.bug_last_pair.clear(); b.bug_last_pat.clear()
b.bugs["*:B6_14"]["fw"] = [0, 0]
b._shadow_defer = True
CONF[PAIRS[4]] = 50; b.shadow_eval(df, xx, PAIRS[4], 0.9, 0, T + timedelta(minutes=5))
T2 = T + timedelta(minutes=5)
s_paper = b.bug_fire("*:B6_14", PAIRS[5], "PUT", "t", T2, 0.9, x=xx)
b.bugs["*:B6_15"] = dict(b.bugs.get("*:B6_15") or b.bugs["*:B6_14"]); b.bugs["*:B6_15"]["fw"] = [20, 5]
s_bug = b.bug_fire("*:B6_15", PAIRS[6], "PUT", "t", T2, 0.9, x=xx)
b._shadow_defer = False; b.shadow_flush()
assert s_bug and [c[0] for c in s_bug["chats"]] == ["bug"] and any(ch == "-1003" for ch, _ in SENT), "bug group unaffected"
assert len(shadow_cards()) == 1, shadow_cards()
assert s_paper.get("capped") and not s_paper["group"] and s_paper["silent"] and s_paper in b.bug_sigs, "bug paper still tracked"

# 5) unlimited (0) → everything posts; /shadowcap persisted + in /status
b.shadowcap_cmd(["0"]); assert C.SHADOW_MAX_PER_MIN == 0
assert json.load(open(C.SHADOW_CAP_FILE))["cap"] == 0
b.shadow_slots.clear(); SENT.clear(); b.shadow_open.clear(); b.shadow_next.clear()
b._shadow_defer = True
for p in PAIRS[:3]: b.shadow_eval(df, xx, p, 0.9, 0, T + timedelta(minutes=10))
b._shadow_defer = False; b.shadow_flush()
assert len(shadow_cards()) == 3
b.shadowcap_cmd(["1"]); C.SHADOW_MAX_PER_MIN = 5; b.load_shadowcap(); assert C.SHADOW_MAX_PER_MIN == 1
assert "Shadow cap: 1" in b.status()

# 6) payout floor still fails closed for shadow
b.payout_gate = lambda p: (False, None, "payout unknown"); SENT.clear(); b.shadow_slots.clear(); b.shadow_next.clear(); b.shadow_open.clear()
b._shadow_defer = True; b.shadow_eval(df, xx, PAIRS[0], 0.9, 0, T + timedelta(minutes=20)); b._shadow_defer = False; b.shadow_flush()
assert not shadow_cards()
print("test_shadow_cap OK")
