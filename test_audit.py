"""Signal IDs + hash-chained append-only audit log + /audit commands + onboarding (/howitworks, welcome)."""
import os, tempfile, json
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv",
                  HIST_GATE="off", BLACKOUT="off", NEWS_FILTER="off", DOUBLE_CONFIRM="off", PATTERNS="on", VOLUME_MODE="off",
                  PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002", BUG_CHANNEL_ID="-1003", SHADOW_CHANNEL_ID="-1004",
                  SHADOW_RELAXED="1", GROUP_FILTER="all", TELEGRAM_ADMIN_ID="42")
import numpy as np, pandas as pd, config as C, bot, analyzer as A, audit as AU, onboarding as OB
from datetime import timedelta

# 1) module: chain, verify, tamper detection, trail, export, never raises
T0 = pd.Timestamp("2026-10-15 10:00:00", tz="UTC")
for i in range(5): AU.event("created", id=f"ID{i}", pair="USDBRL", dir="CALL", payout=0.9, details={"i": i}, now=T0 + timedelta(hours=i * 8))
ok, n, msg = AU.verify(); assert ok and n == 5, msg
assert len(AU.files()) == 2
AU._last["hash"] = None   # simulated restart: chain continues from disk
AU.event("result", id="ID1", pair="USDBRL", details={"result": "WIN"}, now=T0 + timedelta(days=1, hours=2)); assert AU.verify()[0]
assert [r["event"] for r in AU.trail("id1")] == ["created", "result"]
p, k = AU.export(30, T0 + timedelta(days=1, hours=3)); assert k == 6 and len(open(p).readlines()) == 6
f0 = AU.files()[0]; orig = open(f0).read()
open(f0, "w").write(orig.replace('"i":0', '"i":7')); ok, _, msg = AU.verify(); assert not ok and "changed" in msg, msg
open(f0, "w").write("".join(orig.splitlines(True)[1:])); assert AU.verify()[0], "oldest line dropped = new anchor (retention)"
lines = orig.splitlines(True); open(f0, "w").write(lines[1] + lines[0]); assert not AU.verify()[0], "reorder detected"
open(f0, "w").write(orig); assert AU.verify()[0]
real = C.STATE_DIR; C.STATE_DIR = "/proc/forbidden/x"; assert AU.event("created", id="z") is None; C.STATE_DIR = real   # never raises
AU._last["hash"] = None
AU.rotate(T0 + timedelta(days=200)); assert AU.files() == []
AU._last["hash"] = None

# 2) bot: shadow signals get ids, capped/posted audited, ids in feedback, never shown in channel text
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv"))
SENT, ADMIN, DOCS = [], [], []
b.tg.group = lambda t, chats=None: SENT.extend((str(c[1]), t) for c in (chats or []))
b.tg.group_photo = lambda *a, **k: False
b.tg.admin = lambda t: ADMIN.append(t)
b.tg.admin_document = lambda path, caption="": DOCS.append(path) or True
T = pd.Timestamp("2026-10-15 10:00:02", tz="UTC"); b.d.now = lambda: T
r = np.random.default_rng(5); c = 1.1 + np.cumsum(r.normal(0, 1e-4, 200)); o = np.r_[c[0], c[:-1]]
df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c),
                  index=pd.date_range("2026-10-15 06:40", periods=200, freq="1min", tz="UTC"))
xx = A.compute(df); PAIRS = list(C.PAIRS)
b.dc_ok = lambda *a, **k: True; b.payout_gate = lambda p: (True, 0.9, "ok")
CONF = {PAIRS[0]: 60, PAIRS[1]: 80}
bot.analyze_relaxed_ensemble = lambda d_, pair, **k: {"signal": "CALL", "payout": .9, "confidence": CONF[pair], "regime": "trend", "agree": []}
b._shadow_defer = True
for p in PAIRS[:2]: b.shadow_eval(df, xx, p, 0.9, 0, T)
b._shadow_defer = False; b.shadow_flush()
import re
ids = {t["pair"]: t["id"] for t in b.shadow_open}; assert all(re.fullmatch(r"DBS-20261015-[0-9A-F]{4}", v) for v in ids.values()) and len(set(ids.values())) == 2, ids
ev = lambda sid: [x["event"] for x in AU.trail(sid)]
assert ev(ids[PAIRS[1]])[:1] == ["created"] and "posted" in ev(ids[PAIRS[1]]), ev(ids[PAIRS[1]])
assert "capped" in ev(ids[PAIRS[0]]) and "posted" not in ev(ids[PAIRS[0]])
for _, txt in SENT: assert not any(v in txt for v in ids.values()), "id leaked into a channel"
import feedback as FB
assert any(x.get("id") == ids[PAIRS[0]] and x["kind"] == "shadow_capped" for x in FB.read(1, T))
# result carries id into audit + CSV
for t in b.shadow_open: t["settle_at"] = T - timedelta(minutes=1); t["entry_at"] = df.index[-1]
b.cached_candles = lambda pair: df
b.shadow_resolve()
assert "result" in ev(ids[PAIRS[1]])
rows = open(C.SHADOW_LOG).read(); assert ids[PAIRS[1]] in rows
# pause / level / freeze events
b.command("/pause"); b.command("/resume"); b.set_level(3, "test")
evs = [x["event"] for x in AU.read(30, T)]; assert {"pause", "resume", "level"} <= set(evs)
# /audit commands
ADMIN.clear(); b.command("/audit verify"); assert "chain OK" in ADMIN[-1], ADMIN
b.command(f"/audit {ids[PAIRS[1]].lower()}"); assert "created" in ADMIN[-1] and "result" in ADMIN[-1]
b.command("/audit export 7d"); assert DOCS and DOCS[-1].endswith(".jsonl")
# audit dir is included in the daily backup
import zipfile, adminjobs
zp, _, _ = adminjobs.backup_zip(T); assert any(nm.startswith("audit/audit-") for nm in zipfile.ZipFile(zp).namelist())
# audit failure never breaks trading
orig_ev = AU.event; AU.event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk"))
b.audit("created", {"pair": "X"}); b.sid({"pair": "X"}); AU.event = orig_ev

# 3) onboarding
assert "77%" in OB.howitworks_text() and "@DBSdesk" in OB.howitworks_text() and "martingale" in OB.howitworks_text().lower()
for w in ("guaranteed profit", "100%", "sure win"): assert w not in OB.howitworks_text().lower() and w not in OB.welcome_text().lower()
b.tg.kb_log.clear(); ADMIN.clear()
assert b.sched_hook({"text": "/howitworks set Line ONE\nLine Two @DBSdesk"})
assert open(os.path.join(SD, "howitworks.txt")).read().startswith("Line ONE\nLine Two")
b.command("/howitworks"); m, pl = b.tg.kb_log[-1]; assert m == "sendMessage" and "Line ONE" in pl["text"] and "h:approve" in json.dumps(pl)
assert not any(str(x[1].get("chat_id")) == "-1001" for x in b.tg.kb_log), "nothing to public before approve"
calls = []
def fake_kb(method, payload): calls.append((method, payload)); return {"message_id": 555} if method == "sendMessage" else True
b.tg._kb_call = fake_kb; b.tg.on = True
b.on_callback({"from": {"id": 42}, "data": "h:approve", "id": "x", "message": {}}) if False else b.howitworks_cb("approve")
assert [x[0] for x in calls] == ["sendMessage", "pinChatMessage"] and calls[0][1]["chat_id"] == "-1001"
calls.clear(); b.howitworks_cb("approve"); assert [x[0] for x in calls] == ["editMessageText", "pinChatMessage"] and calls[0][1]["message_id"] == 555
# pin failure reported
os.remove(os.path.join(SD, "howitworks_state.json")); calls.clear(); ADMIN.clear()
b.tg._kb_call = lambda m_, p_: {"message_id": 9} if m_ == "sendMessage" else None
b.howitworks_cb("approve"); assert "PIN FAILED" in ADMIN[-1]
calls.clear(); b.tg._kb_call = fake_kb; b.howitworks_cb("skip"); assert not calls
b.tg.on = False
# welcome: non-admin private /start, once per 24h; admin and groups never
W = []; b.tg.send = lambda chat, text, label="": W.append((chat, text))
u = {"text": "/start", "chat": {"id": 777, "type": "private"}, "from": {"id": 777}}
assert b.welcome_user(u, 1000.0) and W[-1][0] == 777 and "@DBSdesk" in W[-1][1]
assert not b.welcome_user(u, 1000.0 + 3600) and len(W) == 1
assert b.welcome_user(u, 1000.0 + 25 * 3600) and len(W) == 2
assert not b.welcome_user({"text": "/start", "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}, 5.0)
assert not b.welcome_user({"text": "/start", "chat": {"id": -5, "type": "group"}, "from": {"id": 8}}, 5.0)
assert not b.welcome_user({"text": "hello", "chat": {"id": 9, "type": "private"}, "from": {"id": 9}}, 5.0)
assert b.sched_hook({"text": "/welcome set Hi there"}); ADMIN.clear(); b.command("/welcome preview"); assert "Hi there" in ADMIN[-1]
# 4) id uniqueness + config versioning
many = {AU.new_id(T) for _ in range(3000)}; assert len(many) == 3000
import configver as CV, adminjobs as AJ
v1 = b.config_check("t"); assert CV.check(b.config_runtime()) == v1, "no change → same version"
C.SHADOW_MAX_PER_MIN = 2; v2 = b.config_check("shadowcap"); assert v2 != v1 and int(v2[1:]) == int(v1[1:]) + 1
last = CV.history(1)[-1]; assert last["diff"]["SHADOW_MAX_PER_MIN"] == [1, 2]
b.set_level(4, "x"); v3 = b.config_check("lvl"); assert "runtime.level" in CV.history(1)[-1]["diff"]
assert not any(CV.is_secret(k) for r in CV.history() for k in list((r.get("full") or {})) + list(r.get("diff") or {}))
assert AU.read(30, T)[-1].get("config_version", "").startswith("v")
ADMIN.clear(); b.command("/config"); assert v3 in ADMIN[-1]; b.command("/config history"); assert "SHADOW_MAX_PER_MIN" in ADMIN[-1]
CV.CURRENT.update(version=None, hash=None, snap=None); assert CV.check(b.config_runtime()) == v3, "restart: same config → same version"
FB.log("vip_result", "x", pair="USDBRL", result="WIN", now=T, config_version="v7"); assert "v7: 1W/0L" in AJ.weekly_text(T + timedelta(hours=1))
zp, _, _ = AJ.backup_zip(T); assert "config_versions.jsonl" in zipfile.ZipFile(zp).namelist()
print("test_audit OK")
