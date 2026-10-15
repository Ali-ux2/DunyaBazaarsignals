"""Bench after 2 lost ladders + stream swap + midnight restore (mock feed). Derived from the auto-mode dry-run: poll 12 pairs -> stream at 93% -> back to poll after midnight reset. Mocked feed, no network."""
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

import json
fp = FakePoll(); fp.used = 1000; mock.advance_to(start)
pay = {p: 0.80 for p in C.PAIRS}; pay.update(USDPHP=0.84, USDARS=0.87, USDNGN=0.70, USDEGP=0.90)
fp.meta = {p: (pay[p], True) for p in C.PAIRS}; fp.meta["USDEGP"] = (0.90, False)    # EGP closed -> not eligible
fp.pair_meta = lambda: fp.meta
b = bot.Bot(fp); msgs = []; orig = b.tg.admin; b.tg.admin = lambda t: (msgs.append(t), orig(t))
calls0 = None
def run(mins):
    global t
    for _ in range(mins):
        mock.advance_to(t.replace(second=C.SCAN_SECOND)); b.tick()
        if b.pending and not b.pending["prechecked"]: mock.advance_to(b.pending["precheck_at"]); b.run_precheck()
        t += timedelta(minutes=1)
t = start
b.command("/stream"); run(3); assert b.is_stream(); print("stream pairs:", b.d.pairs)
b.d.meta = {}   # stream adapter caches only stream pairs; candidates come from the poll adapter cache
used0 = fp.used
b.ladder_update("USDINR", False); b.ladder_update("USDINR", False)
print("requests used by swap:", fp.used - used0)
for m in msgs[-4:]: print("  ", m[:150])
assert "USDINR" in b.rotated_out and "USDINR" not in b.d.pairs and "USDARS" in b.d.pairs and len(b.d.pairs) == 5
assert fp.used - used0 == 1, "warm-up must be 1 request for the new pair only"
assert any(m.startswith("🔄 Stream swap: USDINR OUT (2 lost ladders) → USDARS IN (payout 87%)") for m in msgs)
run(5); assert "USDINR" not in C.PAIRS
print("ARS candles after swap:", len(b.d.books["USDARS"].bars), "| INR candles kept:", len(b.d.books["USDINR"].bars))
# second bench with ARS... bench MXN, next best = PHP (84%)
b.ladder_update("USDMXN", False); b.ladder_update("USDMXN", False); print("  ", [m for m in msgs if "Stream swap" in m][-1])
assert "USDPHP" in b.d.pairs
# no eligible replacement -> 4 pairs
for p in b.all_pairs:
    if p not in b.d.pairs: fp.meta[p] = (0.70, True)
b.d.meta_at = 0; b.d.pair_meta()   # payouts are timestamped now: the drop is seen on the next refresh
b.ladder_update("USDBRL", False); b.ladder_update("USDBRL", False)
sw = [m for m in msgs if "Stream swap" in m][-1]; print("  ", sw[:160]); assert "no eligible replacement" in sw and len(b.d.pairs) == 4
saved = json.load(open(C.BENCH_FILE)); print("persisted:", saved["date"], saved["pairs"]); assert set(saved["pairs"]) == {"USDINR", "USDMXN", "USDBRL"}
b2 = bot.Bot(fp); print("restart keeps bench:", sorted(b2.rotated_out)); assert b2.rotated_out == b.rotated_out
# poll mode: benched pair not signalled
b.command("/poll"); run(3); assert not b.is_stream()
sent_after = [m for m in msgs if m.startswith("🟢 Sent") and any(p in m.split()[2] for p in ("USDINR", "USDMXN", "USDBRL"))]
# midnight restore
mins_to_midnight = int((datetime(2026, 1, 6, 0, 1, tzinfo=UTC) - t).total_seconds() // 60)
b.command("/stream"); run(mins_to_midnight + 3)
print("after midnight: bench", sorted(b.rotated_out), "| stream", b.d.pairs)
for m in msgs:
    if m.startswith("🌅"): print("  ", m)
assert not b.rotated_out and b.d.pairs == C.STREAM_PAIRS[:5]
sent_bench = [m for m in msgs if m.startswith("🟢 Sent")]
print("max streams open:", sum(1 for f in mock.listeners if getattr(f, "__self__", None).__class__.__name__ == "StreamAdapter"))
print("\nALL BENCH / SWAP TESTS PASSED")
