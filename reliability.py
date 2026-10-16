"""Reliability & safety: API circuit breaker, memory + volume checks, data-degradation state, Telegram flood gate,
deadman watchdog and a tiny /health HTTP endpoint. Every entry point swallows its own errors: none of this may
crash or stall trading."""
import os, time, json, logging, threading, collections
import config as C
log = logging.getLogger("reliability")

# ---------------- circuit breaker ----------------
class Breaker:
    """Trips when API error rate > TRIP over WINDOW (min MIN_CALLS); resets once rate < RESET for HOLD seconds."""
    def __init__(self, window=600, min_calls=10, trip=0.20, reset=0.10, hold=300):
        self.window, self.min_calls, self.trip, self.reset, self.hold = window, min_calls, trip, reset, hold
        self.calls = collections.deque(); self.tripped = False; self.good_since = None; self.lock = threading.Lock()
    def record(self, ok, now=None):
        try:
            with self.lock: self.calls.append((now or time.time(), bool(ok)))
        except Exception: pass
    def rate(self, now=None):
        now = now or time.time()
        with self.lock:
            while self.calls and self.calls[0][0] < now - self.window: self.calls.popleft()
            n = len(self.calls); bad = sum(1 for _, ok in self.calls if not ok)
        return n, (bad / n if n else 0.0)
    def update(self, now=None):
        """Returns 'tripped' / 'reset' on a transition, else None."""
        now = now or time.time(); n, r = self.rate(now)
        if not self.tripped:
            if n >= self.min_calls and r > self.trip: self.tripped = True; self.good_since = None; return "tripped"
            return None
        if r < self.reset:
            if self.good_since is None: self.good_since = now
            if now - self.good_since >= self.hold: self.tripped = False; self.good_since = None; return "reset"
        else: self.good_since = None
        return None

# ---------------- memory ----------------
def _readint(p):
    try:
        v = open(p).read().strip()
        return None if v in ("max", "") else int(v)
    except Exception: return None
def memory_usage():
    """(used_bytes, limit_bytes, source) from cgroup v2 / v1, else process RSS vs MemTotal."""
    u, l = _readint("/sys/fs/cgroup/memory.current"), _readint("/sys/fs/cgroup/memory.max")
    if u and l: return u, l, "cgroup"
    u, l = _readint("/sys/fs/cgroup/memory/memory.usage_in_bytes"), _readint("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if u and l and l < 1 << 60: return u, l, "cgroup"
    rss = tot = None
    try:
        for ln in open("/proc/self/status"):
            if ln.startswith("VmRSS:"): rss = int(ln.split()[1]) * 1024
        for ln in open("/proc/meminfo"):
            if ln.startswith("MemTotal:"): tot = int(ln.split()[1]) * 1024
    except Exception: pass
    return rss, tot, "rss"

# ---------------- volume ----------------
def volume_problem(state_dir=None, env_set=None):
    """None if STATE_DIR is OK, else a human-readable problem."""
    d = os.path.abspath(state_dir or C.STATE_DIR)
    env_set = bool(os.environ.get("STATE_DIR")) if env_set is None else env_set
    if not os.path.isdir(d): return f"STATE_DIR {d} does not exist"
    try:
        p = os.path.join(d, ".rw_probe"); open(p, "w").write(str(time.time())); os.remove(p)
    except Exception as e: return f"STATE_DIR {d} is not writable ({e})"
    if not env_set or (state_dir or C.STATE_DIR) in (".", ""): return f"STATE_DIR not set: using {d} (temporary, wiped on redeploy) — add a volume + STATE_DIR=/data"
    if not os.path.ismount(d) and not os.environ.get("STATE_DIR_NO_MOUNT_CHECK"): return f"STATE_DIR {d} is not a mounted volume (data would be lost on redeploy)"
    return None

# ---------------- Telegram flood gate ----------------
class FloodGate:
    """Token buckets: per chat ~1 msg/s, groups/channels ~20/min, global ~25/s. acquire() returns seconds to wait (0 = go)
    and consumes when 0. Priority messages (:57 lines, results) ignore the per-minute group bucket."""
    def __init__(self, per_chat_s=1.0, group_per_min=20, global_per_s=25):
        self.per_chat_s, self.group_per_min, self.global_per_s = per_chat_s, group_per_min, global_per_s
        self.last = {}; self.minute = collections.defaultdict(collections.deque); self.glob = collections.deque()
        self.blocked_until = {}; self.lock = threading.Lock()
    @staticmethod
    def is_group(chat): return str(chat).startswith("-")
    def wait_for(self, chat, prio=False, now=None):
        now = now or time.time(); c = str(chat); w = 0.0
        with self.lock:
            w = max(w, self.blocked_until.get(c, 0) - now)
            w = max(w, self.last.get(c, 0) + self.per_chat_s - now)
            while self.glob and self.glob[0] < now - 1: self.glob.popleft()
            if len(self.glob) >= self.global_per_s: w = max(w, self.glob[0] + 1 - now)
            if self.is_group(c) and not prio:
                q = self.minute[c]
                while q and q[0] < now - 60: q.popleft()
                if len(q) >= self.group_per_min: w = max(w, q[0] + 60 - now)
            return max(w, 0.0)
    def take(self, chat, now=None):
        now = now or time.time(); c = str(chat)
        with self.lock:
            self.last[c] = now; self.glob.append(now)
            if self.is_group(c): self.minute[c].append(now)
    def penalize(self, chat, retry_after, now=None):
        with self.lock: self.blocked_until[str(chat)] = (now or time.time()) + float(retry_after or 1)

PRIO_WORDS = ("CONFIRMED", "SWITCH", "SKIP", "WIN", "LOSS", "TIE", "Result", "RESULT")
def is_priority(text): head = (text or "")[:120]; return any(w in head for w in PRIO_WORDS)

class SendQueue:
    """Background queue for messages that would otherwise wait too long: nothing is dropped; priority first."""
    def __init__(self, sender):
        self.sender = sender; self.q = []; self.cv = threading.Condition(); self.seq = 0; self.th = None
    def put(self, prio, args):
        with self.cv:
            self.seq += 1; self.q.append((0 if prio else 1, self.seq, args)); self.q.sort(); self.cv.notify()
        if self.th is None or not self.th.is_alive():
            self.th = threading.Thread(target=self._run, daemon=True, name="tg-queue"); self.th.start()
    def __len__(self): return len(self.q)
    def _run(self):
        while True:
            with self.cv:
                while not self.q: self.cv.wait(30)
                _, _, args = self.q.pop(0)
            try: self.sender(*args)
            except Exception as e: log.warning("queued send failed: %s", e)

# ---------------- health / deadman ----------------
class Health:
    def __init__(self, max_age=600):
        self.max_age = max_age; self.last_tick = time.time(); self.alerted = False
    def beat(self): self.last_tick = time.time(); self.alerted = False
    def age(self): return time.time() - self.last_tick
    def payload(self):
        a = self.age(); ok = a <= self.max_age
        return (200 if ok else 503), json.dumps({"ok": ok, "last_tick_age_s": round(a, 1), "max_age_s": self.max_age})

def start_watchdog(health, alert, stop, every=30):
    def run():
        while not stop.wait(every):
            try:
                if health.age() > health.max_age and not health.alerted:
                    health.alerted = True; alert(f"💀 Deadman: main loop has not ticked for {int(health.age() // 60)} min. Check Railway logs / restart the worker.")
            except Exception as e: log.warning("watchdog: %s", e)
    t = threading.Thread(target=run, daemon=True, name="watchdog"); t.start(); return t

def start_health_server(health, port=None):
    port = port or os.environ.get("PORT")
    if not port: return None
    try:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                code, body = health.payload() if self.path.split("?")[0] in ("/health", "/") else (404, '{"error":"not found"}')
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body.encode())
            def do_HEAD(self):
                code, _ = health.payload(); self.send_response(code); self.end_headers()
            def log_message(self, *a): pass
        srv = ThreadingHTTPServer(("0.0.0.0", int(port)), H)
        threading.Thread(target=srv.serve_forever, daemon=True, name="health").start()
        log.info("health endpoint on :%s/health", port); return srv
    except Exception as e: log.warning("health server not started: %s", e); return None

# ---------------- Bot mixin ----------------
API = Breaker()   # shared: OTCharts adapter records, Bot reads

class Reliability:
    """Mixed into Bot. rel_init() in __init__, rel_minute() each tick, rel_blocks_signals() gates NEW signals only."""
    def rel_init(self):
        self.breaker = API; self.health = Health(); self.data_state = "paid"; self._no_data_ticks = 0
        self._rel_alerted = {}
    def _rel_alert(self, key, text, every=1800):
        now = time.time()
        if now - self._rel_alerted.get(key, 0) < every: return False
        self._rel_alerted[key] = now
        try: self.tg.admin(text)
        except Exception as e: log.warning("rel alert: %s", e)
        try: self.audit("reliability", kind=key, text=text)
        except Exception: pass
        return True
    def rel_minute(self):
        try:
            self.health.beat()
            tr = self.breaker.update(); n, r = self.breaker.rate()
            if tr == "tripped": self._rel_alert("breaker", f"🧯 API circuit breaker TRIPPED: {r:.0%} errors over the last 10 min ({n} calls). New signals paused; open trades still settle. Auto-resumes when errors < 10% for 5 min.", 0)
            elif tr == "reset": self._rel_alert("breaker_ok", f"✅ API circuit breaker reset ({r:.0%} errors). New signals resumed.", 0)
            if getattr(C, "DATA_SOURCE", "") == "replay" and not getattr(self, "rel_force_checks", False): return   # backtests/tests: no host alerts
            u, l, src = memory_usage()
            if u and l and u / l > 0.85: self._rel_alert("memory", f"🧠 Memory high: {u / l:.0%} of limit ({u >> 20} / {l >> 20} MB, {src}).", 1800)
            vp = volume_problem()
            if vp: self._rel_alert("volume", f"💾 Volume problem: {vp}", 1800)
        except Exception as e: log.warning("reliability minute: %s", e)
    def rel_data_state(self, fresh_pairs):
        """Called after each scan: paid → stream → none (no live data = no new signals). Alerts on every transition."""
        try:
            self._no_data_ticks = 0 if fresh_pairs else self._no_data_ticks + 1
            st = "none" if self._no_data_ticks >= 2 else ("stream" if self.is_stream() else "paid")
            if st != self.data_state:
                old, self.data_state = self.data_state, st
                msg = {"none": "⛔ No live data from any source — new signals paused (cached data is never used for signals; only to settle results).",
                       "stream": "📡 Data: now on the stream (paid API unavailable/over budget).",
                       "paid": "✅ Data: live paid API data back."}[st]
                self._rel_alert("data_" + st, f"{msg} ({old} → {st})", 0)
        except Exception as e: log.warning("data state: %s", e)
    def rel_blocks_signals(self):
        return bool(getattr(self, "breaker", None) and self.breaker.tripped)
    def rel_line(self):
        try:
            n, r = self.breaker.rate(); u, l, _ = memory_usage()
            return (f"Breaker: {'TRIPPED' if self.breaker.tripped else 'ok'} ({r:.0%} errors, {n} calls/10m) · data: {self.data_state}"
                    + (f" · mem {u / l:.0%}" if u and l else "") + f" · last tick {int(self.health.age())}s ago")
        except Exception: return "Reliability: n/a"
