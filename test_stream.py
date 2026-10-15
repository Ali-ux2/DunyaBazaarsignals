"""Unit checks for stream mode (no network)."""
import os, tempfile
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp()); os.environ["DATA_SOURCE"] = "mockstream"; os.environ["TELEGRAM_ENABLED"] = "0"
import config as C, stream as S, pandas as pd
from datetime import datetime, timezone
# 1) SSE parser with the documented event shapes
sample = """event: connected
data: {"symbols":["USDINR_otc","USDMXN_otc"]}

: keep-alive comment

event: tick
data: {"price":88.12345,"symbol":"USDINR_otc","time":1788119396}

event: dropped
data: {"dropped":["USDPKR_otc"],"carried":49,"reason":"not in the otc catalogue when this stream opened"}

event: closed
data: {"closed":["EURUSD_otc"],"reason":"closed for trading by the broker right now"}

""".splitlines()
evs = list(S.parse_sse(sample)); print("SSE events:", [e for e, _ in evs]); assert [e for e, _ in evs] == ["connected", "tick", "dropped", "closed"]
# 2) candle building from ticks
b = S.CandleBook(); t0 = 1788119340
for k, p in enumerate([1.0, 1.2, 0.9, 1.1]): b.on_tick(t0 + k*10, p)
b.on_tick(t0 + 61, 1.3); df = b.df(); print("candles:", df.round(2).values.tolist(), list(df.index.strftime("%H:%M:%S")))
assert df.iloc[0].tolist() == [1.0, 1.2, 0.9, 1.1] and len(df) == 2
# 3) closed-candle vs forming
m = S.MockTickSource(["USDINR_otc"], datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc), tick_sec=5)
C.STREAM_PAIRS = ["USDINR"]; a = S.StreamAdapter(rest=None, mock=m)
m.advance_to(datetime(2026, 1, 5, 6, 3, 57, tzinfo=timezone.utc))
closed, _, _ = a.candles("USDINR"); forming, _, _ = a.candles("USDINR", include_forming=True)
print("closed last:", closed.index[-1].strftime("%H:%M"), "| forming last:", forming.index[-1].strftime("%H:%M"), "(live at :57)")
# 4) payout fallback + cache
print("meta:", a.pair_meta(), a.admin_messages())
# 5) budget at 95% in stream mode stops REST only
import bot
class FakeRest:
    res = {}; 
    def usage(self): return {"used": 6700, "quota": 7000}
    def pair_meta(self): return {"USDINR": (0.92, True)}
a.rest = FakeRest(); a.rest_allowed = True
bt = bot.Bot(a); bt.check_budget(); print("rest_allowed after 95%:", a.rest_allowed, "| budget_pause:", bt.budget_pause)

# 6) instant warm-up on stream open: REST ok -> 50 bars; REST 429 -> disk cache; nothing -> ticks
import analyzer as A
class OkRest:
    res = {}; cache = {}
    def fetch_candles(self, p, limit):
        d = A.synthetic(n=limit, seed=2, t0="2026-01-05 05:13"); return d
class Rest429(OkRest):
    def fetch_candles(self, p, limit): raise RuntimeError("/v1/candles 429: daily requests exhausted")
C.STREAM_PAIRS = ["USDINR"]
for name, rest in (("REST ok", OkRest()), ("REST 429 with disk cache", Rest429()), ("REST 429, no cache", Rest429())):
    if name.endswith("no cache"):
        import shutil; shutil.rmtree(C.CANDLE_DIR, ignore_errors=True)
    m = S.MockTickSource(["USDINR_otc"], datetime(2026, 1, 5, 6, 3, tzinfo=timezone.utc))
    a = S.StreamAdapter(rest=rest, mock=m)
    m.advance_to(datetime(2026, 1, 5, 6, 4, 2, tzinfo=timezone.utc))
    df, g, _ = a.candles("USDINR")
    print(f"[{name}]", a.admin_messages(), "| closed bars:", 0 if df is None else len(df))
    if name == "REST ok":
        a.persist(); s = A.analyze(df, "USDINR", 0.85); print("   analyze right after open:", s["signal"] or s["reject"])
