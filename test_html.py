"""Telegram HTML safety: every outgoing HTML payload (cards, :57, results for public/VIP/bug/shadow/paper, alerts, summaries,
menu, feedback, payout block, history/warm-up notes) passes a validator mimicking Telegram's parser; parse-error fallback to plain text;
bug/shadow routing goes to BUG_CHANNEL_ID / SHADOW_CHANNEL_ID with no per-signal bug/shadow text in the admin chat."""
import os, tempfile
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", HIST_GATE="off", BLACKOUT="off",
                  NEWS_FILTER="off", DOUBLE_CONFIRM="off", PATTERNS="off", VOLUME_MODE="off", PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002",
                  BUG_CHANNEL_ID="-1003", SHADOW_CHANNEL_ID="-1004", GROUP_FILTER="all", SHADOW_RELAXED="1", TELEGRAM_ADMIN_ID="42")
import pandas as pd, numpy as np, config as C, bot, tghtml, analyzer as A
from datetime import timedelta
from chart import signal_caption, result_caption
# 1. sanitizer / validator units (the live error: '< 77%', '< 0.3 ATR', '->', '&')
for raw in ["💸 Not sent: X payout 70% < 77% at send time", "opposite < 0.3 ATR, a > b", "x<y & z", "<\\x", "<b>P</b> <= 5 -> ok", "<i>unclosed"]:
    assert tghtml.validate(raw), raw; assert not tghtml.validate(tghtml.sanitize(raw)), (raw, tghtml.sanitize(raw))
assert tghtml.sanitize("<b>X</b> &lt; ok") == "<b>X</b> &lt; ok", "allowed tags and entities kept"
assert tghtml.to_plain("<b>a</b> &lt; b") == "a < b"
# 2. capture every real HTTP payload
POSTS = []
class R:
    def __init__(s, ok=True, code=200, text='{"ok":true,"result":{}}'): s.ok, s.status_code, s.text = ok, code, text
    def json(s): return {"ok": s.ok, "result": {"message_id": 1}}
FAIL = {"n": 0}
def post(url, json=None, data=None, files=None, timeout=None, **k):
    p = dict(json or data or {}); POSTS.append((url.rsplit("/", 1)[1], p))
    if FAIL["n"] and p.get("parse_mode"): FAIL["n"] -= 1; return R(False, 400, '{"ok":false,"description":"Bad Request: can\'t parse entities: Unsupported start tag \\"\\" at byte offset 361"}')
    return R()
bot.requests.post = post
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); b.tg.on = True; b.tg.base = "https://x/botTOKEN"
T = pd.Timestamp("2026-10-15 07:00:02", tz="UTC"); b.d.now = lambda: T
r = np.random.default_rng(5); c = 1.1 + np.cumsum(r.normal(0, 1e-4, 200)); o = np.r_[c[0], c[:-1]]
df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c), index=pd.date_range("2026-10-15 03:41", periods=200, freq="1min", tz="UTC")); xx = A.compute(df)
P = list(C.PAIRS)[0]
# cards / results for public, vip, bug, shadow, paper
b.tg.group(signal_caption(b.d.label(P), "PUT", T, 1, b.playbook("PUT"), 57, b.wait_line()), chats=[("public", "-1001"), ("vip", "-1002")])
b.tg.group(result_caption("WIN", b.d.label(P), "PUT", 1.0, 0.99), chats=[("vip", "-1002")])
b.bugs["*:B6_14"]["fw"] = [20, 5]; sig = b.bug_fire("*:B6_14", P, "PUT", "t < 0.3 ATR & x", T, 0.9, x=xx)          # proven → bug group
b.d.now = lambda: sig["entry_at"] - timedelta(seconds=2); b.cached_candles = lambda p: df
b.d.candles = lambda p, include_forming=False: (df.set_axis(pd.date_range(sig["entry_at"] - timedelta(minutes=200), periods=200, freq="1min", tz="UTC")), 0, True)
b.bug_precheck_due()
b.d.now = lambda: sig["settle_at"] + timedelta(minutes=2); b.cached_candles = lambda p: df.set_axis(pd.date_range(sig["entry_at"] - timedelta(minutes=150), periods=200, freq="1min", tz="UTC")); b.bug_resolve()
b.d.now = lambda: T
bot.analyze_relaxed_ensemble = lambda *a, **k: {"signal": "CALL", "payout": .9, "confidence": 61, "regime": "trend < 3", "agree": []}
b.dc_ok = lambda *a, **k: True
b.shadow_eval(df, xx, list(C.PAIRS)[1], 0.9, 0, T); st = b.shadow_open[-1]
b.d.now = lambda: st["entry_at"] - timedelta(seconds=2); b.shadow_precheck_due()
b.d.now = lambda: st["settle_at"] + timedelta(minutes=2); b.cached_candles = lambda p: df.set_axis(pd.date_range(st["entry_at"] - timedelta(minutes=150), periods=200, freq="1min", tz="UTC")); b.shadow_resolve()
b.payout_gate = lambda p: (False, 0.70, "payout 70% < 77%")                       # payout block alert (ensemble path text)
b.tg.admin("💸 Not sent: X payout 70% < 77% at send time · paper-tracked")
b.payout_gate = bot.Bot.payout_gate.__get__(b)
for cmd in ["/status", "/stats", "/history", "/feedback", "/patterns", "/filters", "/volume", "/dailystop", "/verbose", "/menu", "/best", "/bugs", "/help", "/publiccap", "/sched", "/reset"]:
    try: b.command(cmd)
    except Exception as e: print("cmd", cmd, "error", e)
b.tg.admin(b.summary_text()); b.drop_note("hist", "bug B2", "📚 Filtered by history: X · Wilson LB 40% < breakeven 53% + 5 pts")
for name in ("main", "level", "channel", "mode", "history"):
    try: t, rows = b.menu_view(name); b.tg.admin_kb(t, rows)
    except Exception as e: print("menu", name, e)
texts = [(m, p.get("text") or p.get("caption"), p.get("chat_id")) for m, p in POSTS if p.get("parse_mode") == "HTML"]
print("HTML payloads checked:", len(texts))
bad = [(m, cid, tghtml.validate(t), t[:80]) for m, t, cid in texts if t and tghtml.validate(t)]
assert not bad, bad[:5]
chats = {str(p.get("chat_id")) for _, p in POSTS}; print("chats:", sorted(chats))
assert "-1003" in chats and "-1004" in chats, "bug + shadow channels really receive"
adm = [p.get("text", "") for _, p in POSTS if str(p.get("chat_id")) == str(C.TELEGRAM_ADMIN_ID) and not p.get("text", "").startswith("📊")]   # summaries may hold the shadow section
assert not any(s in t for t in adm for s in ("BUG SIGNAL", "Bug result", "Bug :57", "👻 Shadow", "Shadow result")), "no per-signal bug/shadow text in admin"
# 3. fallback: 400 parse error → one plain-text retry
POSTS.clear(); FAIL["n"] = 1; b.tg.send("-1003", "<b>X</b> ok")
assert len(POSTS) == 2 and "parse_mode" in POSTS[0][1] and "parse_mode" not in POSTS[1][1] and POSTS[1][1]["text"] == "X ok"
print("ALL HTML TESTS PASSED")
