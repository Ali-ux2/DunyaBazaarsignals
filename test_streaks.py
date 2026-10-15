"""Streak stats: computation, draws, windows (trading day 08:00 UTC+3 / 7d / 30d), per-stream separation, loss clustering, empty history."""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", TRADING_DAY_START="08:00")
for m in ("config", "streakstats"): sys.modules.pop(m, None)
import streakstats as SS
c = SS.compute
# computation
r = c(list("WWLLLWW")); assert (r["cur"], r["max_w"], r["max_l"], r["n"]) == ("W2", 2, 3, 7), r
assert c(list("WLL"))["cur"] == "L2"
# draws neither extend nor break
r = c(list("WWDW")); assert r["cur"] == "W3" and r["max_w"] == 3 and r["n"] == 3 and r["draws"] == 1, r
r = c(list("LDL")); assert r["cur"] == "L2" and r["max_l"] == 2, r
# clustering: L->L transitions
r = c(list("LLWLWL")); assert (r["ll_n"], r["ll_k"], r["losses"], r["n"]) == (3, 1, 4, 6), r
# empty
r = c([]); assert r["cur"] == "—" and r["n"] == 0 and r["ll_n"] == 0
now = datetime(2026, 10, 15, 7, 0, tzinfo=timezone.utc)   # 10:00 UTC+3 → trading day began 05:00 UTC
assert SS.trading_day_start(now) == datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)
assert SS.trading_day_start(datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)
assert "no closed trades yet" in SS.text(now)          # empty ledger file
# windows + separation, written through the ledger (deliberately out of order → sorted by close time)
def at(h): return now - timedelta(hours=h)
rec = [("main", "LOSS", at(20*24)), ("main", "LOSS", at(20*24-1)), ("main", "LOSS", at(20*24-2)),   # 30d only: L3
       ("main", "WIN", at(3*24)), ("main", "WIN", at(3*24-1)), ("main", "WIN", at(3*24-2)), ("main", "WIN", at(3*24-3)),  # 7d: W4
       ("main", "LOSS", at(1)), ("main", "TIE", at(0.5)), ("main", "WIN", at(1.5)), ("main", "LOSS", at(0.2)),   # today: W, L, D, L
       ("main", "WIN", at(40*24)),                                                       # older than 30d
       ("bug", "WIN", at(1)), ("shadow", "LOSS", at(2)), ("shadow", "LOSS", at(1))]
for s, res, t in reversed(rec): SS.record(s, res, t)
SS.record("bogus", "WIN", now); SS.record("main", "UNKNOWN", now)   # ignored
rows = SS.load(); m = SS.stream_stats(rows, "main", now)
assert m["today"]["cur"] == "L2" and m["today"]["max_w"] == 1 and m["today"]["max_l"] == 2 and m["today"]["draws"] == 1, m["today"]
assert m["7d"]["max_w"] == 5 and m["7d"]["max_l"] == 2, m["7d"]
assert m["30d"]["max_l"] == 3 and m["30d"]["max_w"] == 5 and m["30d"]["n"] == 10, m["30d"]
assert m["all"]["n"] == 11
assert (m["30d"]["ll_n"], m["30d"]["ll_k"]) == (4, 3), m["30d"]   # LLL→W: L-L,L-L,L-W ; today L→(draw)→L: L-L
b = SS.stream_stats(rows, "bug", now); sh = SS.stream_stats(rows, "shadow", now)
assert b["30d"]["n"] == 1 and b["all"]["cur"] == "W1" and sh["all"]["cur"] == "L2" and sh["30d"]["max_l"] == 2
t = SS.text(now); print(t)
assert "Main (public+VIP): now L2" in t and "Bug group: now W1" in t and "Shadow: now L2" in t and "loss after loss" in t
SS.prune(now); assert len(SS.load()) == len(rows) - 1
print("test_streaks OK")
