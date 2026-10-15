"""Auto-mode dry-run: poll 12 pairs -> stream at 93% -> back to poll after midnight reset. Mocked feed, no network."""
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
fp = FakePoll(); mock.advance_to(start)
b = bot.Bot(fp)
t = start
for n in range(420):           # 18:00 -> 01:00
    mock.advance_to(t.replace(second=C.SCAN_SECOND))
    b.tick()
    if b.pending and not b.pending["prechecked"]:
        mock.advance_to(b.pending["precheck_at"]); b.run_precheck()
    t += timedelta(minutes=1)
print("FINAL adapter:", type(b.d).__name__, "| pairs:", len(C.PAIRS))
