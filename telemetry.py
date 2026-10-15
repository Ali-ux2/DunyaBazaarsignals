"""Admin-only telemetry for bug research. Pure in-memory counters + a size-capped daily CSV; never makes requests.
(1) delivery: tick inter-arrival, ticks per second-of-minute, pauses >2 s, duplicate prices, jump size vs median
(2) lag: event-timestamp→receive (clock-skew sensitive), candle close→closed bar seen, :57 precheck read time
(3) over-extension: per closed candle z=(close-EMA20)/ATR, body/ATR, run length → next-candle reversal, binned per pair."""
from __future__ import annotations
import csv, math, os, statistics, time
from collections import deque, defaultdict
import config as C

Z_BINS = [(-99, -2.5), (-2.5, -1.5), (-1.5, 1.5), (1.5, 2.5), (2.5, 99)]
B_BINS = [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 99)]
def _bin(v, bins):
    for lo, hi in bins:
        if lo <= v < hi: return f"{lo:g}..{hi:g}".replace("-99..", "<").replace("..99", "+")
    return None
def _pct(xs, q):
    if not xs: return None
    s = sorted(xs); return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]
def _binom_p(k, n):
    return sum(math.comb(n, j) for j in range(k, n + 1)) / 2 ** n

class Telemetry:
    def __init__(self, state_dir=None, max_mb=None):
        self.dir = state_dir or C.STATE_DIR; self.max_bytes = int((max_mb or C.TELEMETRY_MAX_MB) * 1e6)
        self.reset_day(None)
    def reset_day(self, day):
        self.day = day
        self.last = {}                                   # pair -> (event_t, recv_t, price)
        self.iat = defaultdict(lambda: deque(maxlen=2000))
        self.sec = defaultdict(lambda: [0] * 60)         # ticks per second-of-minute
        self.n = defaultdict(int); self.pauses = defaultdict(int); self.dups = defaultdict(int); self.bigjump = defaultdict(int)
        self.moves = defaultdict(lambda: deque(maxlen=300))
        self.lag_ev = defaultdict(lambda: deque(maxlen=2000)); self.lag_bar = defaultdict(lambda: deque(maxlen=500))
        self.lag_pc = defaultdict(lambda: deque(maxlen=200)); self.bar_seen = {}
        self.ox = defaultdict(lambda: defaultdict(lambda: [0, 0]))   # pair -> "z:bin"/"b:bin" -> [reversals, n]
        self.ox_last = {}; self.csv_bytes = 0; self.csv_off = False
    # ---------- CSV (daily file, size cap; old days pruned to 3) ----------
    def _csv(self, kind, row, day):
        if self.csv_off or not C.TELEMETRY_CSV: return
        path = os.path.join(self.dir, f"telemetry_{day}.csv")
        try:
            new = not os.path.exists(path)
            if not new and self.csv_bytes == 0: self.csv_bytes = os.path.getsize(path)
            if self.csv_bytes > self.max_bytes: self.csv_off = True; return
            with open(path, "a", newline="") as f:
                line = ",".join(str(v) for v in [kind] + list(row)) + "\n"; f.write(line); self.csv_bytes += len(line)
            if new:
                old = sorted(p for p in os.listdir(self.dir) if p.startswith("telemetry_") and p.endswith(".csv"))
                for p in old[:-3]: os.remove(os.path.join(self.dir, p))
        except Exception: self.csv_off = True
    def _roll(self, ts):
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        if day != self.day: self.reset_day(day)
        return day
    # ---------- (1)+(2a) ticks ----------
    def on_tick(self, pair, ev_t, price, recv_t=None):
        recv_t = recv_t if recv_t is not None else time.time(); day = self._roll(ev_t)
        self.n[pair] += 1; self.sec[pair][int(ev_t) % 60] += 1
        lag = recv_t - ev_t; self.lag_ev[pair].append(lag)
        prev = self.last.get(pair); dt = mv = None; flag = ""
        if prev:
            dt = ev_t - prev[0]; self.iat[pair].append(dt)
            if dt > 2: self.pauses[pair] += 1; flag += "P"
            mv = abs(price - prev[2])
            if mv == 0: self.dups[pair] += 1; flag += "D"
            else:
                m = self.moves[pair]
                if len(m) >= 30:
                    med = statistics.median(m)
                    if med > 0 and mv >= 5 * med: self.bigjump[pair] += 1; flag += "J"
                m.append(mv)
        self.last[pair] = (ev_t, recv_t, price)
        if self.n[pair] % C.TELEMETRY_TICK_SAMPLE == 0 or flag:   # sample plain ticks; always log anomalies
            self._csv("tick", [pair, f"{ev_t:.3f}", f"{recv_t:.3f}", price, "" if dt is None else f"{dt:.3f}", f"{lag:.3f}", flag], day)
    # ---------- (2b) closed bar arrival, (2c) precheck read ----------
    def on_bar_seen(self, pair, bar_start_ts, recv_t):
        if self.bar_seen.get(pair, 0) >= bar_start_ts: return
        self.bar_seen[pair] = bar_start_ts; lag = recv_t - (bar_start_ts + 60)
        self.lag_bar[pair].append(lag); self._csv("bar", [pair, int(bar_start_ts), f"{recv_t:.3f}", f"{lag:.3f}"], self._roll(recv_t))
    def on_precheck(self, pair, entry_ts, read_t):
        d = read_t - entry_ts; self.lag_pc[pair].append(d)       # negative = seconds before entry
        self._csv("precheck", [pair, int(entry_ts), f"{read_t:.3f}", f"{d:.3f}"], self._roll(read_t))
    # ---------- (3) over-extension ----------
    @staticmethod
    def features(x, i):
        """Causal features of closed bar i (uses only bars ≤ i)."""
        c = x.close.iloc[: i + 1]; ema = c.ewm(span=20, adjust=False).mean().iloc[-1]; atr = float(x.atr.iloc[i]) or 1e-12
        o, cl = float(x.open.iloc[i]), float(x.close.iloc[i]); col = 1 if cl > o else (-1 if cl < o else 0); run = 0
        for j in range(i, max(-1, i - 20), -1):
            cj = 1 if x.close.iloc[j] > x.open.iloc[j] else (-1 if x.close.iloc[j] < x.open.iloc[j] else 0)
            if cj != col or col == 0: break
            run += 1
        z = (cl - ema) / atr
        return {"z": z, "body": abs(cl - o) / atr, "col": col, "run": run}
    def keys_for(self, f):
        """Bin keys; reversal = next candle opposite to the over-extension side (z sign; body bins use bar colour)."""
        out = []
        a = abs(f["z"]); zb = "2.5+" if a >= 2.5 else ("1.5..2.5" if a >= 1.5 else None)
        if zb and f["z"] != 0: out.append((f"z{zb}", -1 if f["z"] > 0 else 1))
        bb = _bin(f["body"], B_BINS)
        if bb and f["col"]: out.append((f"body{bb}", -f["col"]))
        if f["run"] >= 3 and f["col"]: out.append((f"run{min(f['run'], 6)}{'+' if f['run'] >= 6 else ''}", -f["col"]))
        return out
    def on_candles(self, pair, x):
        """Call with the closed-candle frame (atr column). Records each newly closed bar once: features of bar i-1, outcome bar i."""
        if x is None or len(x) < 30: return
        last = self.ox_last.get(pair); n = len(x)
        start = n - 1 if last is None else next((k for k in range(n - 1, 0, -1) if x.index[k - 1] <= last), n - 1)
        for i in range(max(start, 21), n):
            if last is not None and x.index[i] <= last: continue
            f = self.features(x, i - 1); col = 1 if x.close.iloc[i] > x.open.iloc[i] else (-1 if x.close.iloc[i] < x.open.iloc[i] else 0)
            for key, rev_dir in self.keys_for(f):
                if col == 0: continue
                cell = self.ox[pair][key]; cell[1] += 1; cell[0] += col == rev_dir
            self._csv("ox", [pair, x.index[i - 1].isoformat(), f"{f['z']:.2f}", f"{f['body']:.2f}", f["run"], col], self.day or "na")
        self.ox_last[pair] = x.index[-1]
    def ox_patterns(self, min_n=None, max_p=None):
        """Significant reversal bins (binomial vs 50%, Bonferroni over all tested cells) → [(pair, key, rate, n, p)]."""
        min_n = min_n or C.BUG_MIN_SAMPLES; max_p = max_p or C.BUG_MAX_P
        cells = [(p, k, r, n) for p, d in self.ox.items() for k, (r, n) in d.items() if n >= min_n]
        m = max(1, len(cells)); out = []
        for p, k, r, n in cells:
            rate = r / n; pv = _binom_p(r, n) * m
            if rate >= C.BUG_MIN_RATE and pv < max_p: out.append((p, k, rate, n, pv))
        return sorted(out, key=lambda t: -t[2])
    # ---------- summaries ----------
    def lag_tag(self, pair):
        b, e = self.lag_bar.get(pair), self.lag_ev.get(pair)
        parts = []
        if e: parts.append(f"tick lag {_pct(e, .5):.2f}/{_pct(e, .95):.2f}s")
        if b: parts.append(f"bar lag {_pct(b, .5):.1f}/{_pct(b, .95):.1f}s")
        return ("⏱ " + " · ".join(parts) + " (med/p95)") if parts else "⏱ lag n/a"
    def heat(self, pair):
        s = self.sec[pair]; mx = max(s) or 1; ch = " ▁▂▃▄▅▆▇█"
        return "".join(ch[int(8 * v / mx)] for v in s)
    def summary(self, short=False):
        rows = [f"📡 Telemetry {self.day or '-'} UTC (admin only; no extra requests; tick lag depends on clock sync)"]
        for p in sorted(set(self.n) | set(self.lag_bar)):
            ia = self.iat.get(p) or []; n = self.n.get(p, 0)
            rows.append(f"{p}: {n} ticks · gap med {(_pct(ia, .5) or 0):.2f}s p95 {(_pct(ia, .95) or 0):.2f}s · pauses>2s {self.pauses[p]} · dup {self.dups[p]} · jumps≥5×med {self.bigjump[p]} · {self.lag_tag(p)}"
                        + (f" · :57 read {_pct(self.lag_pc[p], .5):+.1f}s" if self.lag_pc.get(p) else ""))
            if n and not short: rows.append(f"  sec 0→59 |{self.heat(p)}|")
        cells = sorted(((p, k, r / n, n) for p, d in self.ox.items() for k, (r, n) in d.items() if n >= 15), key=lambda t: -t[2])[: (3 if short else 8)]
        if cells:
            rows.append("Over-extension → next-candle reversal (top bins):")
            rows += [f"  {p} {k}: {r:.0%} ({n})" for p, k, r, n in cells]
        sig = self.ox_patterns()
        rows.append(f"Significant (Bonferroni) bins fed to bug watcher: {', '.join(f'{p} {k} {r:.0%}' for p, k, r, _, _ in sig[:5]) or 'none'}")
        return "\n".join(rows)
