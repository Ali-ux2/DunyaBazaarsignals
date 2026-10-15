"""OTCharts /v1/stream data source: one SSE connection carrying up to STREAM_MAX_INSTRUMENTS symbols.
Builds M1 candles from ticks in memory, persists the last 300 per pair to STATE_DIR, caches payouts/resolutions.

Verified from https://otcharts.com/api.html (14 Oct 2026):
- Opening a stream counts as ONE request against the daily total; holding it costs nothing further.
- Lite: 1 stream at once, up to 5 instruments (Quotex/Pocket Option carry many per stream).
- SSE events: `connected` {"symbols":[...]}, `tick` {"price","symbol","time",...}, `dropped` {"dropped":[...]},
  `closed` {"closed":[...]}; a comment line every 20 s when quiet. 429 = daily requests exhausted OR all streams in use.
- Only price and time are on every tick. Quotex times are UTC; Pocket Option ('otc') is UTC+2.
NOT documented: whether an already-open stream survives once the daily quota is exhausted. Reopening after exhaustion
should be refused (it costs a request). So this client never closes a healthy stream on purpose."""
from __future__ import annotations
import json, logging, math, os, threading, time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import numpy as np, pandas as pd, requests
import config as C
log = logging.getLogger("bot")
UTC = timezone.utc

# ---------------- SSE parsing ----------------
def parse_sse(lines):
    """Yield (event, data_dict) from an iterable of text lines (comments/keep-alives skipped)."""
    ev, data = None, []
    for raw in lines:
        line = raw.decode() if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line == "":
            if data:
                try: yield (ev or "message"), json.loads("\n".join(data))
                except json.JSONDecodeError: log.warning("bad SSE data: %s", data[:1])
            ev, data = None, []; continue
        if line.startswith(":"): continue           # keep-alive comment (every 20 s when quiet)
        k, _, v = line.partition(":"); v = v[1:] if v.startswith(" ") else v
        if k == "event": ev = v
        elif k == "data": data.append(v)
    if data:
        try: yield (ev or "message"), json.loads("\n".join(data))
        except json.JSONDecodeError: pass

# ---------------- candles from ticks ----------------
class CandleBook:
    def __init__(self): self.bars = {}; self.last_tick = None; self.ticks = deque(maxlen=4000)   # raw ticks for 5 s bars (B1)
    def on_tick(self, t: float, price: float):
        self.ticks.append((t, price))
        m = int(t // 60) * 60; b = self.bars.get(m)
        if b is None: self.bars[m] = [price, price, price, price]
        else: b[1] = max(b[1], price); b[2] = min(b[2], price); b[3] = price
        self.last_tick = t
    def load(self, df: pd.DataFrame, overwrite=False):
        """Merge candles, de-duplicated by minute. overwrite=True lets authoritative REST bars replace tick-built ones."""
        n = 0
        for ts, r in df.iterrows():
            m = int(ts.timestamp())
            if overwrite or m not in self.bars: self.bars[m] = [r.open, r.high, r.low, r.close]; n += 1
        return n
    def df(self, n=1500):
        if not self.bars: return None
        ks = sorted(self.bars)[-n:]
        idx = pd.to_datetime(ks, unit="s", utc=True)
        return pd.DataFrame([self.bars[k] for k in ks], index=idx, columns=["open", "high", "low", "close"])
    def trim(self, keep=2000):
        if len(self.bars) > keep:
            for k in sorted(self.bars)[:-keep]: del self.bars[k]

# ---------------- live SSE client ----------------
_STREAM_LOCK = threading.RLock()     # process-wide: at most ONE /v1/stream connection may exist
_ACTIVE = {"stream": None}           # the SSEStream that currently owns the slot

def classify_429(body: str) -> str:
    """'in_use' (plan's stream slot taken) or 'quota' (daily requests exhausted), from the JSON error body."""
    b = (body or "").lower()
    if "stream" in b and ("at once" in b or "open" in b or "in use" in b or "close a stream" in b): return "in_use"
    if "request" in b and ("exhaust" in b or "quota" in b or "daily" in b or "used" in b): return "quota"
    return "in_use" if "stream" in b else "quota"

def _http_connect(venue, symbols):
    """Opens the SSE connection on its own Session so close() releases the socket for certain."""
    s = requests.Session()
    try:
        r = s.get(f"{C.OTCHARTS_BASE}/v1/stream", stream=True, timeout=(10, 75),
                  params={"venue": venue, "symbol": ",".join(symbols)},
                  headers={"Authorization": f"Bearer {C.OTCHARTS_API_KEY}", "User-Agent": C.USER_AGENT,
                           "Accept": "text/event-stream"})
    except Exception:
        s.close(); raise
    r._otc_session = s
    return r

def _close_resp(r):
    if r is None: return
    for f in (lambda: r.close(), lambda: r.raw.close() if getattr(r, "raw", None) is not None else None,
              lambda: r._otc_session.close() if getattr(r, "_otc_session", None) else None):
        try: f()
        except Exception: pass

class SSEStream(threading.Thread):
    """Exactly one held connection per process. Auto-reconnect with backoff; 429 'stream in use' backs off
    60/120/300/600s and alerts once; a tick watchdog force-reconnects after STREAM_STALE_SEC without ticks.
    on_event(event, data); extra events: 'down' {kind, body}, 'up' {}."""
    connect = staticmethod(_http_connect)   # tests inject a fake connector
    def __init__(self, venue, symbols, on_event, stop: threading.Event, usage=None):
        super().__init__(daemon=True); self.venue, self.symbols, self.on_event, self.stop = venue, symbols, on_event, stop
        self.connected = False; self.reopen = threading.Event(); self.connected_once = False; self.on_reconnect = None
        self.usage = usage            # callable -> /v1/usage json (free); used to wait until the server frees the slot
        self.resp = None; self.last_tick = None; self.down_kind = None; self.attempts = 0
        self._reopen_timer = None

    # -- control --
    def force_reconnect(self, why=""):
        """Close the current connection now (breaks the blocking read); run() reconnects."""
        log.warning("stream force-reconnect: %s", why); self.reopen.set()
        with _STREAM_LOCK: _close_resp(self.resp)
    def schedule_reopen(self, sec):
        if self._reopen_timer and self._reopen_timer.is_alive(): return     # never stack timers
        self._reopen_timer = threading.Timer(sec, lambda: self.force_reconnect("reopen to pick up dropped instruments"))
        self._reopen_timer.daemon = True; self._reopen_timer.start()
    def close(self, join=True):
        self.stop.set()
        if self._reopen_timer: self._reopen_timer.cancel()
        with _STREAM_LOCK:
            _close_resp(self.resp); self.resp = None
            if _ACTIVE["stream"] is self: _ACTIVE["stream"] = None
        if join and self.is_alive() and threading.current_thread() is not self: self.join(timeout=5)
        self.connected = False; log.info("stream connection closed")

    def _slot_busy(self):
        """True if /v1/usage (free) says every stream slot is held. None if unknown."""
        if not self.usage: return None
        try:
            st = (self.usage() or {}).get("streams") or {}
            if "open" in st and "limit" in st: return int(st["open"]) >= int(st["limit"] or 1)
        except Exception as e: log.info("usage check before reconnect failed: %s", e)
        return None

    def _wait(self, sec):
        return self.stop.wait(sec)

    def run(self):
        backoff = 2; i429 = 0; checked = False
        with _STREAM_LOCK:   # take over the slot: close any other stream object in this process first
            prev = _ACTIVE["stream"]
            if prev is not None and prev is not self: prev.close(join=False)
            _ACTIVE["stream"] = self
        while not self.stop.is_set():
            if _ACTIVE["stream"] is not self: log.warning("another stream owns the slot; this one exits"); break
            if self.down_kind == "in_use" and not checked and self._slot_busy():   # server still holds a stream: wait, free check
                checked = True
                wait = C.STREAM_429_BACKOFFS[min(i429, len(C.STREAM_429_BACKOFFS) - 1)]; i429 += 1
                log.warning("stream slot still open per /v1/usage (streams.open >= limit); retry in %ss", wait)
                if self._wait(wait): break
                continue
            checked = False
            r = None
            try:
                with _STREAM_LOCK:
                    _close_resp(self.resp); self.resp = None       # never two connections
                    if self.stop.is_set(): break
                    self.attempts += 1
                    r = self.connect(self.venue, self.symbols); self.resp = r
                if r.status_code == 429:
                    body = (r.text or "")[:300]; kind = classify_429(body)
                    with _STREAM_LOCK: _close_resp(r); self.resp = None
                    wait = C.STREAM_429_BACKOFFS[min(i429, len(C.STREAM_429_BACKOFFS) - 1)]; i429 += 1
                    if self.down_kind != kind:
                        self.down_kind = kind; self.on_event("down", {"kind": kind, "body": body})
                    log.warning("stream 429 (%s): %s; retry in %ss", "stream still open" if kind == "in_use" else "daily quota exhausted", body[:120], wait)
                    if self._wait(wait): break
                    continue
                if r.status_code != 200:
                    self.on_event("error", {"status": r.status_code, "body": (r.text or "")[:200]}); raise RuntimeError(f"HTTP {r.status_code}")
                if r.headers.get("x-otc-dropped"): log.warning("stream dropped header: %s", r.headers["x-otc-dropped"])
                backoff = 2; i429 = 0; self.reopen.clear()
                if self.down_kind is not None or (self.connected_once and not self.connected):
                    was = self.down_kind; self.down_kind = None
                    if was: self.on_event("up", {})
                if self.connected_once and self.on_reconnect:
                    try: self.on_reconnect()
                    except Exception as e: log.warning("reconnect warm-up failed: %s", e)
                self.connected = True; self.connected_once = True; self.last_tick = time.time()
                for ev, data in parse_sse(r.iter_lines(decode_unicode=True)):
                    if ev == "tick": self.last_tick = time.time()
                    self.on_event(ev, data)
                    if self.stop.is_set() or self.reopen.is_set(): break
                if not self.reopen.is_set() and not self.stop.is_set(): raise RuntimeError("server ended the stream")
            except Exception as e:
                if not self.stop.is_set() and not self.reopen.is_set(): log.warning("stream disconnected: %s", e)
                if not self.stop.is_set() and not self.reopen.is_set() and self.down_kind is None:
                    self.down_kind = "dropped"; self.on_event("down", {"kind": "dropped", "body": str(e)[:200]})
            finally:
                with _STREAM_LOCK: _close_resp(r); self.resp = None if self.resp is r else self.resp
                self.connected = False
            if self.stop.is_set(): break
            planned = self.reopen.is_set(); self.reopen.clear()
            wait = 3 if planned else backoff
            log.info("stream reconnect in %ss", wait)
            if self._wait(wait): break
            if not planned: backoff = min(backoff * 2, 60)
        with _STREAM_LOCK:
            _close_resp(self.resp); self.resp = None
            if _ACTIVE["stream"] is self: _ACTIVE["stream"] = None
        self.connected = False

# ---------------- mock tick source (tests / dry-run) ----------------
class MockTickSource:
    """Deterministic random-walk ticks every TICK_SEC seconds for each symbol; drives a simulated clock."""
    def __init__(self, symbols, start: datetime, seed=7, tick_sec=5, bases=None):
        self.symbols, self.t, self.tick_sec = symbols, start, tick_sec
        self.g = np.random.default_rng(seed); self.p = {s: (bases or {}).get(s, 10 + 7*i) for i, s in enumerate(symbols)}
        self.vol = {s: 0.00012 for s in symbols}; self.listeners = []
    def connect(self, on_event, symbols=None):
        self.listeners.append(on_event); on_event("connected", {"symbols": list(symbols or self.symbols)})
    def disconnect(self, on_event):
        if on_event in self.listeners: self.listeners.remove(on_event)
    def on_event(self, ev, data):
        for f in list(self.listeners): f(ev, data)
    def advance_to(self, target: datetime):
        while self.t + timedelta(seconds=self.tick_sec) <= target:
            self.t += timedelta(seconds=self.tick_sec)
            for s in self.symbols:
                self.vol[s] *= math.exp(self.g.normal(0, 0.01)); self.vol[s] = min(max(self.vol[s], 0.00005), 0.0004)
                self.p[s] *= math.exp(self.g.normal(0, 1) * self.vol[s] / math.sqrt(60 / self.tick_sec) * 1.0)
                self.on_event("tick", {"symbol": s, "price": round(self.p[s], 6), "time": self.t.timestamp()})
        self.t = max(self.t, target)
    def now(self): return self.t

# ---------------- adapter used by the bot ----------------
class StreamAdapter:
    default_mock = None   # tests: inject a MockTickSource for auto-mode switches
    def __init__(self, rest=None, mock: MockTickSource | None = None, stop: threading.Event | None = None):
        mock = mock or StreamAdapter.default_mock
        self.rest, self.mock = rest, mock; self._stop = stop or threading.Event()
        self.rest_allowed = rest is not None
        self.pairs = list(C.STREAM_PAIRS)[:C.STREAM_MAX_INSTRUMENTS]
        if len(C.STREAM_PAIRS) > C.STREAM_MAX_INSTRUMENTS:
            log.warning("STREAM_PAIRS has %d pairs; plan stream carries %d. Using %s", len(C.STREAM_PAIRS), C.STREAM_MAX_INSTRUMENTS, self.pairs)
        os.makedirs(C.CANDLE_DIR, exist_ok=True)
        self.books = {p: CandleBook() for p in self.pairs}
        self.closed, self.dropped = set(), set()
        self.res = self._resolutions()
        self.sym2pair = {self.res[p].symbol: p for p in self.pairs if self.res.get(p) and self.res[p].symbol}
        self._warm_marks = {p: 0 for p in self.pairs}; self._msgs = []
        self.stream = None; self.down = None; self.down_since = None; self._fb_state = None; self._fb_usage = (0.0, None)
        self._load_persisted(); self._rest_warmup()
        pc = self._load_json(C.PAYOUT_CACHE, {})
        if isinstance(pc, dict) and "payouts" in pc: self.payout_cache, self.payout_ts = pc.get("payouts", {}), pc.get("ts", {})
        else: self.payout_cache, self.payout_ts = dict(pc or {}), {}    # old flat cache: no timestamps → treated as stale
        self.meta_at = 0.0; self.meta = {}
        syms = [self.res[p].symbol for p in self.pairs if self.res.get(p) and self.res[p].symbol]
        if mock: mock.connect(self.on_event, syms)
        else:
            self.stream = SSEStream(C.BROKER_BOOK, syms, self.on_event, self._stop,
                                    usage=(self.rest.usage_raw if self.rest is not None and hasattr(self.rest, "usage_raw") else None))
            self.stream.on_reconnect = self._rest_warmup   # fill the gap left by the disconnect
            self.stream.start()
            self._wd = threading.Thread(target=self._watchdog, daemon=True); self._wd.start()
        log.info("Stream adapter: %d instruments on one stream: %s", len(syms), syms)

    # -- resolution (REST if possible, else cached on disk, else default ids) --
    def _resolutions(self):
        from resolver import Resolution
        cached = self._load_json(C.RESOLUTION_CACHE, {})
        out = {}
        for p in self.pairs:
            r = self.rest.res.get(p) if self.rest else None
            if r and r.symbol: out[p] = r; continue
            if p in cached:
                c = cached[p]; out[p] = Resolution(p, c["symbol"], c["book"], c["inverted"], c.get("variant"), ["cache"])
                log.warning("%s -> using cached resolution %s (catalogue unavailable)", p, c["symbol"])
            else:
                out[p] = Resolution(p, C.symbol_for(p), C.BROKER_BOOK, False, 0, ["default"])
                log.warning("%s -> catalogue unavailable and no cache; assuming %s", p, out[p].symbol)
        self._save_json(C.RESOLUTION_CACHE, {p: {"symbol": r.symbol, "book": r.book, "inverted": r.inverted, "variant": r.variant}
                                            for p, r in out.items() if r.symbol})
        return out

    # -- persistence --
    def _load_json(self, path, default):
        try:
            with open(path) as f: return json.load(f)
        except Exception: return default
    def _save_json(self, path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w") as f: json.dump(obj, f)
        os.replace(tmp, path)
    def _load_persisted(self):
        for p in self.pairs:
            f = os.path.join(C.CANDLE_DIR, f"{p}.csv")
            if os.path.exists(f):
                df = pd.read_csv(f, index_col=0, parse_dates=True); df.index = pd.to_datetime(df.index, utc=True)
                self.books[p].load(df); log.info("%s: loaded %d persisted candles (last %s)", p, len(df), df.index[-1])
    def persist(self):
        for p, b in self.books.items():
            df = b.df(C.PERSIST_CANDLES + 1)
            if df is not None: df = df[df.index + pd.Timedelta("60s") <= pd.Timestamp(self.now())].iloc[-C.PERSIST_CANDLES:]
            if df is not None and len(df): df.to_csv(os.path.join(C.CANDLE_DIR, f"{p}.csv"))
            b.trim()
    def _rest_warmup(self):
        """Instant warm-up on every stream open: /v1/candles limit=STREAM_WARMUP_BARS per pair, merged with saved/in-memory
        candles. Falls back to disk cache (already loaded), then ticks."""
        for p in self.pairs:   # free: candles the polling adapter already holds (auto-mode switch)
            cached = getattr(self.rest, "cache", {}).get(p) if self.rest else None
            if cached is not None and len(cached): self.books[p].load(cached)
        for p in self.pairs:
            got = None
            if self.rest is not None and hasattr(self.rest, "fetch_candles"):
                try: got = self.rest.fetch_candles(p, C.STREAM_WARMUP_BARS)
                except Exception as e: log.warning("%s: warm-up fetch failed (%s)", p, e)
            if got is not None and len(got):
                self.books[p].load(got, overwrite=True)
                msg = f"⚡ Instant warm-up: {p} {len(got)} candles loaded ({len(self.books[p].bars)} in memory)"
            elif len(self.books[p].bars):
                msg = f"💾 Warm-up {p}: REST unavailable, using {len(self.books[p].bars)} saved/in-memory candles"
            else:
                msg = f"⏳ Warm-up {p}: REST unavailable and no saved candles; building from ticks (need {C.MIN_BARS})"
            log.info(msg); self._msgs.append(msg)
            if len(self.books[p].bars) >= C.MIN_BARS: self._warm_marks[p] = C.MIN_BARS

    def set_pairs(self, new_pairs, reason=""):
        """Change the instrument list (max STREAM_MAX_INSTRUMENTS). OTCharts has no 'modify stream' call, so the SAME
        SSEStream object closes its connection and reopens with the new list (single-stream lock kept). Instant warm-up
        (1 /v1/candles request) only for ADDED pairs; candles of the others stay in memory. Returns (added, removed)."""
        from resolver import Resolution
        new_pairs = list(dict.fromkeys(new_pairs))[:C.STREAM_MAX_INSTRUMENTS]
        added = [p for p in new_pairs if p not in self.pairs]; removed = [p for p in self.pairs if p not in new_pairs]
        if not added and not removed: return [], []
        for p in added:
            r = (self.rest.res.get(p) if self.rest is not None and getattr(self.rest, "res", None) else None)
            if not (r and r.symbol): r = Resolution(p, C.symbol_for(p), C.BROKER_BOOK, False, 0, ["default"])
            self.res[p] = r
            self.books.setdefault(p, CandleBook()); self._warm_marks.setdefault(p, 0)
            got = None
            if self.rest is not None and hasattr(self.rest, "fetch_candles"):
                try: got = self.rest.fetch_candles(p, C.STREAM_WARMUP_BARS)
                except Exception as e: log.warning("%s: swap warm-up failed (%s)", p, e)
            if got is not None and len(got):
                self.books[p].load(got, overwrite=True); self._msgs.append(f"⚡ Instant warm-up: {p} {len(got)} candles loaded")
            else: self._msgs.append(f"⏳ Warm-up {p}: REST unavailable; building from ticks")
            if len(self.books[p].bars) >= C.MIN_BARS: self._warm_marks[p] = C.MIN_BARS
        self.pairs = new_pairs
        self.sym2pair = {self.res[p].symbol: p for p in self.pairs if self.res.get(p) and self.res[p].symbol}
        syms = [self.res[p].symbol for p in self.pairs if self.res.get(p) and self.res[p].symbol]
        self.dropped -= set(removed)
        if self.mock:
            for s in syms:
                if s not in self.mock.symbols:
                    self.mock.symbols.append(s); self.mock.p[s] = 10 + 7*len(self.mock.p); self.mock.vol[s] = 0.00012
            self.mock.disconnect(self.on_event); self.mock.connect(self.on_event, syms)
        elif self.stream is not None:
            self.stream.symbols = syms; self.stream.force_reconnect(f"instrument list changed {reason}".strip())
        log.info("stream instruments now %s (added %s, removed %s)", syms, added, removed)
        return added, removed

    def close(self):
        """Close the stream connection for certain (auto-mode switch back to polling, SIGTERM, exit)."""
        self._stop.set()
        if self.mock: self.mock.disconnect(self.on_event)
        if self.stream is not None: self.stream.close()
        log.info("stream closed")

    # -- tick watchdog: no tick on ANY pair for > STREAM_STALE_SEC => treat as dropped, close and reconnect --
    def _watchdog(self):
        while not self._stop.wait(C.STREAM_WATCHDOG_SEC):
            st = self.stream
            if st is None or not st.connected or st.last_tick is None: continue
            idle = time.time() - st.last_tick
            if idle > C.STREAM_STALE_SEC:
                if not getattr(self, "_silent", False):   # alert once per silent spell; reset by the next tick
                    self._silent = True; self._msgs.append(f"⚠️ Stream silent for {idle:.0f}s (no ticks): closing and reconnecting")
                st.last_tick = time.time(); st.force_reconnect(f"no tick for {idle:.0f}s")

    # -- REST fallback while the stream is down --
    def _fallback_budget_ok(self):
        if self.rest is None or not hasattr(self.rest, "candles") or not C.STREAM_FALLBACK_POLL: return False, "no REST"
        at, u = self._fb_usage
        if time.time() - at > 60 or u is None:
            try: u = self.rest.usage(); self._fb_usage = (time.time(), u)
            except Exception as e: return False, f"usage unavailable ({e})"
        used, quota = u.get("used", 0), u.get("quota") or 0
        if quota and used < quota - C.STREAM_FALLBACK_RESERVE: return True, f"{used}/{quota}"
        return False, f"budget exhausted ({used}/{quota})"
    def fallback_active(self):
        return self.down is not None

    # -- events --
    def on_event(self, ev, data):
        if ev == "tick":
            self._silent = False
            p = self.sym2pair.get(data.get("symbol"))
            if p not in self.books: return
            if p and data.get("price") is not None:
                t = float(data["time"]) - 3600 * C.BOOK_UTC_OFFSET_H.get(C.BROKER_BOOK, 0)
                tel = getattr(self, "telemetry", None)
                if tel is not None:
                    try:
                        rt = time.time(); lt = self.books[p].last_tick
                        if lt is not None and int(t // 60) > int(lt // 60): tel.on_bar_seen(p, (int(lt // 60)) * 60, t if self.mock else rt)  # closed bar complete
                        tel.on_tick(p, t, float(data["price"]), t if self.mock else rt)
                    except Exception: pass
                self.books[p].on_tick(t, float(data["price"]))
        elif ev == "connected": log.info("stream connected: %s", data.get("symbols"))
        elif ev == "dropped":
            self.dropped = {self.sym2pair.get(s, s) for s in data.get("dropped", [])}
            self._msgs.append(f"⚠️ Stream dropped {sorted(self.dropped)}: {data.get('reason', '')[:120]} (will reopen in {C.STREAM_REOPEN_DROPPED_MIN} min)")
            if not self.mock and self.stream is not None: self.stream.schedule_reopen(C.STREAM_REOPEN_DROPPED_MIN * 60)
        elif ev == "closed":
            self.closed = {self.sym2pair.get(s, s) for s in data.get("closed", [])}
            self._msgs.append(f"⛔ Broker closed {sorted(self.closed)} for trading")
        elif ev == "error": log.warning("stream error %s: %s", data.get("status"), data.get("body", "")[:200])
        elif ev == "down":
            first = self.down is None; self.down = data.get("kind"); self.down_since = self.down_since or time.time()
            if data.get("kind") == "in_use":
                self._msgs.append("🔴 Stream down: OTCharts says a stream is still open; retrying...")
            elif data.get("kind") == "quota":
                self._msgs.append(f"🔴 Stream down: OTCharts daily request quota exhausted; retrying until the reset at {C.reset_local()}...")
            elif first:
                self._msgs.append(f"🔴 Stream down: connection lost ({data.get('body', '')[:80]}); reconnecting...")
        elif ev == "up":
            mins = (time.time() - self.down_since)/60 if self.down_since else 0
            self.down = None; self.down_since = None; self._fb_state = None
            self._msgs.append(f"🟢 Stream reconnected (down {mins:.0f} min)")

    # -- bot interface --
    def now(self): return self.mock.now() if self.mock else datetime.now(UTC)
    def label(self, pair):
        r = self.res.get(pair); return r.symbol if r and r.symbol else C.symbol_for(pair)
    def note(self, pair, direction):
        r = self.res.get(pair)
        return f" (= {pair} {'PUT' if direction == 'CALL' else 'CALL'})" if r and r.inverted else ""
    def candles(self, pair, include_forming=False):
        if self.down is not None and self.rest is not None and pair in self.books:
            ok, why = self._fallback_budget_ok()
            if ok:
                if self._fb_state != "poll":
                    self._fb_state = "poll"
                    self._msgs.append(f"🔁 Stream down: polling the {len(self.pairs)} stream pairs via REST so signals continue (budget {why}).")
                try:
                    df, gaps, op = self.rest.candles(pair, include_forming=include_forming)
                    if df is not None and len(df): self.books[pair].load(df, overwrite=True)
                    return df, gaps, op and pair not in self.closed
                except Exception as e: log.warning("%s fallback poll failed: %s", pair, e)
            elif self._fb_state != "paused":
                self._fb_state = "paused"
                self._msgs.append(f"⏸ Stream down and REST {why}: signals paused until the stream reconnects.")
        b = self.books.get(pair)
        if b is None: return None, 0, False
        df = b.df()
        if df is None: return None, 0, pair not in self.closed
        now = pd.Timestamp(self.now())
        if not include_forming: df = df[df.index + pd.Timedelta("60s") <= now]
        recent = df.index[-30:]
        gaps = int((pd.Series(recent).diff().dropna() > pd.Timedelta("60s")).sum()) if len(recent) > 1 else 0
        if b.last_tick and now.timestamp() - b.last_tick > C.STREAM_STALE_SEC: gaps = max(gaps, 1)
        return df, gaps, pair not in self.closed and pair not in self.dropped
    def pair_meta(self):
        # payouts refresh every PAYOUT_REFRESH_SEC; while REST candles are paused for budget, still every PAYOUT_REFRESH_PAUSED_SEC
        # (one /v1/symbols request) so the 77% floor never runs on stale data
        every = C.PAYOUT_REFRESH_SEC if self.rest_allowed else C.PAYOUT_REFRESH_PAUSED_SEC
        if self.rest is not None and time.time() - self.meta_at > every:
            try:
                self.rest.meta_at = 0; m = self.rest.pair_meta(); self.meta_at = time.time()
                for p, (pay, op) in m.items():
                    if pay is not None: self.payout_cache[p] = pay; self.payout_ts[p] = self.meta_at
                    else: self.payout_cache.pop(p, None); self.payout_ts.pop(p, None)   # unknown now → never keep an old value
                self._save_json(C.PAYOUT_CACHE, {"payouts": self.payout_cache, "ts": self.payout_ts})
            except Exception as e: log.warning("payout refresh failed (%s); cached payouts expire after %ss", e, C.PAYOUT_MAX_AGE_SEC); self.meta_at = time.time()
        out = {}
        for p in self.pairs:
            pay, _ = self.payout_info(p)
            if pay is None and p not in getattr(self, "_fb_logged", set()):
                self._fb_logged = getattr(self, "_fb_logged", set()) | {p}
                self._msgs.append(f"⛔ {p}: payout unknown or older than {C.PAYOUT_MAX_AGE_SEC // 60} min → BLOCKED until a fresh payout arrives")
            elif pay is not None: getattr(self, "_fb_logged", set()).discard(p)
            out[p] = (pay, p not in self.closed)
        return out
    def payout_info(self, p):
        """(payout|None, age_seconds|None). Unknown or stale → None (fail closed). Mock source (tests) uses the fixed fallback."""
        if self.rest is None and self.mock is not None: return C.STREAM_FALLBACK_PAYOUT, 0.0
        pay, ts = self.payout_cache.get(p), self.payout_ts.get(p)
        if pay is None or ts is None: return None, None
        age = time.time() - ts
        return (pay if age <= C.PAYOUT_MAX_AGE_SEC else None), age
    def usage(self):
        if self.rest:
            try: return self.rest.usage()
            except Exception as e: log.warning("usage unavailable: %s", e)
        return {"used": 0, "quota": 0}
    def admin_messages(self):
        for p, b in self.books.items():
            n = len(b.bars); mark = (min(n, C.MIN_BARS) // 10) * 10
            if n < C.MIN_BARS and mark > self._warm_marks[p]:
                self._warm_marks[p] = mark; self._msgs.append(f"⏳ Warm-up {p}: {n}/{C.MIN_BARS} candles")
            elif n >= C.MIN_BARS and self._warm_marks[p] < C.MIN_BARS:
                self._warm_marks[p] = C.MIN_BARS; self._msgs.append(f"✅ Warm-up done {p}: {n} candles; signals enabled")
        out, self._msgs = self._msgs, []; return out
