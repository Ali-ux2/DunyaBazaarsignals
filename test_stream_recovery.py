"""Stream drop + 429 'stream in use' + recovery, with a fake HTTP connector (no network)."""
import os, tempfile, threading, time, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="otcharts", MODE="stream")
import config as C, stream as S, pandas as pd, analyzer as A
C.STREAM_429_BACKOFFS = [0.3, 0.6, 1.0, 1.5]; C.STREAM_STALE_SEC = 1.0; C.STREAM_WATCHDOG_SEC = 0.2; C.STREAM_PAIRS = ["USDINR", "USDMXN"]
OPEN = {"now": 0, "max": 0, "total": 0}; LOCK = threading.Lock()
IN_USE = '{"error":"your plan allows one live stream at once, across all your keys, and 1 is open. Close a stream before opening another"}'
QUOTA = '{"error":"daily requests exhausted: 7000 of 7000 used today"}'
class FakeResp:
    def __init__(self, status, text="", lines=None, hang=False):
        self.status_code, self.text, self.headers = status, text, {}
        self._lines, self._hang, self._closed = lines or [], hang, threading.Event()
        if status == 200:
            with LOCK: OPEN["now"] += 1; OPEN["total"] += 1; OPEN["max"] = max(OPEN["max"], OPEN["now"])
    def iter_lines(self, decode_unicode=True):
        for l in self._lines:
            if self._closed.is_set(): raise ConnectionError("closed")
            yield l; time.sleep(0.01)
        if self._hang:
            while not self._closed.wait(0.05): yield ": keep-alive"      # comments only, no ticks
            raise ConnectionError("socket closed")
    def close(self):
        if not self._closed.is_set():
            self._closed.set()
            if self.status_code == 200:
                with LOCK: OPEN["now"] -= 1
def ticks(n, hang):
    t0 = time.time(); out = ['event: connected', 'data: {"symbols":["USDINR_otc","USDMXN_otc"]}', '']
    for k in range(n):
        for s in ("USDINR_otc", "USDMXN_otc"): out += ['event: tick', f'data: {json.dumps({"symbol": s, "price": 88+k*0.001, "time": t0+k})}', '']
    return out
SCRIPT = []
def connector(venue, syms): return SCRIPT.pop(0)() if SCRIPT else FakeResp(200, lines=ticks(5, True), hang=True)
S.SSEStream.connect = staticmethod(connector)

class FakeRest:
    res, cache = {}, {}
    def __init__(self, used=1000): self.used, self.calls = used, 0
    def usage(self): return {"used": self.used, "quota": 7000}
    def usage_raw(self): return {"requests": self.usage(), "streams": {"open": 1 if OPEN["now"] else 0, "limit": 1}}
    def fetch_candles(self, p, limit): return A.synthetic(n=limit, seed=2, t0=(pd.Timestamp.now("UTC").floor("min") - pd.Timedelta(minutes=limit)).isoformat())
    def candles(self, pair, include_forming=False):
        self.calls += 1; self.used += 1
        return A.synthetic(n=60, seed=3, t0=(pd.Timestamp.now("UTC").floor("min") - pd.Timedelta(minutes=60)).isoformat()), 0, True
    def pair_meta(self): return {p: (0.88, True) for p in C.STREAM_PAIRS}

def run(name, script, used):
    global SCRIPT; SCRIPT = list(script); OPEN.update(now=0, max=0, total=0)
    rest = FakeRest(used); a = S.StreamAdapter(rest=rest); msgs = []
    fb_calls = None; t_end = time.time() + 9
    while time.time() < t_end:
        if a.down is not None:
            a.candles("USDINR"); fb_calls = rest.calls
        msgs += a.admin_messages(); time.sleep(0.1)
    a.close(); msgs += a.admin_messages()
    alerts = [m for m in msgs if m[:1] in "🔴🟢🔁⏸⚠"]
    print(f"\n[{name}] connections opened={OPEN['total']} max_concurrent={OPEN['max']} open_after_close={OPEN['now']} fallback_REST_calls={rest.calls}")
    for m in alerts: print("   ", m)
    return alerts
r429 = lambda body: (lambda: FakeResp(429, body))
ok_then_silent = lambda: FakeResp(200, lines=ticks(5, True), hang=True)
al = run("drop -> 3x 429 in_use -> recover", [ok_then_silent, r429(IN_USE), r429(IN_USE), r429(IN_USE)], 1000)
assert OPEN["max"] == 1 and OPEN["now"] == 0
assert sum(m.startswith("🔴 Stream down: OTCharts says a stream is still open") for m in al) == 1, al
assert sum(m.startswith("🟢 Stream reconnected") for m in al) >= 1
assert sum(m.startswith("🔁") for m in al) == 1
al = run("429 quota, REST budget exhausted", [r429(QUOTA)] * 3, 6990)
assert sum("quota exhausted" in m for m in al) == 1 and sum(m.startswith("⏸") for m in al) == 1 and OPEN["max"] <= 1
print("\nclassify:", S.classify_429(IN_USE), S.classify_429(QUOTA)); assert S.classify_429(IN_USE) == "in_use" and S.classify_429(QUOTA) == "quota"
# singleton: a second adapter takes over and the first connection is closed
SCRIPT = []; OPEN.update(now=0, max=0, total=0)
a1 = S.StreamAdapter(rest=FakeRest()); time.sleep(0.5); a2 = S.StreamAdapter(rest=FakeRest()); time.sleep(1.0)
print("two adapters -> concurrent open:", OPEN["now"], "max:", OPEN["max"]); assert OPEN["now"] == 1
a1.close(); a2.close(); time.sleep(0.3); print("after close:", OPEN["now"]); assert OPEN["now"] == 0
print("\nALL STREAM RECOVERY TESTS PASSED")
