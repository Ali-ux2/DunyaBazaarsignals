"""Circuit breaker, memory/volume checks, data degradation, Telegram flood gate + 429, deadman + /health."""
import os, tempfile, time, json, threading, urllib.request
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", TELEGRAM_ADMIN_ID="42")
import config as C, reliability as RL, bot

# breaker: trip >20% over >=10 calls; reset only after <10% for 5 min
b = RL.Breaker(); t0 = 1000.0
for i in range(9): b.record(False, t0)
assert b.update(t0) is None, "min 10 calls"
for i in range(11): b.record(True, t0 + 1)
assert b.update(t0 + 2) == "tripped" and b.tripped   # 9/20 = 45%
assert b.update(t0 + 700) is None and b.tripped       # window empty → rate 0 → hold starts
assert b.update(t0 + 700 + 299) is None and b.tripped
assert b.update(t0 + 700 + 301) == "reset" and not b.tripped

# volume
assert RL.volume_problem(SD, env_set=True)   # tmp dir is not a mount
assert "not set" in RL.volume_problem(".", env_set=False)
assert "does not exist" in RL.volume_problem("/nonexistent/x", env_set=True)
os.environ["STATE_DIR_NO_MOUNT_CHECK"] = "1"; assert RL.volume_problem(SD, env_set=True) is None; del os.environ["STATE_DIR_NO_MOUNT_CHECK"]
u, l, src = RL.memory_usage(); assert src in ("cgroup", "rss")

# flood gate
g = RL.FloodGate(); now = 5000.0
assert g.wait_for("-100", now=now) == 0; g.take("-100", now=now)
assert 0.9 < g.wait_for("-100", now=now + 0.05) <= 1.0, "1 msg/s per chat"
for i in range(19): g.take("-100", now=now + 1.1 * (i + 1))
w = g.wait_for("-100", now=now + 30); assert w > 1, "20/min per channel"
assert g.wait_for("-100", prio=True, now=now + 30) == 0, ":57/result skip the per-minute bucket"
g.penalize("-100", 7, now=now + 30); assert g.wait_for("-100", prio=True, now=now + 30) >= 6.9, "429 retry_after honoured"
assert RL.is_priority("✅ CONFIRMED X CALL — enter now") and not RL.is_priority("⏰ Entry 10:01")

# queue: never drops, priority first
out = []; ev = threading.Event()
q = RL.SendQueue(lambda *a: (out.append(a[0]), len(out) == 3 and ev.set()))
q.q = [(1, 1, ("card",)), (0, 2, ("result",))]; q.put(True, ("conf",)); ev.wait(3)
assert out[0] in ("result", "conf") and out[-1] == "card" and len(out) == 3, out

# Telegram 429 handling (mocked HTTP)
class R:
    def __init__(s, code, j): s.status_code = code; s._j = j; s.ok = code == 200; s.text = json.dumps(j)
    def json(s): return s._j
calls = []
def fake_post(url, json=None, **k):
    calls.append(json["text"]); return R(429, {"ok": False, "parameters": {"retry_after": 0.2}}) if len(calls) == 1 else R(200, {"ok": True, "result": {"message_id": 7}})
tg = bot.Telegram(); tg.on = True; tg.gate = RL.FloodGate()
orig = bot.requests.post; bot.requests.post = fake_post
try:
    tg.last_posted = []; tg.send("-1001", "✅ CONFIRMED USDBRL CALL — enter now", "vip")
    assert calls == ["✅ CONFIRMED USDBRL CALL — enter now"] * 2 and tg.last_posted == [("vip", 7)]
finally: bot.requests.post = orig

# bot: breaker blocks NEW signals only; data degradation alerts; deadman/health
B = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); A = []; B.tg.admin = lambda t: A.append(t)
B.breaker = RL.Breaker()
for i in range(10): B.breaker.record(False)
B.rel_minute(); assert B.breaker.tripped and any("TRIPPED" in a for a in A) and B.rel_blocks_signals()
B.is_stream = lambda: False
B.rel_data_state(0); assert B.data_state == "paid"
B.rel_data_state(0); assert B.data_state == "none" and "never used for signals" in A[-1]
B.is_stream = lambda: True; B.rel_data_state(3); assert B.data_state == "stream" and "stream" in A[-1]
B.is_stream = lambda: False; B.rel_data_state(3); assert B.data_state == "paid"
B.rel_force_checks = True; A.clear(); B.rel_minute(); assert any("Volume problem" in a for a in A); n = len(A); B.rel_minute(); assert len(A) == n, "rate-limited"
assert "Breaker" in B.rel_line() and "Breaker" in B.status()
B.tg.admin = lambda t: (_ for _ in ()).throw(RuntimeError("tg down")); B._rel_alerted.clear(); B.rel_minute()   # never raises

h = RL.Health(max_age=600); h.last_tick -= 700; st = threading.Event(); got = []
RL.start_watchdog(h, got.append, st, every=0.05); time.sleep(0.3); st.set(); assert len(got) == 1 and "Deadman" in got[0]
h.beat(); srv = RL.start_health_server(h, port=18765)
r = urllib.request.urlopen("http://127.0.0.1:18765/health"); assert r.status == 200 and json.loads(r.read())["ok"]
h.last_tick -= 1000
try: urllib.request.urlopen("http://127.0.0.1:18765/health"); assert False
except urllib.error.HTTPError as e: assert e.code == 503
srv.shutdown()
assert RL.start_health_server(h, port=None) is None or os.environ.get("PORT")
print("test_reliability OK")
