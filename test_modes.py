"""Manual /stream /poll /auto /mode switching (mock feed, no network). Derived from the auto-mode dry-run: poll 12 pairs -> stream at 93% -> back to poll after midnight reset. Mocked feed, no network."""
import os, tempfile
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="otcharts", MODE="auto")
from datetime import datetime, timedelta, timezone
import pandas as pd
import config as C; C.USAGE_CHECK_SEC = 0
import bot, stream as S
UTC = timezone.utc
start = datetime(2026, 1, 5, 18, 0, tzinfo=UTC)
mock = S.MockTickSource([C.symbol_for(p) for p in C.PAIRS], start - timedelta(minutes=300), seed=3, tick_sec=10)
S.StreamAdapter.default_mock = mock
class FakePoll(bot.OTChartsAdapter):
    """Poll adapter fed by the same mocked prices; counts requests against a 7,000/day quota that resets at 00:00 UTC."""
    def __init__(self):
        self.cache, self.meta, self.meta_at, self.requests = {}, {}, 0.0, 0
        self.books = {p: S.CandleBook() for p in C.PAIRS}; self.sym = {C.symbol_for(p): p for p in C.PAIRS}
        self.used, self.day = 6400, None
        from resolver import resolve; self.res = {p: resolve(p, list(self.sym), "quotex") for p in C.PAIRS}
        mock.connect(self.feed)
    def feed(self, ev, d):
        if ev == "tick": self.books[self.sym[d["symbol"]]].on_tick(d["time"], d["price"])
    def now(self): return mock.now()
    def _count(self):
        if self.day != self.now().date(): 
            if self.day is not None: self.used = 0
            self.day = self.now().date()
        self.used += 1
    def candles(self, pair, include_forming=False):
        self._count(); df = self.books[pair].df()
        self.cache[pair] = df
        return (df if include_forming else bot.closed_only(df, self.now())), 0, True
    def fetch_candles(self, pair, limit):
        self._count(); df = self.books[pair].df(); return bot.closed_only(df, self.now()).iloc[-limit:]
    def pair_meta(self): return {p: (0.88, True) for p in C.PAIRS}
    def usage(self):
        if self.day != self.now().date(): self._count(); self.used -= 1
        return {"used": self.used, "quota": 7000}

import json, stream as S2
fp = FakePoll(); fp.used = 1000; mock.advance_to(start)
b = bot.Bot(fp); msgs = []
orig = b.tg.admin
b.tg.admin = lambda t: (msgs.append(t), orig(t))
def streams_open():   # StreamAdapter listeners on the mock = open streams in this process
    return sum(1 for f in mock.listeners if getattr(f, "__self__", None).__class__.__name__ == "StreamAdapter")
t = start; maxopen = 0; seq = ["/stream", "/poll", "/stream", "/stream", "/auto", "/stream", "/poll", "/mode", "/auto", "/poll", "/stream", "/poll"]
log_modes = []
for n in range(360):
    if n % 25 == 5 and seq:
        cmd = seq.pop(0); b.command(cmd)
    mock.advance_to(t.replace(second=C.SCAN_SECOND))
    b.tick()
    if b.pending and not b.pending["prechecked"]:
        mock.advance_to(b.pending["precheck_at"]); b.run_precheck()
    maxopen = max(maxopen, streams_open()); log_modes.append(type(b.d).__name__)
    t += timedelta(minutes=1)
switches = sum(1 for i in range(1, len(log_modes)) if log_modes[i] != log_modes[i-1])
print("adapter switches:", switches, "| max streams open at once:", maxopen, "| final:", type(b.d).__name__, b.mode_choice)
for m in msgs:
    if m[:1] in "🕹🔀🔁🤖⚙⛔⚠ℹ": print("  ", m.split(chr(10))[0][:140])
assert maxopen <= 1 and switches >= 6
saved = json.load(open(C.MODE_CHOICE_FILE)); print("persisted:", saved); assert saved["mode"] == b.mode_choice
# restart keeps the manual choice
b2 = bot.Bot(fp); print("after restart mode_choice:", b2.mode_choice, "want:", b2.want_mode)
assert b2.mode_choice == b.mode_choice
# /poll refused at stop threshold; manual poll safety fallback
b3 = bot.Bot(fp); b3.tg.admin = lambda t: msgs.append(t)
b3.command("/stream"); b3.tick(); assert b3.is_stream()
fp.used = 6700; b3.command("/poll"); print("refuse:", msgs[-1][:90]); assert msgs[-1].startswith("⛔ /poll refused")
fp.used = 6400; b3.command("/poll"); print("warn:", msgs[-1].replace(chr(10), " | ")[:160]); b3.tick(); assert not b3.is_stream()
fp.used = 6700; b3.usage_at = 0; b3.tick(); print("safety:", [m for m in msgs if "safety fallback" in m][-1][:110]); assert b3.is_stream()
print("streams open:", streams_open()); assert streams_open() <= 1
b3.command("/mode"); print(msgs[-1].replace(chr(10), " | "))
print("\nALL MODE TESTS PASSED")
