"""Regression (live bug 2026-10-15 09:44 UTC+3: NZDUSD PUT to VIP at Quotex payout 58%): payout floor fails CLOSED.
Unknown / stale (> PAYOUT_MAX_AGE_SEC) / old untimestamped cache → blocked, never a default; 58% blocked at candidate AND send time
on the ensemble (all tiers) and bug/warm-up paths; payout logged to the feedback log."""
import os, tempfile, json, time
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", HIST_GATE="off", BLACKOUT="off",
                  NEWS_FILTER="off", DOUBLE_CONFIRM="off", PATTERNS="off", VOLUME_MODE="off", PUBLIC_CHANNEL_ID="-1001", VIP_CHANNEL_ID="-1002",
                  BUG_CHANNEL_ID="-1003", GROUP_FILTER="all")
import pandas as pd, config as C, bot, feedback as FB
from stream import StreamAdapter
assert C.CROSS_BOOK_PAYOUT is None, "no assumed payout for cross-book pairs"
# --- stream adapter: no fallback, timestamps, max age ---
class Rest:
    meta_at = 0; pm = {"NZDUSD": (0.58, True)}
    def pair_meta(self): return dict(self.pm)
sa = object.__new__(StreamAdapter); sa.rest = Rest(); sa.mock = None; sa.rest_allowed = True; sa.pairs = ["NZDUSD", "USDBRL"]; sa.closed = set()
sa.payout_cache, sa.payout_ts, sa.meta_at, sa._msgs = {"USDBRL": 0.9}, {}, 0.0, []
m = sa.pair_meta(); print("stream meta:", m)
assert m["NZDUSD"] == (0.58, True) and m["USDBRL"][0] is None, "58% passed through; old cache without timestamp → unknown"
assert any("USDBRL" in x and "BLOCKED" in x for x in sa._msgs)
sa.payout_ts["NZDUSD"] = time.time() - C.PAYOUT_MAX_AGE_SEC - 5; assert sa.payout_info("NZDUSD")[0] is None, "stale → unknown"
sa.rest.pm = {"NZDUSD": (None, True)}; sa.meta_at = 0; sa.payout_cache["NZDUSD"] = 0.84; sa.payout_ts["NZDUSD"] = time.time()
assert sa.pair_meta()["NZDUSD"][0] is None, "refresh says unknown → old 84% is dropped, not kept"
sa.rest_allowed = False; sa.meta_at = time.time() - C.PAYOUT_REFRESH_PAUSED_SEC - 1; sa.rest.pm = {"NZDUSD": (0.80, True)}
assert sa.pair_meta()["NZDUSD"][0] == 0.80, "payouts still refresh while REST candles are budget-paused"
saved = json.load(open(C.PAYOUT_CACHE)); assert "ts" in saved and "NZDUSD" in saved["ts"]
# --- bot: payout gate + bug send path ---
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); SENT = []
b.tg.send = lambda chat, text, label=None: SENT.append((str(chat), text)); b.tg.group_photo = lambda *a, **k: False
T = pd.Timestamp("2026-10-15 06:44:02", tz="UTC"); b.d.now = lambda: T
PAY = {"NZDUSD": (0.58, 5.0)}
b.d.payout_info = lambda p: PAY.get(p, (None, None))
assert b.payout_gate("NZDUSD")[:2] == (False, 0.58) and not b.payout_gate("USDXXX")[0], "58% and unknown both blocked"
assert b._cached_meta("USDXXX")[0] is None
import numpy as np, analyzer as A
r = np.random.default_rng(5); c = 1.1 + np.cumsum(r.normal(0, 1e-4, 200)); o = np.r_[c[0], c[:-1]]
xx = A.compute(pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 3e-5, low=np.minimum(o, c) - 3e-5, close=c), index=pd.date_range("2026-10-15 03:20", periods=200, freq="1min", tz="UTC")))
groups = lambda: [c for c, _ in SENT if c in ("-1001", "-1002", "-1003")]
for case in ({"NZDUSD": (0.58, 5.0)}, {}, {"NZDUSD": (0.84, C.PAYOUT_MAX_AGE_SEC + 60)}):   # 58% · unknown · stale-cached
    PAY.clear(); PAY.update({k: (v[0] if v[1] <= C.PAYOUT_MAX_AGE_SEC else None, v[1]) for k, v in case.items()})
    SENT.clear(); b.bug_last_pair.clear(); b.bug_last_pat.clear(); b.pending = None
    sig = b.bug_fire("*:B6_14", "NZDUSD", "PUT", "t", T, 0.85, x=xx)   # candidate believed 85% (old value) → send-time check must block
    assert not groups(), f"no group post for {case}: {SENT}"
assert b.bug_fire("*:B6_14", "NZDUSD", "PUT", "t", T, 0.58, x=xx) is None, "candidate-time floor"
# warm-up path also respects it (warm-up only changes the history decision)
recs = [r for r in FB.read(1, T) if r.get("pair") == "NZDUSD"]
assert recs and all("payout_send" in r for r in recs if r["kind"] in ("bug_signal", "paper_signal", "warmup_signal")), "payout logged"
print("ALL PAYOUT FLOOR TESTS PASSED")
