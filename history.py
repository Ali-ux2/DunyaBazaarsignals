"""Historical confirmation gate: before ANY signal is sent, replay the same detector over this pair's last 7 and 30 days of
1m candles, simulate next-candle expiry, and require: 30d n >= HIST_MIN_N and Wilson LB (7d weighted x2) >= breakeven + HIST_MARGIN,
and the 7d win rate not below breakeven when 7d n >= 5. Candles come from an incremental store in STATE_DIR/candles
(filled from streams/polls for free) plus a slow backfill under HIST_BACKFILL_MAX_REQ requests/day."""
from __future__ import annotations
import os, time, math, logging
import numpy as np, pandas as pd
import config as C
log = logging.getLogger("history")

def wilson_lb(w, n, z=1.64):
    if n <= 0: return 0.0
    p = w / n; d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d

def wilson_ub(w, n, z=1.64):
    if n <= 0: return 1.0
    p = w / n; d = 1 + z * z / n
    return (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d

class CandleStore:
    def __init__(self, d=None):
        self.dir = os.path.join(d or C.STATE_DIR, "candles"); os.makedirs(self.dir, exist_ok=True)
        self.df = {}; self.saved_at = {}; self.dirty = set()
    def _path(self, p): return os.path.join(self.dir, f"{p}.pkl")
    def get(self, p):
        if p not in self.df:
            try: self.df[p] = pd.read_pickle(self._path(p))
            except Exception: self.df[p] = None
        return self.df[p]
    def add(self, p, df):
        """Merge closed candles (dedup, keep HIST_KEEP_DAYS). Cheap when nothing is new."""
        if df is None or not len(df): return
        old = self.get(p)
        if old is not None and len(old) and df.index[-1] <= old.index[-1] and df.index[0] >= old.index[0]: return
        new = df[["open", "high", "low", "close"]] if old is None else pd.concat([old, df[["open", "high", "low", "close"]]])
        new = new[~new.index.duplicated(keep="last")].sort_index()
        new = new[new.index >= new.index[-1] - pd.Timedelta(days=C.HIST_KEEP_DAYS)]
        self.df[p] = new; self.dirty.add(p)
    def flush(self, force=False):
        for p in list(self.dirty):
            if force or time.time() - self.saved_at.get(p, 0) > C.HIST_SAVE_SEC:
                try: self.df[p].to_pickle(self._path(p) + ".tmp"); os.replace(self._path(p) + ".tmp", self._path(p))
                except Exception as e: log.warning("candle store save %s: %s", p, e)
                self.saved_at[p] = time.time(); self.dirty.discard(p)
    def coverage_days(self, p):
        d = self.get(p); return 0.0 if d is None or not len(d) else (d.index[-1] - d.index[0]).total_seconds() / 86400

class HistGate:
    def __init__(self, store=None):
        self.last = None; self.store = store or CandleStore(); self.occ = {}; self.x = {}; self.req_day = None; self.req_n = 0; self.bf_done = {}
    # ---------- backfill (bounded) ----------
    def backfill_step(self, pairs, fetch, today):
        """At most one /v1/candles call per invocation, one per pair per day, under HIST_BACKFILL_MAX_REQ/day."""
        if self.req_day != today: self.req_day, self.req_n = today, 0
        if self.req_n >= C.HIST_BACKFILL_MAX_REQ or fetch is None: return None
        for p in pairs:
            if self.bf_done.get(p) == today or self.store.coverage_days(p) >= C.HIST_KEEP_DAYS - 0.1: continue
            self.bf_done[p] = today; self.req_n += 1
            try: df = fetch(p, C.HIST_BACKFILL_LIMIT)
            except Exception as e: log.warning("backfill %s: %s", p, e); return p
            self.store.add(p, df); return p
        return None
    # ---------- occurrences ----------
    def _x(self, p, compute):
        d = self.store.get(p)
        if d is None or len(d) < 60: return None
        c = self.x.get(p)
        if c is None or c[0] != d.index[-1]: c = (d.index[-1], compute(d)); self.x[p] = c
        return c[1]
    def occurrences(self, p, name, detector, compute, upto, budget_s=None):
        """[(ts, win)] for every past trigger of `detector(x, i) -> 'CALL'|'PUT'|None` on bars < upto, outcome = next bar.
        Incremental: only bars not yet scanned are evaluated."""
        x = self._x(p, compute)
        if x is None: return []
        key = (p, name); st = self.occ.get(key) or {"last": None, "rows": []}
        idx = x.index; cut = idx.searchsorted(upto)                       # bars strictly before the signal bar's outcome
        start = 60 if st["last"] is None else idx.searchsorted(st["last"], side="right")
        start = max(start, idx.searchsorted(idx[-1] - pd.Timedelta(days=C.HIST_KEEP_DAYS)))
        o, c = x.open.values, x.close.values; t0 = time.monotonic(); st["done"] = True
        for i in range(start, max(start, cut - getattr(detector, 'horizon', 1))):
            if budget_s is not None and time.monotonic() - t0 > budget_s: st["done"] = False; break
            try: r = detector(x, i)
            except Exception: r = None
            hz = getattr(detector, "horizon", 1)          # expiry in bars (P13 = 2): entry next open, outcome close of bar i+hz
            if r in ("CALL", "PUT") and i + hz < len(c) and c[i + hz] != o[i + 1]:
                st["rows"].append((idx[i], (c[i + hz] > o[i + 1]) == (r == "CALL")))
            st["last"] = idx[i]
        lo = idx[-1] - pd.Timedelta(days=30); st["rows"] = [t for t in st["rows"] if t[0] >= lo]
        self.occ[key] = st
        return st["rows"]
    def warm(self, pairs, detectors, compute, now, budget_s):
        """Background-friendly: spend up to budget_s scanning un-scanned bars (called once per minute)."""
        t0 = time.monotonic()
        for p in pairs:
            for name, fn in detectors.items():
                left = budget_s - (time.monotonic() - t0)
                if left <= 0: return
                st = self.occ.get((p, name))
                if st and st.get("done") and st.get("last") is not None and self.store.get(p) is not None and st["last"] >= self.store.get(p).index[-3]: continue
                self.occurrences(p, name, fn, compute, now - pd.Timedelta(minutes=1), budget_s=left)
    def stats(self, rows, now):
        r30 = [w for t, w in rows if t >= now - pd.Timedelta(days=30)]; r7 = [w for t, w in rows if t >= now - pd.Timedelta(days=7)]
        return {"n30": len(r30), "w30": int(sum(r30)), "n7": len(r7), "w7": int(sum(r7))}
    def decide(self, s, payout, coverage):
        """Pass/fail + text. 7d counted twice (recency weight)."""
        be = 1 / (1 + payout); n = s["n30"] + C.HIST_W7 * s["n7"]; w = s["w30"] + C.HIST_W7 * s["w7"]
        lb = wilson_lb(w, n); wr30 = s["w30"] / s["n30"] if s["n30"] else 0; wr7 = s["w7"] / s["n7"] if s["n7"] else None
        line = f"seen {s['n30']}× in 30d, {s['w30']} won ({wr30:.0%})" + (f" · 7d {s['w7']}/{s['n7']}" if s["n7"] else "")
        if coverage < C.HIST_MIN_COVER_DAYS: return False, f"not enough history yet ({coverage:.1f} days stored, need {C.HIST_MIN_COVER_DAYS})", line
        if s["n30"] < C.HIST_MIN_N: return False, f"only {s['n30']} past occurrences in 30d (need {C.HIST_MIN_N})", line
        if lb < be + C.HIST_MARGIN: return False, f"Wilson LB {lb:.0%} < breakeven {be:.0%} + {C.HIST_MARGIN*100:.0f} pts", line
        if s["n7"] >= 5 and wr7 < be: return False, f"7d {wr7:.0%} below breakeven {be:.0%}", line
        return True, f"LB {lb:.0%} ≥ BE {be:.0%}+{C.HIST_MARGIN*100:.0f}", line
    def check(self, p, names_detectors, compute, now, payout):
        """names_detectors: [(name, fn)] — pooled (used for ensembles: the agreeing members). Returns (ok, why, card_line)."""
        rows = []; t0 = time.monotonic()
        for name, fn in names_detectors:
            left = max(0.05, C.HIST_CHECK_BUDGET_S - (time.monotonic() - t0))
            rows += self.occurrences(p, name, fn, compute, now - pd.Timedelta(minutes=1), budget_s=left)
            if not self.occ.get((p, name), {}).get("done", True):
                return False, "history still warming up (scan in progress)", ""
        self.last = self.stats(rows, now)
        return self.decide(self.last, payout, self.store.coverage_days(p))
    @staticmethod
    def fill_ok(s, payout):
        """Tier-2 fill: allowed with no/thin history or history WR >= breakeven; never when clearly negative (Wilson UB < BE)."""
        be = 1 / (1 + payout)
        if not s or s.get("n30", 0) == 0: return True, "no history"
        if wilson_ub(s["w30"], s["n30"]) < be: return False, "history clearly negative (Wilson UB < breakeven)"
        if s["n30"] >= C.HIST_MIN_N and s["w30"] / s["n30"] < be: return False, "history below breakeven"
        return True, f"history {s['w30']}/{s['n30']}"
