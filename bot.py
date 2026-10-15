"""M1 OTC signal bot (NO martingale). Long-lived worker: `python bot.py`.
Data: OTCharts REST (/v1/candles polled once per minute after candle close) or CSV replay.
Telegram: plain Bot API over requests. Logs to stdout."""
from __future__ import annotations
import json, logging, os, signal, sys, threading, time
from datetime import datetime, timedelta, timezone
import numpy as np, pandas as pd, requests
import config as C
from analyzer import (ensemble, analyze_relaxed_ensemble, apply_level, strategies_line, bug_scan, bug_prediction,
                      STRATEGY_NAMES)
from analyzer import analyze, analyze_relaxed, compute, tracker, shadow_tracker, breakeven, precheck, precheck_flip_kind, switch_body_atr
from resolver import resolve, describe, Resolution
from chart import result_caption, render_result_chart, render_chart, signal_caption, display_name

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
UTC = timezone.utc
STOP = threading.Event()

# ======================= DATA ADAPTERS =======================
class OTChartsAdapter:
    """GET /v1/candles, /v1/symbols, /v1/usage with Bearer key + explicit User-Agent."""
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {C.OTCHARTS_API_KEY}", "User-Agent": C.USER_AGENT})
        self.cache: dict[str, pd.DataFrame] = {}
        self.offset = timedelta(hours=C.BOOK_UTC_OFFSET_H.get(C.BROKER_BOOK, 0))
        self.meta, self.meta_at = {}, 0.0
        self.requests = 0
        self.res: dict[str, Resolution] = {}; self.resolve_at = 0.0
        self.resolve_all()

    # ---- symbol resolution ----
    def catalogue(self, book):
        j = self._get("/v1/symbols", venue=book); return [s["symbol"] for s in j.get("symbols", [])], j

    def resolve_all(self, only_missing=False):
        try: cat, j = self.catalogue(C.BROKER_BOOK)
        except Exception as e: log.error("Cannot read %s catalogue: %s", C.BROKER_BOOK, e); cat, j = [], {}
        self._sym_rows = {s["symbol"]: s for s in j.get("symbols", [])}; self.meta_at = time.time()
        for p in C.PAIRS:
            if only_missing and self.res.get(p) and self.res[p].symbol and not self.res[p].cross_book: continue
            r = resolve(p, cat, C.BROKER_BOOK)
            if not r.symbol and C.ALLOW_CROSS_BOOK_FALLBACK:
                for fb in C.FALLBACK_BOOKS:
                    try: fcat, _ = self.catalogue(fb)
                    except Exception as e: log.warning("%s: fallback book %s unreadable (%s)", p, fb, e); continue
                    fr = resolve(p, fcat, fb)
                    if fr.symbol: fr.cross_book = True; fr.tried = r.tried; r = fr; break
            self.res[p] = r
            (log.warning if (not r.symbol or r.cross_book or r.inverted) else log.info)(describe(r))
        self.resolve_at = time.time()

    def maybe_reresolve(self):
        if any(not r.symbol or r.cross_book for r in self.res.values()) and time.time() - self.resolve_at > C.RESOLVE_RETRY_MIN*60:
            log.info("Retrying symbol resolution for unresolved pairs"); self.resolve_all(only_missing=True)

    def label(self, pair):
        r = self.res.get(pair); return r.symbol if r and r.symbol else C.symbol_for(pair)
    def note(self, pair, direction):
        r = self.res.get(pair)
        if r and r.inverted: return f" (= {pair} {'PUT' if direction == 'CALL' else 'CALL'})"
        return ""

    def _get(self, path, **params):
        for attempt in range(3):
            try:
                r = self.s.get(C.OTCHARTS_BASE + path, params=params, timeout=15)
                if r.status_code == 429: raise BudgetExhausted(r.text[:200])
                if r.status_code in (401, 403, 404, 400): raise RuntimeError(f"{path} {r.status_code}: {r.text[:200]}")
                r.raise_for_status(); return r.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                log.warning("OTCharts %s attempt %d failed: %s", path, attempt+1, e); time.sleep(2*(attempt+1))
        raise RuntimeError(f"OTCharts {path} unavailable")

    def now(self): return datetime.now(UTC)

    def candles(self, pair, include_forming=False):
        """Returns (closed-candle DataFrame indexed by true-UTC open time, gap runs in last window, open flag)."""
        r = self.res.get(pair)
        if not r or not r.symbol: return None, 0, False
        sym, book = r.symbol, r.book
        limit = C.POLL_LIMIT if pair in self.cache else C.INITIAL_LIMIT
        j = self._get("/v1/candles", venue=book, symbol=sym, tf=60, limit=limit)
        df = pd.DataFrame(j.get("candles", []))
        if df.empty: return self.cache.get(pair), 0, j.get("open", True)
        self.requests += 1
        off = timedelta(hours=C.BOOK_UTC_OFFSET_H.get(book, 0))                  # Pocket Option UTC+2 -> UTC
        df["time"] = pd.to_datetime(df.time, unit="s", utc=True) - off
        df = df.set_index("time")[["open", "high", "low", "close"]].astype(float)
        old = self.cache.get(pair)
        df = pd.concat([old, df]) if old is not None else df
        df = df[~df.index.duplicated(keep="last")].sort_index().iloc[-1500:]
        self.cache[pair] = df
        gaps = int((j.get("gaps") or {}).get("runs", 0)) if limit == C.POLL_LIMIT else 0
        # also detect gaps locally in the last 30 bars
        recent = df.index[-30:]
        if len(recent) > 1 and (pd.Series(recent).diff().dropna() > pd.Timedelta("60s")).any(): gaps = max(gaps, 1)
        return (df if include_forming else closed_only(df, self.now())), gaps, j.get("open", True)

    def pair_meta(self):
        """{pair: (payout_fraction|None, open_bool)} refreshed every PAYOUT_REFRESH_SEC."""
        self.maybe_reresolve()
        if time.time() - self.meta_at > C.PAYOUT_REFRESH_SEC or not self.meta:
            if time.time() - self.meta_at > C.PAYOUT_REFRESH_SEC:
                j = self._get("/v1/symbols", venue=C.BROKER_BOOK); self._sym_rows = {s["symbol"]: s for s in j.get("symbols", [])}
                self.meta_at = time.time()
            self.meta = {}
            for p in C.PAIRS:
                r = self.res.get(p)
                if not r or not r.symbol: self.meta[p] = (None, False); continue
                if r.cross_book:   # payout is Quotex's concept; a foreign book's payout doesn't apply
                    self.meta[p] = (C.CROSS_BOOK_PAYOUT, True); continue
                s = self._sym_rows.get(r.symbol)
                self.meta[p] = (None, False) if s is None else (
                    (s["payout"]/100.0) if s.get("payout") is not None else None, s.get("open") is not False)
        return self.meta

    def usage(self): return self._get("/v1/usage").get("requests", {})
    def usage_raw(self): return self._get("/v1/usage")   # free; includes "streams": {"open","limit"}

    def fetch_candles(self, pair, limit):
        """One /v1/candles call (stream warm-up). Returns closed candles in true UTC."""
        r = self.res.get(pair); sym, book = (r.symbol, r.book) if r and r.symbol else (C.symbol_for(pair), C.BROKER_BOOK)
        j = self._get("/v1/candles", venue=book, symbol=sym, tf=60, limit=limit)
        df = pd.DataFrame(j.get("candles", []))
        if df.empty: return None
        df["time"] = pd.to_datetime(df.time, unit="s", utc=True) - timedelta(hours=C.BOOK_UTC_OFFSET_H.get(book, 0))
        return closed_only(df.set_index("time")[["open", "high", "low", "close"]].astype(float), self.now())

class ReplayAdapter:
    """CSV with columns time,pair,open,high,low,close. Simulated clock; no sleeping."""
    def __init__(self, path):
        d = pd.read_csv(path, parse_dates=["time"]); d["time"] = pd.to_datetime(d.time, utc=True)
        self.data = {p: g.set_index("time")[["open", "high", "low", "close"]].sort_index() for p, g in d.groupby("pair")}
        first = min(g.index[0] for g in self.data.values())
        self.clock = first + timedelta(minutes=C.MIN_BARS + 40, seconds=C.SCAN_SECOND)
        self.calls = 0
    def now(self): return self.clock
    def advance(self): self.clock += timedelta(minutes=1)
    def candles(self, pair, include_forming=False):
        """include_forming: the bar containing `clock` is returned with its FINAL OHLC (approximation of the
        live snapshot; replay data has no intra-minute ticks)."""
        self.calls += 1; df = self.data.get(pair)
        if df is None: return None, 0, False
        d = df[df.index <= self.clock]
        return (d if include_forming else closed_only(d, self.clock)).iloc[-1500:], 0, True
    def pair_meta(self): return {p: (C.DEFAULT_PAYOUT, True) for p in C.PAIRS}
    def label(self, pair): return C.symbol_for(pair)
    def note(self, pair, direction): return ""
    def usage(self): return {"used": self.calls, "quota": 10**9}

class BudgetExhausted(Exception): pass

def closed_only(df, now):
    """Drop the still-forming candle: a bar is closed when open_time + 60s <= now."""
    if df is None or df.empty: return df
    return df[df.index + pd.Timedelta("60s") <= now]

# ======================= TELEGRAM =======================
class Telegram:
    def __init__(self):
        self.on = C.TELEGRAM_ENABLED; self.base = f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}"; self.offset = None
    def send(self, chat, text, label="group"):
        if not self.on or not chat: log.info("[TG->%s] %s", label, text.replace("\n", " | ")); return
        try:
            r = requests.post(f"{self.base}/sendMessage", json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                              "disable_web_page_preview": True}, timeout=10)
            if not r.ok: log.warning("Telegram send failed %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as e: log.warning("Telegram error: %s", e)
    chats = None   # [(name, chat_id)] current group target(s); set by Bot
    def _targets(self, chats):
        c = chats if chats is not None else self.chats
        return c if c else [("group", C.TELEGRAM_CHANNEL_ID)]
    def group(self, t, chats=None):
        for name, cid in self._targets(chats): self.send(cid, t, name)
    def group_photo(self, png: bytes, caption: str, name="result.png", chats=None):
        """sendPhoto (multipart) to each target; a failed photo falls back to text in THAT chat. Disabled: PNG to CHART_DIR."""
        for lab, cid in self._targets(chats):
            if not self.on or not cid:
                os.makedirs(C.CHART_DIR, exist_ok=True); path = os.path.join(C.CHART_DIR, name)
                with open(path, "wb") as f: f.write(png)
                log.info("[TG->%s photo] %s (%s, %d bytes)", lab, caption, path, len(png)); continue
            ok = False
            try:
                r = requests.post(f"{self.base}/sendPhoto", data={"chat_id": cid, "caption": caption, "parse_mode": "HTML"},
                                  files={"photo": (name, png, "image/png")}, timeout=20)
                ok = r.ok
                if not r.ok: log.warning("sendPhoto %s failed %s: %s", lab, r.status_code, r.text[:200])
            except requests.RequestException as e: log.warning("sendPhoto %s error: %s", lab, e)
            if not ok: self.send(cid, caption, lab)
        return True
    def admin(self, t): self.send(C.TELEGRAM_ADMIN_ID, t, "admin")
    def set_commands(self, commands):
        """Telegram Menu button: setMyCommands scoped to the admin chat only (BotCommandScopeChat)."""
        cmds = [{"command": c.lstrip("/"), "description": d[:256]} for _, c, d in commands]
        if not self.on: log.info("[TG setMyCommands admin-scope] %s", [c["command"] for c in cmds]); return True
        try:
            r = requests.post(f"{self.base}/setMyCommands", json={"commands": cmds,
                              "scope": {"type": "chat", "chat_id": C.TELEGRAM_ADMIN_ID}}, timeout=10)
            if not r.ok: log.warning("setMyCommands failed %s: %s", r.status_code, r.text[:200])
            return r.ok
        except requests.RequestException as e: log.warning("setMyCommands error: %s", e); return False
    def poll_commands(self, handler):
        """Long-poll getUpdates; only messages from TELEGRAM_ADMIN_ID are obeyed."""
        while self.on and not STOP.is_set():
            try:
                r = requests.get(f"{self.base}/getUpdates", params={"timeout": 25, "offset": self.offset}, timeout=35).json()
                for u in r.get("result", []):
                    self.offset = u["update_id"] + 1; m = u.get("message") or {}
                    if str(m.get("from", {}).get("id")) == str(C.TELEGRAM_ADMIN_ID) or str(m.get("chat", {}).get("id")) == str(C.TELEGRAM_ADMIN_ID):
                        handler((m.get("text") or "").strip().split("@")[0].lower())
            except Exception as e: log.warning("getUpdates: %s", e); STOP.wait(5)

# ======================= BOT =======================
def parse_sessions(spec):
    out = []
    for part in filter(None, (s.strip() for s in spec.split(","))):
        a, b = part.split("-"); out.append((int(a[:2])*60+int(a[3:]), int(b[:2])*60+int(b[3:])))
    return out

class Bot:
    def __init__(self, data):
        self.d, self.tg = data, Telegram()
        self.manual_pause = False; self.budget_pause = False; self.warned_budget = False
        self.pending = None            # active trade
        self.prechecks = 0
        self.day = {p: [0, 0, 0] for p in self.all_pairs if hasattr(self, "all_pairs")} if hasattr(self, "all_pairs") else {p: [0, 0, 0] for p in C.PAIRS}; self.day_date = data.now().date()
        self.kind_day = {"confirmed": [0, 0], "switched": [0, 0]}; self.prechecks_day = self.switches_day = self.kept_day = 0
        self.all_pairs = list(C.ALL_PAIRS); self.poll_adapter = None; self.stream_since = None
        self.mode_choice = C.MODE if C.MODE in ("auto", "poll", "stream") else "auto"; self.want_mode = None
        self.mode_lock = threading.Lock(); self.load_mode()
        self.compound_active = list(C.COMPOUND_PAIRS); self.lost_ladders = {}; self.rotated_out = set(); self.ladder = {}
        self.block_until = None        # cooldown end (datetime)
        self.sessions = parse_sessions(C.SESSIONS_UTC); self.usage_at = 0.0
        self.active_log = []; self._args = []
        self.shadow_open = []           # relaxed shadow trades awaiting their result (admin only)
        self.shadow_day = {}            # pair -> [W, L] for today's summary
        self.shadow_payout = {}         # pair -> [sum payout, n] for the breakeven comparison
        self.shadow_next = {}           # pair -> datetime (per-pair shadow cooldown)
        self.load_stats(); self.load_shadow(); self.load_channel(); self.load_bench()
        self.load_level(); self.load_strat(); self.load_bugs()

    # ---- persistence ----
    def load_stats(self):
        try:
            with open(C.STATS_FILE) as f: tracker.load(json.load(f)); log.info("Loaded stats for %d pairs", len(tracker.res))
        except FileNotFoundError: pass
        except Exception as e: log.warning("Stats file unreadable (%s); starting fresh", e)
    def save_stats(self):
        tmp = C.STATS_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(tracker.to_json(), f)
        os.replace(tmp, C.STATS_FILE)

    # ---- benched pairs (2 lost ladders): no signals today, swapped out of the stream ----
    def load_bench(self):
        try:
            with open(C.BENCH_FILE) as f: j = json.load(f)
            if j.get("date") == str(self.day_date):
                self.rotated_out = set(j.get("pairs", [])); self.lost_ladders = j.get("lost_ladders", {})
                self.compound_active = j.get("compound_active", self.compound_active)
                if self.rotated_out: log.info("Benched today (persisted): %s", sorted(self.rotated_out))
        except FileNotFoundError: pass
        except Exception as e: log.warning("benched.json unreadable (%s)", e)
    def save_bench(self):
        try:
            tmp = C.BENCH_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"date": str(self.day_date), "pairs": sorted(self.rotated_out), "lost_ladders": self.lost_ladders,
                           "compound_active": self.compound_active}, f)
            os.replace(tmp, C.BENCH_FILE)
        except Exception as e: log.warning("bench save: %s", e)
    def _cached_meta(self, p):
        """(payout, open) from caches only: no request."""
        pay, op = None, True
        for src in (getattr(self.d, "meta", None), getattr(self.poll_adapter, "meta", None), getattr(getattr(self.d, "rest", None), "meta", None)):
            if src and p in src:
                pay, op = src[p]; break
        if pay is None: pay = getattr(self.d, "payout_cache", {}).get(p)
        if p in getattr(self.d, "closed", set()): op = False
        return pay, op
    def stream_swap(self, out_pair, why):
        if not self.is_stream() or out_pair not in self.d.pairs: return
        sa = self.d; cands = []
        for p in self.all_pairs:
            if p in sa.pairs or p in self.rotated_out: continue
            pay, op = self._cached_meta(p)
            if not op or pay is None or pay < C.MIN_PAYOUT: continue
            w, l = self.day.get(p, [0, 0, 0])[:2]
            cands.append((-pay, l, -(tracker.winrate(p) or 0), p, pay))
        cands.sort(); new = [p for p in sa.pairs if p != out_pair]
        if cands:
            _, _, _, inp, pay = cands[0]; new.append(inp)
            sa.set_pairs(new, f"swap {out_pair}->{inp}"); C.PAIRS = list(sa.pairs)
            self.tg.admin(f"🔄 Stream swap: {out_pair} OUT ({why}) → {inp} IN (payout {pay:.0%})")
        else:
            sa.set_pairs(new, f"remove {out_pair}"); C.PAIRS = list(sa.pairs)
            self.tg.admin(f"🔄 Stream swap: {out_pair} OUT ({why}) → no eligible replacement (open, ≥{C.MIN_PAYOUT:.0%} payout, not benched); "
                          f"streaming {len(sa.pairs)} pairs: {sa.pairs}")
    def restore_stream_pairs(self):
        """Midnight: benched pairs return; rebuild the configured stream list (swap-ins go back out)."""
        if not self.is_stream(): return
        want = list(C.STREAM_PAIRS)[:C.STREAM_MAX_INSTRUMENTS]
        if list(self.d.pairs) != want:
            added, removed = self.d.set_pairs(want, "midnight reset"); C.PAIRS = list(self.d.pairs)
            self.tg.admin(f"🌅 New day ({C.reset_local()}): benched pairs back. Stream restored to {self.d.pairs} (in {added}, out {removed})")

    # ---- runtime mode: /stream /poll /auto /mode ----
    def load_mode(self):
        try:
            with open(C.MODE_CHOICE_FILE) as f: m = json.load(f).get("mode")
            if m in ("auto", "poll", "stream"):
                self.mode_choice = m
                if m != C.MODE: self.want_mode = m      # applied on the first tick (main thread)
                log.info("Persisted mode choice: %s", m)
        except FileNotFoundError: pass
        except Exception as e: log.warning("mode_choice.json unreadable (%s)", e)
    def save_mode(self):
        try:
            tmp = C.MODE_CHOICE_FILE + ".tmp"
            with open(tmp, "w") as f: json.dump({"mode": self.mode_choice}, f)
            os.replace(tmp, C.MODE_CHOICE_FILE)
        except Exception as e: log.warning("mode save: %s", e)
    def is_stream(self):
        from stream import StreamAdapter
        return isinstance(self.d, StreamAdapter)
    def _usage(self):
        try:
            u = self.d.usage(); used, quota = u.get("used", 0), u.get("quota") or 0
            return used, quota, (used/quota if quota else 0.0)
        except Exception: return None, None, None
    def mode_info(self):
        used, quota, frac = self._usage()
        cur = "STREAM" if self.is_stream() else ("REPLAY" if isinstance(self.d, ReplayAdapter) else "POLL")
        b = f"{used}/{quota} ({frac:.0%})" if quota else "unknown"
        pend = f"\n  queued switch: {self.want_mode.upper()} (applies after the open trade settles)" if self.want_mode else ""
        return (f"⚙️ Mode: {cur} · {C.hm(self.d.now())} {C.TZ_LABEL} · {'AUTO' if self.mode_choice == 'auto' else 'MANUAL ' + self.mode_choice.upper()}\n"
                f"  pairs: {len(C.PAIRS)} {C.PAIRS if self.is_stream() else ''}\n  API budget: {b} · stream switch at {C.STREAM_SWITCH_AT:.0%}, "
                f"stop at {C.BUDGET_PAUSE:.0%}{pend}")
    def request_mode(self, m):
        """From the Telegram thread: validate, persist, and queue. The switch itself runs in tick() (main thread)."""
        used, quota, frac = self._usage()
        if m == "poll" and frac is not None and quota and frac >= C.BUDGET_PAUSE:
            self.tg.admin(f"⛔ /poll refused: API budget {frac:.0%} ({used}/{quota}) is at/over the {C.BUDGET_PAUSE:.0%} stop threshold. "
                          f"Staying on the stream; polling returns after the budget reset at {C.reset_local()} (/auto) or a bigger plan."); return
        if m == "poll" and self.is_stream() and not (self.poll_adapter or getattr(self.d, "rest", None)):
            self.tg.admin("⛔ /poll refused: no REST connection available (stream-only start)."); return
        with self.mode_lock:
            self.mode_choice = m; self.save_mode(); self.want_mode = m
        warn = ""
        if m == "poll" and frac is not None and frac >= C.STREAM_SWITCH_AT:
            warn = f"\n⚠️ Budget already {frac:.0%}: polling 12 pairs uses ~730 requests/hour; at {C.BUDGET_PAUSE:.0%} it falls back to the stream automatically."
        self.tg.admin(f"🕹 Mode set to {'AUTO' if m == 'auto' else 'MANUAL ' + m.upper()}" +
                      (" (applies after the open trade settles)" if self.pending else "") + warn)
    def apply_mode(self):
        """Main thread only. Never two streams: switch_to_stream/switch_to_poll close the old stream first."""
        with self.mode_lock: m = self.want_mode
        if not m or self.pending or isinstance(self.d, ReplayAdapter): return
        with self.mode_lock: self.want_mode = None
        used, quota, frac = self._usage(); used, quota, frac = used or 0, quota or 1, frac or 0.0
        if m == "stream" and not self.is_stream(): self.switch_to_stream(used, quota, frac, why="manual /stream")
        elif m == "poll" and self.is_stream(): self.switch_to_poll(used, quota, why="manual /poll")
        elif m == "auto":
            if self.is_stream() and frac < C.STREAM_SWITCH_AT and (self.poll_adapter or getattr(self.d, "rest", None)):
                self.switch_to_poll(used, quota, why="/auto (budget below the stream switch)")
            elif not self.is_stream() and frac >= C.STREAM_SWITCH_AT:
                self.switch_to_stream(used, quota, frac, why="/auto (budget over the stream switch)")
            self.tg.admin(f"🤖 AUTO mode: poll all pairs, stream at {C.STREAM_SWITCH_AT:.0%}, back to poll after {C.reset_local()}. " + self.mode_info().split("\n")[0])
        else: self.tg.admin(f"ℹ️ Already in {m.upper()} mode.")

    # ---- public / VIP channel target ----
    def load_channel(self):
        ids = C.channel_ids(); t = None
        try:
            with open(C.CHANNEL_TARGET_FILE) as f: t = json.load(f).get("target")
        except FileNotFoundError: pass
        except Exception as e: log.warning("channel_target.json unreadable (%s)", e)
        if t not in ("public", "vip", "both") or not self._target_ok(t, ids):
            t = "vip" if ids["vip"] else ("public" if ids["public"] else None)
        self.target = t; self.tg.chats = self.chats_for(t)
    def _target_ok(self, t, ids=None):
        ids = ids or C.channel_ids()
        return all(ids[k] for k in (("public", "vip") if t == "both" else (t,)))
    def chats_for(self, t):
        ids = C.channel_ids()
        if t is None: return [("group", C.TELEGRAM_CHANNEL_ID)]
        return [(k, ids[k]) for k in (("public", "vip") if t == "both" else (t,))]
    def target_label(self): return (self.target or "group").upper()
    def set_channel(self, t):
        ids = C.channel_ids()
        if not self._target_ok(t, ids):
            miss = [f"{k.upper()}_CHANNEL_ID" for k in (("public", "vip") if t == "both" else (t,)) if not ids[k]]
            self.tg.admin(f"⚠️ Can't switch to {t.upper()}: {', '.join(miss)} not set in Railway. Still posting to: {self.target_label()}"); return
        self.target = t; self.tg.chats = self.chats_for(t)
        try:
            tmp = C.CHANNEL_TARGET_FILE + ".tmp"
            with open(tmp, "w") as f: json.dump({"target": t}, f)
            os.replace(tmp, C.CHANNEL_TARGET_FILE)
        except Exception as e: log.warning("channel save: %s", e)
        extra = " (open trade keeps its channel for the :57 line and result)" if self.pending else ""
        self.tg.admin(f"📢 Posting to: {self.target_label()}{extra}")
    def channel_info(self):
        ids = C.channel_ids()
        return (f"📢 Posting to: {self.target_label()}\n  PUBLIC_CHANNEL_ID: {'set' if ids['public'] else 'not set'}\n"
                f"  VIP_CHANNEL_ID: {'set' if ids['vip'] else 'not set'}")

    # ---- shadow (relaxed) persistence ----
    def load_shadow(self):
        try:
            with open(C.SHADOW_STATS_FILE) as f: j = json.load(f)
            shadow_tracker.load(j.get("tracker", {})); self.shadow_payout = j.get("payout", {})
            if j.get("day_date") == str(self.day_date): self.shadow_day = j.get("day", {})
            log.info("Loaded shadow stats for %d pairs", len(shadow_tracker.total))
        except FileNotFoundError: pass
        except Exception as e: log.warning("Shadow stats unreadable (%s); starting fresh", e)
    def save_shadow(self):
        tmp = C.SHADOW_STATS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"tracker": shadow_tracker.to_json(), "payout": self.shadow_payout,
                       "day": self.shadow_day, "day_date": str(self.day_date)}, f)
        os.replace(tmp, C.SHADOW_STATS_FILE)

    # ---- shadow (relaxed) engine: admin chat only, never posted, never touches real trades ----
    def shadow_eval(self, df, x, pair, payout, gaps, now):
        if any(t["pair"] == pair for t in self.shadow_open): return
        if self.shadow_next.get(pair) and now < self.shadow_next[pair]: return
        try: s = analyze_relaxed_ensemble(df, pair, payout=payout, gaps=gaps, x=x)
        except Exception as e: log.warning("shadow %s: %s", pair, e); return
        if not s.get("signal"): return
        entry = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        t = {"pair": pair, "signal": s["signal"], "entry_at": entry, "settle_at": entry + timedelta(minutes=C.EXPIRY_MIN),
             "payout": s["payout"], "confidence": s["confidence"], "regime": s["regime"]}
        self.shadow_open.append(t); self.shadow_next[pair] = now + timedelta(minutes=C.SIGNAL_COOLDOWN_MIN)
        self.tg.admin(f"👻 Shadow (relaxed): {pair} {s['signal']} entry {C.hm(entry)} {C.TZ_LABEL} — not posted "
                      f"({s['regime']}, {s['confidence']}%, payout {s['payout']:.0%})")

    def cached_candles(self, pair):
        # Closed candles already in memory: NO OTCharts request is made here.
        for d in (self.d, self.poll_adapter):
            if d is None: continue
            if isinstance(d, OTChartsAdapter):
                df = d.cache.get(pair)
                if df is not None and len(df): return closed_only(df, self.d.now())
            elif hasattr(d, "books") or isinstance(d, ReplayAdapter):   # stream books / local replay: free
                try:
                    df, _, _ = d.candles(pair)
                    if df is not None and len(df): return df
                except Exception: pass
        return None

    def shadow_resolve(self):
        now = self.d.now(); keep = []
        for t in self.shadow_open:
            if now < t["settle_at"]: keep.append(t); continue
            df = self.cached_candles(t["pair"])
            bar = df.loc[df.index == t["entry_at"]] if df is not None else None
            if bar is None or bar.empty:
                if now - t["settle_at"] > timedelta(minutes=C.SHADOW_MAX_WAIT_MIN):
                    self.tg.admin(f"👻 Shadow result {t['pair']}: UNKNOWN (entry candle never fetched)")
                    self.log_shadow(t, float("nan"), float("nan"), "UNKNOWN")
                else: keep.append(t)
                continue
            o, c = float(bar.open.iloc[0]), float(bar.close.iloc[0])
            res = "TIE" if c == o else ("WIN" if (c > o) == (t["signal"] == "CALL") else "LOSS")
            if res != "TIE":
                win = res == "WIN"; shadow_tracker.record(t["pair"], win)
                self.shadow_day.setdefault(t["pair"], [0, 0])[0 if win else 1] += 1
                ps = self.shadow_payout.setdefault(t["pair"], [0.0, 0]); ps[0] += t["payout"]; ps[1] += 1
            self.tg.admin(f"👻 Shadow result {t['pair']}: {res} ({t['signal']} {C.hm(t['entry_at'])}, {o:.5f} → {c:.5f})")
            self.log_shadow(t, o, c, res)
            try: self.save_shadow()
            except Exception as e: log.warning("shadow save: %s", e)
        self.shadow_open = keep

    def log_shadow(self, t, o, c, res):
        import csv
        new = not os.path.exists(C.SHADOW_LOG)
        with open(C.SHADOW_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["entry_at", "pair", "dir", "confidence", "regime", "payout", "open", "close", "result"])
            w.writerow([f"{t['entry_at']:%Y-%m-%d %H:%M}", t["pair"], t["signal"], t["confidence"], t["regime"], t["payout"], o, c, res])

    def shadow_lines(self, day_only=False):
        rows, tw, tl, ps, pn = [], 0, 0, 0.0, 0
        pairs = sorted(set(self.shadow_day) if day_only else set(shadow_tracker.total))
        for p in pairs:
            w, l = (self.shadow_day.get(p, [0, 0]) if day_only else shadow_tracker.total[p])
            if not (w or l): continue
            s_, n_ = self.shadow_payout.get(p, [C.DEFAULT_PAYOUT, 1]); pay = s_/n_ if n_ else C.DEFAULT_PAYOUT
            be = breakeven(pay); wr = w/(w+l); tw += w; tl += l; ps += pay*(w+l); pn += w+l
            rows.append(f"  {p}: {w}W/{l}L · {wr:.0%} vs BE {be:.1%} {'✅' if wr > be else '❌'}")
        if not rows: return ["  no resolved shadow trades yet"]
        be = breakeven(ps/pn); wr = tw/(tw+tl)
        rows.append(f"  Total {tw}W/{tl}L · {wr:.0%} vs BE {be:.1%} {'✅ above' if wr > be else '❌ below'} breakeven")
        return rows

    def shadow_report(self):
        if not C.SHADOW_RELAXED: return "👻 Shadow (relaxed) is OFF (set SHADOW_RELAXED=1 in Railway)."
        return "\n".join(["👻 Shadow (relaxed) — admin only, never posted", "Today:"] + self.shadow_lines(True)
                         + ["All time:"] + self.shadow_lines(False) + [f"Open shadow trades: {len(self.shadow_open)}"])

    # ---- frequency levels ----
    def load_level(self):
        d = C.DEFAULT_LEVEL; self.level_auto = d == "auto"; self.level = C.LEVEL_AUTO_START if self.level_auto else max(1, min(5, int(d)))
        self.level_trades = []; self.level_want = None; self.rate_check_n = 0; self.warned_l5 = False
        try:
            with open(C.LEVEL_FILE) as f: j = json.load(f)
            self.level_auto = bool(j.get("auto")); self.level = int(j.get("level", self.level)); self.level_trades = j.get("trades", [])
        except FileNotFoundError: pass
        except Exception as e: log.warning("level.json unreadable (%s)", e)
        apply_level(self.level)
    def save_level(self):
        try:
            tmp = C.LEVEL_FILE + ".tmp"
            with open(tmp, "w") as f: json.dump({"auto": self.level_auto, "level": self.level, "trades": self.level_trades[-200:]}, f)
            os.replace(tmp, C.LEVEL_FILE)
        except Exception as e: log.warning("level save: %s", e)
    def level_table(self):
        keys = [("MIN_CONF", "conf"), ("ADX_TREND", "ADXt"), ("ADX_RANGE", "ADXr"), ("ER_TREND", "ER"), ("ATR_PCTL_MIN", "ATR%lo"),
                ("ATR_PCTL_MAX", "ATR%hi"), ("SR_MIN_RANGE_ATR", "S/R"), ("BODY_MIN", "body"), ("ENSEMBLE_MIN_AGREE", "agree"),
                ("ENSEMBLE_STRONG_OPP", "opp"), ("ALLOW_CONFLICT", "conflict")]
        rows = []
        for n, lv in C.LEVELS.items():
            mark = " ◀ current" if n == self.level else (" (shadow)" if n == min(5, self.level + 1) else "")
            rows.append(f"L{n}: " + " ".join(f"{lab} {lv[k]}" for k, lab in keys) + mark)
        return rows
    def level_info(self):
        names = {1: "very conservative", 2: "conservative", 3: "balanced", 4: "aggressive", 5: "extreme"}
        sph = self.signals_per_hour(60, 1)
        return "\n".join([f"🎚 Level {self.level} — {names[self.level]} · {'AUTO (1–5)' if self.level_auto else 'MANUAL'} · shadow L{min(5, self.level + 1)}",
                          f"Signals/hour (last 60 active min): {'n/a' if sph is None else format(sph, '.1f')} · target {C.MIN_SIGNALS_PER_HOUR:g}/h",
                          "All levels keep the 77% payout floor, gap rejection and the :57 0.8 ATR switch rule."] + self.level_table())
    def level_cmd(self, args):
        if not args: self.tg.admin(self.level_info()); return
        a = args[0].lower()
        if a == "auto": self.level_want = ("auto", self.level)
        elif a.isdigit() and 1 <= int(a) <= 5: self.level_want = ("manual", int(a))
        else: self.tg.admin("⚠️ Use /level, /level 1..5 or /level auto"); return
        self.tg.admin(f"🎚 Level change queued: {a.upper()} (applies on the next scan)")
    def apply_level_queue(self):
        if not self.level_want: return
        mode, lv = self.level_want; self.level_want = None
        self.level_auto = mode == "auto"; self.set_level(lv, "admin" + (" /level auto" if self.level_auto else f" /level {lv}"))
    def set_level(self, lv, why):
        old = self.level; self.level = lv; apply_level(lv); self.save_level()
        self.tg.admin(f"🎚 Level {old} → {lv} ({why}){' · AUTO' if self.level_auto else ''}. Shadow now L{min(5, lv + 1)}.")
    def signals_per_hour(self, minutes=120, need=60):
        a = self.active_log[-minutes:]
        return (sum(x[1] for x in a) / (len(a)/60)) if len(a) >= need else None
    def recent_wr_be(self):
        last = self.level_trades[-C.LEVEL_AUTO_EVERY:]
        if len(last) < C.LEVEL_AUTO_EVERY: return None, None
        return sum(w for w, _ in last)/len(last), breakeven(sum(p for _, p in last)/len(last))
    def level_rate_check(self):
        """AUTO: every LEVEL_RATE_CHECK_MIN active minutes, signals in the last 60 active minutes vs MIN_SIGNALS_PER_HOUR."""
        if not self.level_auto: return
        self.rate_check_n += 1
        if self.rate_check_n < C.LEVEL_RATE_CHECK_MIN or len(self.active_log) < 60: return
        self.rate_check_n = 0
        sph = self.signals_per_hour(60, 60)
        if sph is None or sph >= C.MIN_SIGNALS_PER_HOUR or self.level >= C.LEVEL_AUTO_MAX: return
        wr, be = self.recent_wr_be()
        if self.level + 1 >= 5 and wr is not None and wr < be:
            if not self.warned_l5:
                self.warned_l5 = True
                self.tg.admin(f"⚠️ Target {C.MIN_SIGNALS_PER_HOUR:g}/h needs level 5 but win rate below breakeven "
                              f"(last {C.LEVEL_AUTO_EVERY}: {wr:.0%} < {be:.1%}); staying at {self.level}")
            return
        self.warned_l5 = False
        self.set_level(self.level + 1, f"auto: {sph:.1f} signals in the last 60 active min < target {C.MIN_SIGNALS_PER_HOUR:g}/h → looser")
    def level_after_trade(self, win, payout):
        self.level_trades.append([int(win), float(payout)]); self.level_trades = self.level_trades[-200:]
        n = len(self.level_trades)
        if self.level_auto and n % C.LEVEL_AUTO_EVERY == 0:
            last = self.level_trades[-C.LEVEL_AUTO_EVERY:]; wr = sum(w for w, _ in last)/len(last)
            be = breakeven(sum(p for _, p in last)/len(last)); sph = self.signals_per_hour()
            if wr < be and self.level > C.LEVEL_AUTO_MIN:
                self.set_level(self.level - 1, f"auto: last {len(last)} WR {wr:.0%} < breakeven {be:.1%} → stricter")
            elif wr > be + C.LEVEL_AUTO_LOOSEN_MARGIN and sph is not None and sph < C.MIN_SIGNALS_PER_HOUR and self.level < C.LEVEL_AUTO_MAX:
                self.set_level(self.level + 1, f"auto: WR {wr:.0%} > BE+5 ({be + .05:.1%}) and only {sph:.1f} signals/h (target {C.MIN_SIGNALS_PER_HOUR:g}) → looser")
            else:
                self.tg.admin(f"🎚 Auto level check: last {len(last)} WR {wr:.0%} vs BE {be:.1%}, "
                              f"{'n/a' if sph is None else format(sph, '.1f')} signals/h → stay at L{self.level}")
        self.save_level()

    # ---- per-strategy stats ----
    def load_strat(self):
        self.strat_total, self.strat_day = {}, {}
        try:
            with open(C.STRATEGY_STATS_FILE) as f: j = json.load(f)
            self.strat_total = j.get("total", {})
            if j.get("day_date") == str(self.day_date): self.strat_day = j.get("day", {})
        except FileNotFoundError: pass
        except Exception as e: log.warning("strategy stats unreadable (%s)", e)
    def save_strat(self):
        try:
            tmp = C.STRATEGY_STATS_FILE + ".tmp"
            with open(tmp, "w") as f: json.dump({"total": self.strat_total, "day": self.strat_day, "day_date": str(self.day_date)}, f)
            os.replace(tmp, C.STRATEGY_STATS_FILE)
        except Exception as e: log.warning("strategy save: %s", e)
    def strat_record(self, t, o, c):
        """Every strategy that voted on a traded signal is scored on ITS OWN direction against the entry candle."""
        for k, v in (t.get("strategies") or {}).items():
            if not v or c == o: continue
            win = (c > o) == (v[0] == "CALL")
            for d in (self.strat_total, self.strat_day): d.setdefault(k, [0, 0])[0 if win else 1] += 1
        self.save_strat()
    def strat_lines(self, day_only=False):
        src = self.strat_day if day_only else self.strat_total; out = []
        for k in "ABCD":
            w, l = src.get(k, [0, 0])
            out.append(f"  {k} {STRATEGY_NAMES[k]}: {w}W/{l}L" + (f" · {w/(w+l):.0%}" if w + l else ""))
        return out
    def strat_report(self):
        return "\n".join(["🧩 Strategies (scored on their own vote whenever they voted on a traded signal)",
                          f"Group signal needs ≥{C.ENSEMBLE_MIN_AGREE} agreeing and none opposing with score ≥{C.ENSEMBLE_STRONG_OPP} (level {self.level})",
                          "Today:"] + self.strat_lines(True) + ["All time:"] + self.strat_lines(False))

    # ---- bug / glitch watcher (EXPERIMENTAL, admin only, paper-tracked) ----
    def load_bugs(self):
        self.bugs = {}; self.bug_open = []; self.bug_scan_at = {}; self.bug_alerts = []
        try:
            with open(C.BUG_FILE) as f: j = json.load(f)
            self.bugs = j.get("patterns", {}); self.bug_alerts = j.get("alerts", [])
        except FileNotFoundError: pass
        except Exception as e: log.warning("bug_watch.json unreadable (%s)", e)
    def save_bugs(self):
        try:
            tmp = C.BUG_FILE + ".tmp"
            with open(tmp, "w") as f: json.dump({"patterns": self.bugs, "alerts": self.bug_alerts[-50:]}, f)
            os.replace(tmp, C.BUG_FILE)
        except Exception as e: log.warning("bug save: %s", e)
    @staticmethod
    def bug_text(pair, key, desc, rule, rate, n):
        col = lambda v: "green" if v > 0 else "red"
        if rule is None: return f"🐞 Bug watch (experimental): {pair} {desc} ({n} samples)"
        pred, d = rule
        if pred == 0: what = f"{desc} close {col(d)} {rate:.0%}"
        elif pred is None: what = f"{desc.split(' → ')[0]} → next {'continues alternating' if key == 'alt4' and d > 0 else ('reverses' if d > 0 else 'continues')} {rate:.0%}"
        else: what = f"{desc} → next {col(pred * d)} {rate:.0%}"
        return f"🐞 Bug watch (experimental): {pair} {what} ({n} samples)"
    def bug_feed(self, pair, x, now):
        if len(x) < max(C.MIN_BARS, 50): return
        # 1) forward paper predictions for active patterns on this pair
        for key, b in self.bugs.items():
            if b["pair"] != pair or b.get("rule") is None: continue
            pred = bug_prediction(x, b["key"], tuple(b["rule"]))
            nt = x.index[-1] + pd.Timedelta("60s")
            if pred is not None and not any(o["k"] == key and o["at"] == nt for o in self.bug_open):
                self.bug_open.append({"k": key, "pair": pair, "at": nt, "pred": int(pred)})
        # 2) periodic history scan (reuses the candles already fetched; no requests)
        live = self.is_stream() and pair in getattr(self.d, "pairs", [])   # live stream pairs: every closed candle
        last = self.bug_scan_at.get(pair)
        if not live and last and now - last < timedelta(minutes=C.BUG_SCAN_MIN): return
        self.bug_scan_at[pair] = now
        for key, desc, rule, rate, n in bug_scan(x, C.BUG_STREAM_MIN_SAMPLES if live else None):
            k = f"{pair}:{key}"; b = self.bugs.get(k)
            if b is None:
                b = self.bugs[k] = {"pair": pair, "key": key, "desc": desc, "rule": list(rule) if rule else None, "status": "watch",
                                    "rate": rate, "n": n, "found": now.isoformat(), "fw": [0, 0], "alerted": None}
            b.update(rate=rate, n=n, rule=list(rule) if rule else None, seen=now.isoformat())
            recent = [a for a in self.bug_alerts if now - pd.Timestamp(a) < pd.Timedelta(hours=1)]
            cool = b.get("alerted") and now - pd.Timestamp(b["alerted"]) < pd.Timedelta(hours=C.BUG_ALERT_COOLDOWN_H)
            if not cool and len(recent) < C.BUG_MAX_ALERTS_PER_HOUR:
                b["alerted"] = now.isoformat(); self.bug_alerts.append(now.isoformat())
                self.tg.admin(self.bug_text(pair, key, desc, rule, rate, n) + " — paper-tracking forward, never posted")
        self.save_bugs()
    def bug_resolve(self):
        if not self.bug_open: return
        now = self.d.now(); keep = []; changed = False
        for o in self.bug_open:
            if now < o["at"] + timedelta(minutes=1): keep.append(o); continue
            df = self.cached_candles(o["pair"]); bar = df.loc[df.index == o["at"]] if df is not None else None
            if bar is None or bar.empty:
                if now - o["at"] < timedelta(minutes=C.SHADOW_MAX_WAIT_MIN): keep.append(o)
                continue
            col = int(np.sign(float(bar.close.iloc[0]) - float(bar.open.iloc[0])))
            if col == 0 or o["k"] not in self.bugs: continue
            self.bugs[o["k"]]["fw"][0 if col == o["pred"] else 1] += 1; changed = True
        self.bug_open = keep
        if changed: self.save_bugs()
    def bug_review(self):
        """Midnight UTC (03:00 UTC+3): forward paper W/L vs breakeven; promote to candidate / demote / drop. Admin only."""
        if not C.BUG_WATCH or not self.bugs: return
        rows = [f"🐞 Daily bug review {self.day_date} (EXPERIMENTAL, admin only, never posted)"]; drop = []
        for k, b in sorted(self.bugs.items()):
            w, l = b["fw"]; n = w + l; pay = self._cached_meta(b["pair"])[0] or C.DEFAULT_PAYOUT; be = breakeven(pay)
            st = b.get("status", "watch"); rate = w/n if n else None
            if b.get("rule") is not None and n >= C.BUG_PROMOTE_MIN_FW:
                if rate >= be + C.BUG_PROMOTE_MARGIN and st != "candidate": st = "candidate"; tag = "⬆️ promoted to CANDIDATE"
                elif rate < be and st == "candidate": st = "watch"; tag = "⬇️ demoted to watch"
                elif rate < be: drop.append(k); tag = "🗑 dropped"
                else: tag = "kept"
            else: tag = f"kept (needs {C.BUG_PROMOTE_MIN_FW} forward, has {n})" if b.get("rule") is not None else "info only"
            b["status"] = st
            rows.append(f"  {b['pair']} {b['key']} [{st}]: forward {w}W/{l}L" + (f" {rate:.0%} vs BE {be:.1%}" if n else "") + f" → {tag}")
        for k in drop: self.bugs.pop(k, None)
        cands = [f"{b['pair']} {b['key']}" for b in self.bugs.values() if b.get("status") == "candidate"]
        rows.append(f"  Candidates carried over: {', '.join(cands) or 'none'} (admin only; never auto-posted)")
        self.save_bugs(); self.tg.admin("\n".join(rows))

    def bug_report(self):
        if not C.BUG_WATCH: return "🐞 Bug watch is OFF (BUG_WATCH=0)."
        if not self.bugs: return ("🐞 Bug watch (EXPERIMENTAL, admin only): no pattern has passed the filter yet "
                                  f"(≥{C.BUG_MIN_SAMPLES} samples, ≥{C.BUG_MIN_RATE:.0%}, p<{C.BUG_MAX_P}).")
        rows = ["🐞 Bug watch (EXPERIMENTAL, admin only, never posted). History rate → forward paper W/L:"]
        cands = [f"{b['pair']} {b['key']}" for b in self.bugs.values() if b.get("status") == "candidate"]
        rows.append(f"  Candidates: {', '.join(cands) or 'none yet'} (≥{C.BUG_PROMOTE_MIN_FW} forward at ≥ breakeven+{C.BUG_PROMOTE_MARGIN*100:.0f} pts)")
        for k, b in sorted(self.bugs.items(), key=lambda kv: -kv[1]["rate"])[:25]:
            w, l = b["fw"]
            rows.append("  " + self.bug_text(b["pair"], b["key"], b["desc"], tuple(b["rule"]) if b.get("rule") else None, b["rate"], b["n"])[2:]
                        .replace("Bug watch (experimental): ", "") + f" [{b.get('status', 'watch')}] · forward {w}W/{l}L" + (f" {w/(w+l):.0%}" if w + l else ""))
        return "\n".join(rows)

    # ---- admin commands ----
    # Single source of truth: /commands, /help and Telegram's Menu (setMyCommands) are all built from this table.
    COMMANDS = [
        ("Control",  "/pause",    "stop sending signals"),
        ("Control",  "/resume",   "resume signals (also clears a budget pause)"),
        ("Control",  "/status",   "pause state, per-pair W/L, compounding eligibility"),
        ("Stats",    "/stats",    "today's full summary now (W/L, skips, switches, budget, compounding, shadow)"),
        ("Stats",    "/strategies", "per-strategy W/L (A trend-pullback, B range-reversion, C S/R-rejection, D voting)"),
        ("Stats",    "/bugs",     "EXPERIMENTAL bug/glitch watcher: patterns found + forward paper results"),
        ("Stats",    "/shadow",   "relaxed shadow stats: W/L, win rate vs breakeven (never posted)"),
        ("Channels", "/channel",  "show where group posts go"),
        ("Channels", "/public",   "post to the PUBLIC channel only"),
        ("Channels", "/vip",      "post to the VIP channel only"),
        ("Channels", "/both",     "post to PUBLIC and VIP"),
        ("Mode",     "/level",    "frequency level: /level shows; /level 1..5 or /level auto (1 strict … 5 extreme)"),
        ("Mode",     "/mode",     "current mode (stream/poll), manual or auto, API budget"),
        ("Mode",     "/stream",   "switch to the live stream now (5 pairs)"),
        ("Mode",     "/poll",     "switch to polling all pairs now"),
        ("Mode",     "/auto",     "automatic: poll, stream at 93%, back to poll after the reset"),
        ("Test",     "/test",     "post one sample TEST card to the current channel(s) (alias /testcard)"),
        ("Test",     "/commands", "this list (alias /help)"),
    ]
    def handlers(self):
        return {
            "/pause": lambda: (setattr(self, "manual_pause", True), self.tg.admin("⏸ Paused by admin")),
            "/resume": lambda: (setattr(self, "manual_pause", False), setattr(self, "budget_pause", False), self.tg.admin("▶️ Resumed")),
            "/status": lambda: self.tg.admin(self.status()),
            "/stats": lambda: self.tg.admin(self.summary_text(f"📊 Stats so far today {self.day_date} (day {C.reset_local()} → {C.reset_local()}) · now {C.hm(self.d.now())} {C.TZ_LABEL} — on demand, nothing reset")),
            "/shadow": lambda: self.tg.admin(self.shadow_report()),
            "/strategies": lambda: self.tg.admin(self.strat_report()),
            "/bugs": lambda: self.tg.admin(self.bug_report()),
            "/level": lambda: self.level_cmd(self._args),
            "/channel": lambda: self.tg.admin(self.channel_info()),
            "/public": lambda: self.set_channel("public"), "/vip": lambda: self.set_channel("vip"), "/both": lambda: self.set_channel("both"),
            "/mode": lambda: self.tg.admin(self.mode_info()),
            "/stream": lambda: self.request_mode("stream"), "/poll": lambda: self.request_mode("poll"), "/auto": lambda: self.request_mode("auto"),
            "/test": self.test_card, "/testcard": self.test_card,
            "/commands": lambda: self.tg.admin(self.commands_text()), "/help": lambda: self.tg.admin(self.commands_text()),
        }
    def commands_text(self):
        out, grp = ["🤖 Admin commands (times in " + C.TZ_LABEL + ")"], None
        for g, c, d in self.COMMANDS:
            if g != grp: out.append(f"\n<b>{g}</b>"); grp = g
            out.append(f"{c} — {d}")
        return "\n".join(out)
    def command(self, cmd):
        parts = (cmd or "").split(); self._args = parts[1:]
        h = self.handlers().get(parts[0] if parts else "")
        if h: h()

    def test_card(self):
        """Post a sample card with the real card builder + chart. Touches no stats, cooldowns, trades.csv, precheck or results."""
        pair, d = "USDINR", "CALL"
        lab = self.d.label(pair) if hasattr(self.d, "label") else "USDINR_otc"
        entry = self.d.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
        df, src = None, "synthetic"
        try:
            df, _, _ = self.d.candles(pair)
            if df is not None and len(df) >= 30: src = "live"
            else: df = None
        except Exception as e: log.warning("test card: candles unavailable (%s)", e)
        if df is None:
            from analyzer import synthetic
            df = synthetic(n=60, seed=11, start=83.2)
        p1, p2 = self.playbook(d)
        hdr = "🧪 TEST CARD — NOT A LIVE SIGNAL"
        cap = hdr + "\n" + signal_caption(lab, d, entry, C.EXPIRY_MIN, (p1, p2) if C.PRECHECK_MODE in ("confirm", "direction") else None,
                                          C.PRECHECK_SECOND, self.wait_line()) + "\n" + hdr
        sent = False
        if C.SIGNAL_CHART:
            try: sent = self.tg.group_photo(render_chart(df, lab), cap, "test_card.png")
            except Exception as e: log.warning("test card chart failed (%s); sending text", e)
        if not sent: self.tg.group(cap)
        self.tg.admin(f"🧪 Test card posted to {self.target_label()} (chart from {src} candles)")
    def status(self):
        sph = self.signals_per_hour(60, 1)
        lines = [f"Running: {not (self.manual_pause or self.budget_pause or not self.in_session())} | paused by admin: {self.manual_pause} | paused for budget: {self.budget_pause} | inside trading hours: {self.in_session()}"]
        lines.append(f"Level {self.level}{' (auto)' if self.level_auto else ''} · signals/hour (last 60 active min): "
                     f"{'n/a' if sph is None else format(sph, '.1f')} · target {C.MIN_SIGNALS_PER_HOUR:g}/h")
        for p in C.PAIRS:
            wr = tracker.winrate(p); w, l = tracker.total[p]
            lines.append(f"{p}: {w}W/{l}L" + (f" rolling {wr:.0%}" if wr is not None else ""))
        lines.append(f"Compounding active: {self.compound_active}")
        lines.append("Eligibility: " + self.eligibility_line())
        return "\n".join(lines)

    def in_session(self):
        if not self.sessions: return True
        now = self.d.now(); m = now.hour*60 + now.minute
        return any(a <= m < b if a < b else (m >= a or m < b) for a, b in self.sessions)

    def check_budget(self):
        if time.time() - self.usage_at < C.USAGE_CHECK_SEC and not isinstance(self.d, ReplayAdapter): return
        self.usage_at = time.time()
        try: u = self.d.usage()
        except Exception as e: log.warning("usage check failed: %s", e); return
        used, quota = u.get("used", 0), u.get("quota") or 1; frac = used/quota
        from stream import StreamAdapter
        if isinstance(self.d, StreamAdapter):
            if self.mode_choice == "auto" and self.stream_since and self.d.now().date() > self.stream_since and frac < C.STREAM_SWITCH_AT \
                    and not self.pending and self.poll_adapter is not None:
                self.switch_to_poll(used, quota); return
            if frac >= C.BUDGET_PAUSE and self.d.rest_allowed:
                self.d.rest_allowed = False
                self.tg.admin(f"🛑 API budget {frac:.0%} ({used}/{quota}): REST calls stopped (payouts from cache); stream keeps running.")
            return
        reserve = len(C.STREAM_PAIRS[:C.STREAM_MAX_INSTRUMENTS]) + 2 + len(C.PAIRS)   # warm-up + stream open + one poll cycle
        if self.mode_choice == "auto" and isinstance(self.d, OTChartsAdapter) and (frac >= C.STREAM_SWITCH_AT or quota - used <= reserve) \
                and not self.pending:
            self.switch_to_stream(used, quota, frac); return
        if self.mode_choice == "poll" and isinstance(self.d, OTChartsAdapter) and (frac >= C.BUDGET_PAUSE or quota - used <= reserve) \
                and not self.pending:   # safety: manual poll would run dry -> stream (choice stays POLL; /auto to clear)
            self.switch_to_stream(used, quota, frac, why=f"safety fallback: manual POLL hit the {C.BUDGET_PAUSE:.0%} stop threshold"); return
        if frac >= C.BUDGET_PAUSE and not self.budget_pause:
            self.budget_pause = True; self.tg.admin(f"🛑 API budget {frac:.0%} ({used}/{quota}). Auto-paused. /resume after reset.")
        elif frac >= C.BUDGET_WARN and not self.warned_budget:
            self.warned_budget = True; self.tg.admin(f"⚠️ API budget {frac:.0%} used ({used}/{quota}).")
        elif frac < C.BUDGET_WARN: self.warned_budget = False
        log.info("API usage %d/%d (%.0f%%)", used, quota, frac*100)

    # ---- auto mode switching ----
    def switch_to_stream(self, used, quota, frac, why=None):
        from stream import StreamAdapter
        if self.is_stream(): return
        try:
            C.MIN_BARS = C.STREAM_MIN_BARS
            rest = self.d; sa = StreamAdapter(rest=rest, stop=threading.Event())   # SSEStream closes any older stream first
            sa.rest_allowed = frac < C.BUDGET_PAUSE
            self.poll_adapter, self.d, self.stream_since = rest, sa, sa.now().date()
            C.PAIRS = list(sa.pairs)
            self.budget_pause = False
            self.tg.admin(f"🔀 Switched to STREAM mode{' (' + why + ')' if why else ''} at {frac:.0%} budget ({used}/{quota}): 1 stream × {len(sa.pairs)} pairs "
                          f"{sa.pairs}. Polling stopped for the other {len(self.all_pairs) - len(sa.pairs)} pairs"
                          + (" until the budget reset at {C.reset_local()}." if self.mode_choice == "auto" else " until /poll or /auto."))
        except Exception as e:
            C.MIN_BARS = C.POLL_MIN_BARS; log.exception("stream switch failed"); self.tg.admin(f"⚠️ Stream switch failed: {e}. Staying in poll mode.")

    def switch_to_poll(self, used, quota, why=None):
        if not self.is_stream(): return
        sa, rest = self.d, (self.poll_adapter or getattr(self.d, "rest", None))
        if rest is None or not isinstance(rest, OTChartsAdapter):
            self.tg.admin("⚠️ Can't switch to POLL: no REST adapter."); return
        for p, book in sa.books.items():           # hand stream-built candles back to the poller (no extra requests)
            df = book.df()
            if df is not None: rest.cache[p] = df
        for p in self.all_pairs:
            if p not in sa.books: rest.cache.pop(p, None)   # stale: refetch full history once
        C.MIN_BARS = C.POLL_MIN_BARS
        sa.close(); self.d = rest; C.PAIRS = list(self.all_pairs); self.stream_since = None; self.poll_adapter = None
        rest.meta_at = 0
        if any(p not in getattr(rest, "res", {}) for p in C.PAIRS) and hasattr(rest, "resolve_all"):
            try: rest.resolve_all(only_missing=True)
            except Exception as e: log.warning("resolve on /poll: %s", e)
        self.tg.admin((f"🔁 Switched to POLL ({why}) ({used}/{quota})" if why else f"🔁 Daily quota reset at {C.reset_local()} ({used}/{quota})")
                      + f": stream closed, POLL mode for all {len(C.PAIRS)} pairs.")

    # ---- one minute ----
    def tick(self):
        try: self._tick()
        finally:
            if C.SHADOW_RELAXED:
                try: self.shadow_resolve()
                except Exception: log.exception("shadow resolve failed")
            if C.BUG_WATCH:
                try: self.bug_resolve()
                except Exception: log.exception("bug resolve failed")

    def _tick(self):
        now = self.d.now()
        self.maybe_daily_summary()
        if hasattr(self.d, "admin_messages"):
            for m in self.d.admin_messages(): self.tg.admin(m)
        if hasattr(self.d, "persist"): self.d.persist()
        self.apply_mode()
        self.check_budget()
        if self.manual_pause or self.budget_pause or not self.in_session():
            log.info("idle (paused/out of session)"); return
        self.active_log.append([now, 0]); self.active_log = self.active_log[-180:]
        self.apply_level_queue(); self.level_rate_check()
        # settle pending trade first
        if self.pending and now >= self.pending["settle_at"]:
            self.settle(); return
        if self.pending or (self.block_until and now < self.block_until):
            return
        try: meta = self.d.pair_meta()
        except BudgetExhausted: self.budget_pause = True; self.tg.admin("🛑 OTCharts 429: quota exhausted. Paused."); return
        best = None
        for pair in C.PAIRS:
            if pair in self.rotated_out: continue                         # benched today (2 lost ladders)
            payout, is_open = meta.get(pair, (None, False))
            if not is_open: continue
            if payout is None: payout = C.DEFAULT_PAYOUT if isinstance(self.d, ReplayAdapter) else None
            if payout is None or payout < C.MIN_PAYOUT: continue          # skip w/o spending a request
            try: df, gaps, open_ = self.d.candles(pair, include_forming=(C.SCAN_MODE == "early52"))
            except BudgetExhausted: self.budget_pause = True; self.tg.admin("🛑 OTCharts 429. Paused."); return
            except Exception as e: log.warning("%s candles: %s", pair, e); continue
            if df is None or len(df) < C.MIN_BARS or not open_: continue
            if now - (df.index[-1] + pd.Timedelta("60s")) > pd.Timedelta("90s") + (pd.Timedelta("60s") if C.SCAN_MODE == "early52" else pd.Timedelta(0)):
                log.info("%s stale data, skip", pair); continue
            x = compute(df)
            if C.BUG_WATCH:
                try: self.bug_feed(pair, x, now)
                except Exception as e: log.warning("bug watch %s: %s", pair, e)
            s = ensemble(df, pair, payout=payout, gaps=gaps, x=x)
            if C.SHADOW_RELAXED and not s["signal"]: self.shadow_eval(df, x, pair, payout, gaps, now)
            if s["signal"]: s["_df"] = df
            if s["signal"] and (best is None or s["confidence"] > best["confidence"]): best = s
        if not best:
            if C.SEND_NO_SETUP: self.tg.admin("⚪ No setup"); return
            return
        entry = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
        best["entry_at"], best["settle_at"] = entry, entry + timedelta(minutes=C.EXPIRY_MIN)
        best["precheck_at"] = entry - timedelta(seconds=60 - C.PRECHECK_SECOND); best["prechecked"] = C.PRECHECK_MODE == "off"
        best["level"] = self.level
        best["chats"] = list(self.tg.chats or self.chats_for(self.target))   # :57 line + result follow the card
        self.pending = best; self.block_until = now + timedelta(minutes=C.SIGNAL_COOLDOWN_MIN)
        if self.active_log: self.active_log[-1][1] = 1
        self.post_signal(best)

    # ---------- messages ----------
    def playbook(self, d):
        s = C.PRECHECK_SECOND
        if C.PRECHECK_MODE == "direction":
            if d == "CALL": return (f"If current candle is GREEN at :{s} → CALL", f"🔄 If RED strongly → PUT")
            return (f"If current candle is RED at :{s} → PUT", f"🔄 If GREEN strongly → CALL")
        if d == "CALL": return (f"If the current candle is GREEN at :{s} → enter CALL", "⛔ If it's RED strongly → SKIP")
        return (f"If the current candle is RED at :{s} → enter PUT", "⛔ If it's GREEN strongly → SKIP")

    def wait_line(self):
        if C.PRECHECK_MODE == "direction": return f"Wait for ✅ CONFIRMED / 🔄 SWITCH at :{C.PRECHECK_SECOND}"
        return f"Wait for ✅ CONFIRMED / ❌ SKIP at :{C.PRECHECK_SECOND}"

    def post_signal(self, s):
        lab = self.d.label(s["pair"]); entry = s["entry_at"]; d = s["signal"]
        arrow = "🟢 CALL ⬆️" if d == "CALL" else "🔴 PUT ⬇️"
        p1, p2 = self.playbook(d)
        cap = signal_caption(lab, d, entry, C.EXPIRY_MIN, (p1, p2) if C.PRECHECK_MODE in ("confirm", "direction") else None, C.PRECHECK_SECOND, self.wait_line())
        sent = False
        if C.SIGNAL_CHART and s.get("_df") is not None:
            try: sent = self.tg.group_photo(render_chart(s["_df"], lab), cap, f"signal_{s['pair']}_{entry:%Y%m%d_%H%M}.png", chats=s.get("chats"))
            except Exception as e: log.warning("signal chart failed (%s); sending text", e)
        if not sent: self.tg.group(cap, chats=s.get("chats"))
        s.pop("_df", None)
        a = s["atr"]
        votes = "\n".join(f"  • {k}: {'+1' if v > 0 else ('-1' if v < 0 else ' 0')} {t or '(no vote)'}" for k, (v, t) in s.get("votes", {}).items())
        comp = ""
        if s["pair"] in self.compound_active: comp = f"\n🔥 compound pair: ladder step {self.ladder.get(s['pair'], 0)+1}/{C.COMPOUND_TARGET}"
        self.tg.admin(
            f"🟢 Sent {s['pair']} → {lab} {d}{self.d.note(s['pair'], d)} · entry {C.hm(entry)} {C.TZ_LABEL}\n"
            f"Regime {s['regime']} (ADX {s.get('adx','?')}) · score {s['score']}/{s['max_score']} ({s['confidence']}%) · payout {s['payout']:.0%} (BE {s['breakeven']:.1%})\n"
            f"Level {self.level}{' (auto)' if self.level_auto else ''} · strategies agreed: {', '.join(k + ' ' + STRATEGY_NAMES[k] for k in s.get('agree', []))}\n"
            f"  {strategies_line(s)}\n"
            f"S {s['support']:.5f} / R {s['resistance']:.5f} · ATR {a:.5f} · overlap={s.get('overlap')}\nVoting engine (D) votes:\n{votes}\n"
            + (f"\nDirection mode: no skips. SWITCH only if the forming candle is clearly opposite (body ≥ {C.SWITCH_MIN_BODY_ATR} ATR); "
               "any weaker opposite candle or doji → keep the original direction (CONFIRMED)." if C.PRECHECK_MODE == "direction" else "") +
            f"\nPlaybook reasoning:\n  • '{p1}': confirms if the candle is with the signal, or the opposing body is < {C.PC_SMALL_BODY_ATR} ATR, and price hasn't run > {C.PC_EXHAUST_ATR} ATR (exhausted) or reached S/R\n"
            f"  • '{p2}': cancels if the opposing body is ≥ {C.PC_STRONG_BODY_ATR} ATR, it moved > {C.PC_MAX_ADVERSE_ATR} ATR against, or crossed back through EMA9 / the signal-candle midpoint{comp}")

    def run_precheck(self):
        """Re-read the forming candle just before entry (1 extra request) and confirm or cancel."""
        t = self.pending; t["prechecked"] = True
        try: df, _, _ = self.d.candles(t["pair"], include_forming=True)
        except Exception as e: log.warning("precheck fetch: %s", e); df = None
        fstart = t["entry_at"] - timedelta(minutes=1)
        bar = df.loc[df.index == fstart] if df is not None else None
        if bar is None or bar.empty:
            ok, why = (C.PRECHECK_MODE != "confirm"), "forming candle unavailable"   # direction mode: keep original
        else:
            ok, why = precheck(t, float(bar.open.iloc[0]), float(bar.close.iloc[0]))
        t["precheck_ok"], t["precheck_reason"] = ok, why
        self.prechecks += 1; self.prechecks_day += 1
        lab = self.d.label(t["pair"])
        if bar is not None and not bar.empty:
            o_, c_ = float(bar.open.iloc[0]), float(bar.close.iloc[0])
            colour = "GREEN" if c_ > o_ else ("RED" if c_ < o_ else "DOJI"); body = abs(c_-o_)/(t["atr"] or 1e-12)
            cinfo = f"forming candle {colour}, body {body:.2f} ATR"
        else: cinfo = "forming candle unavailable"
        t["precheck_candle"] = cinfo
        if C.PRECHECK_MODE == "direction":
            name = display_name(lab); kind = precheck_flip_kind(ok, why); t["original_signal"] = t["signal"]
            opp = None
            if bar is not None and not bar.empty:
                opp = switch_body_atr(t, float(bar.open.iloc[0]), float(bar.close.iloc[0]))
            if kind is not None and (opp is None or opp < C.SWITCH_MIN_BODY_ATR):
                # every switch path (momentum AND reversion) needs a clearly opposite candle, body >= SWITCH_MIN_BODY_ATR
                self.kept_day += 1
                why = (f"{why}; opposite candle {max(opp or 0, 0):.2f} ATR < {C.SWITCH_MIN_BODY_ATR} → kept {t['signal']}"
                       + ("" if opp is None or opp > 0 else " (candle with the signal or doji)"))
                kind = None
            if kind is None:
                t["kind"] = "confirmed"
                self.tg.group(f"✅ CONFIRMED {name} {t['signal']} — enter now", chats=t.get("chats"))
                self.tg.admin(f"✅ Precheck {t['pair']} ({lab}): {cinfo} → CONFIRMED {t['signal']}. {why}")
            else:
                t["kind"] = f"switched-{kind}"; t["signal"] = "PUT" if t["signal"] == "CALL" else "CALL"; self.switches_day += 1
                why = f"{why}; opposite candle {opp:.2f} ATR ≥ {C.SWITCH_MIN_BODY_ATR}"
                self.tg.group(f"🔄 SWITCH {name} {t['signal']} — enter now", chats=t.get("chats"))
                self.tg.admin(f"🔄 Precheck {t['pair']} ({lab}): {cinfo} → SWITCH {t['original_signal']}→{t['signal']} "
                              f"({'momentum: follow the opposing candle' if kind == 'momentum' else 'reversion: move exhausted / at S/R'}). {why}")
        elif C.PRECHECK_MODE == "confirm":
            if ok:
                self.tg.group(f"✅ CONFIRMED {lab} {t['signal']} — enter now at {C.hm(t['entry_at'])}:00 {C.TZ_LABEL}", chats=t.get("chats"))
                self.tg.admin(f"✅ Precheck OK {t['pair']} ({lab}): {cinfo} → TRADE. {why}")
            else:
                self.tg.group(f"❌ SKIP {lab} {t['signal']}", chats=t.get("chats"))
                self.tg.admin(f"❌ Precheck CANCEL {t['pair']} ({lab}): {cinfo} → SKIPPED (not a loss). {why}")
                self.day.setdefault(t["pair"], [0, 0, 0])[2] += 1
                self.log_trade(t, float("nan"), float("nan"), "CANCELLED"); self.pending = None
                self.block_until = self.d.now() + timedelta(minutes=C.RESULT_COOLDOWN_MIN)
        else:
            self.tg.admin(f"(log) precheck {t['pair']}: {cinfo} → {'ok' if ok else 'would cancel'}. {why}")

    def settle(self):
        t = self.pending; self.pending = None
        try: df, _, _ = self.d.candles(t["pair"])
        except Exception as e: log.warning("settle fetch: %s", e); df = None
        bar = df.loc[df.index == t["entry_at"]] if df is not None else None
        if bar is None or bar.empty:
            self.tg.admin(f"⚠️ Result unknown for {t['pair']} (entry bar missing)"); res = None
        else:
            o, c = float(bar.open.iloc[0]), float(bar.close.iloc[0])
            win = c > o if t["signal"] == "CALL" else c < o; tie = c == o; res = "WIN" if win else ("TIE" if tie else "LOSS")
            if not tie:
                tracker.record(t["pair"], win)
                self.strat_record(t, o, c); self.level_after_trade(win, t["payout"])
                if self.n_trades(t["pair"]) == C.COMPOUND_MIN_TRADES:
                    self.tg.admin(f"🎓 {t['pair']} now eligible for compounding ({C.COMPOUND_MIN_TRADES} trades, WR {tracker.total[t['pair']][0]/C.COMPOUND_MIN_TRADES:.0%})")
            self.save_stats(); self.log_trade(t, o, c, res)
            if C.GROUP_SHOW_RESULTS:
                lab = self.d.label(t["pair"]); cap = result_caption(res, lab, t["signal"], o, c); sent = False
                if C.RESULT_CHART:
                    try:
                        png = render_result_chart(df, t["entry_at"], t["signal"], lab, res, C.EXPIRY_MIN)
                        sent = self.tg.group_photo(png, cap, f"{t['pair']}_{t['entry_at']:%Y%m%d_%H%M}.png", chats=t.get("chats"))
                    except Exception as e: log.warning("chart render failed (%s); sending text", e)
                if not sent: self.tg.group(cap, chats=t.get("chats"))
            if not tie:
                self.day.setdefault(t["pair"], [0, 0, 0])[0 if win else 1] += 1
                k = "switched" if t.get("kind", "").startswith("switched") else "confirmed"
                self.kind_day[k][0 if win else 1] += 1
            self.tg.admin(f"📋 Round {C.hm(t['entry_at'])} {C.TZ_LABEL} {t['pair']} ({self.d.label(t['pair'])}) {t.get('original_signal', t['signal'])}"
                          f"{(' → traded ' + t['signal'] + ' (' + t.get('kind', '') + ')') if t.get('original_signal', t['signal']) != t['signal'] else ' (confirmed)'} · "
                          f"precheck: {t.get('precheck_candle', 'n/a')} → traded · result {res} ({o:.5f} → {c:.5f})\n"
                          f"  strategies agreed: {','.join(t.get('agree', [])) or '—'} · {strategies_line(t)} · level {t.get('level', '?')}")
            if not tie: self.ladder_update(t["pair"], win)
            g = tracker.gate(t["pair"], t["payout"])
            if g: self.tg.admin(f"⏸ {t['pair']} gated: {g}")
        self.block_until = self.d.now() + timedelta(minutes=C.RESULT_COOLDOWN_MIN)

    # ---------- compounding ladders / rotation ----------
    def ladder_update(self, pair, win):
        if pair not in self.compound_active: return
        if win:
            self.ladder[pair] = self.ladder.get(pair, 0) + 1
            if self.ladder[pair] >= C.COMPOUND_TARGET:
                self.tg.admin(f"🏁 Ladder complete on {pair}: {self.ladder[pair]}/{C.COMPOUND_TARGET}"); self.ladder[pair] = 0
            return
        steps = self.ladder.get(pair, 0); self.ladder[pair] = 0
        self.lost_ladders[pair] = self.lost_ladders.get(pair, 0) + 1
        self.tg.admin(f"💥 Lost ladder on {pair} at step {steps+1} ({self.lost_ladders[pair]} lost today)")
        if self.lost_ladders[pair] >= C.MAX_LOST_LADDERS_PER_DAY:
            self.compound_active.remove(pair)
            pool = [p for p in C.PAIRS if p not in self.compound_active and p not in self.rotated_out
                    and self.n_trades(p) >= C.COMPOUND_MIN_TRADES and not tracker.gate(p, C.DEFAULT_PAYOUT)]
            pool.sort(key=lambda p: (tracker.winrate(p) or 0), reverse=True)
            repl = pool[0] if pool else None
            if repl: self.compound_active.append(repl)
            self.rotated_out.add(pair); self.save_bench()
            if repl:
                self.tg.admin(f"🔄 Rotation: {pair} OUT of compounding for today ({self.lost_ladders[pair]} lost ladders) → "
                              f"{repl} IN ({self.n_trades(repl)} trades, rolling WR {tracker.winrate(repl):.0%}). Active: {self.compound_active}")
            else:
                self.tg.admin(f"🔄 Rotation: {pair} OUT of compounding for today ({self.lost_ladders[pair]} lost ladders) → "
                              f"slot left EMPTY: no pair has ≥{C.COMPOUND_MIN_TRADES} settled trades. {self.eligibility_line()}. Active: {self.compound_active}")
            log.info("Rotation %s -> %s", pair, repl)
            self.tg.admin(f"🪑 {pair} benched for today: no signals until {C.reset_local()}")
            self.stream_swap(pair, f"{self.lost_ladders[pair]} lost ladders")

    def n_trades(self, p):
        w, l = tracker.total[p]; return w + l

    def eligibility_line(self):
        out = []
        for p in C.PAIRS:
            n = self.n_trades(p)
            out.append(f"{p} {n}/{C.COMPOUND_MIN_TRADES} " + ("eligible" if n >= C.COMPOUND_MIN_TRADES else "not eligible"))
        return " · ".join(out)

    def summary_text(self, title=None):
        """Full daily summary (real trades + 👻 shadow section). Read-only: resets nothing."""
        lines = [title or f"📊 Daily summary {self.day_date} (day {C.reset_local()} → {C.reset_local()}; budget resets {C.reset_local()})"]
        tw = tl = tc = 0
        for p in self.all_pairs:
            w, l, c = self.day.get(p, (0, 0, 0)); tw += w; tl += l; tc += c
            if w or l or c: lines.append(f"  {p}: {w}W / {l}L / {c} skipped" + (f" · {w/(w+l):.0%}" if w+l else ""))
        if not (tw or tl or tc): lines.append("  no real trades yet")
        lines.append(f"  Total {tw}W / {tl}L / {tc} skipped" + (f" · WR {tw/(tw+tl):.0%}" if tw + tl else "")
                     + f" · prechecks {self.prechecks_day}")
        lines.append(f"  :57 switches {self.switches_day} · weak opposite candles kept {self.kept_day}")
        for k, (w, l) in self.kind_day.items():
            lines.append(f"  {k}: {w}W / {l}L" + (f" · WR {w/(w+l):.0%}" if w + l else ""))
        try: u = self.d.usage(); lines.append(f"  API budget used {u.get('used')}/{u.get('quota')}")
        except Exception: pass
        if self.rotated_out: lines.append(f"  Rotated out today: {sorted(self.rotated_out)}")
        lines.append(f"  Compounding active: {self.compound_active}")
        lines.append("  Eligibility: " + self.eligibility_line())
        lines.append(f"  Level {self.level}{' (auto)' if self.level_auto else ' (manual)'}")
        lines += ["  🧩 Strategies (today):"] + ["  " + l for l in self.strat_lines(True)]
        if C.SHADOW_RELAXED:
            lines += ["  👻 Shadow (relaxed) — not posted:"] + ["  " + l for l in self.shadow_lines(True)]
        return "\n".join(lines)

    def maybe_daily_summary(self, force=False):
        today = self.d.now().date()
        if self.day_date == today and not force: return
        if any(sum(v) for v in self.day.values()) or (C.SHADOW_RELAXED and any(sum(v) for v in self.shadow_day.values())):
            self.tg.admin(self.summary_text())
        try: self.bug_review()
        except Exception as e: log.warning("bug review: %s", e)
        self.shadow_day = {}; self.strat_day = {}; self.save_strat()
        self.day = {p: [0, 0, 0] for p in self.all_pairs}; self.day_date = today
        self.kind_day = {"confirmed": [0, 0], "switched": [0, 0]}; self.prechecks_day = self.switches_day = self.kept_day = 0
        had_bench = bool(self.rotated_out)
        self.compound_active = list(C.COMPOUND_PAIRS); self.lost_ladders = {}; self.rotated_out = set(); self.ladder = {}
        self.save_bench()
        if had_bench or (self.is_stream() and list(self.d.pairs) != list(C.STREAM_PAIRS)[:C.STREAM_MAX_INSTRUMENTS]):
            try: self.restore_stream_pairs()
            except Exception as e: log.warning("stream restore: %s", e)

    def log_trade(self, t, o, c, res):
        import csv
        new = not os.path.exists(C.TRADE_LOG)
        with open(C.TRADE_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["entry_at", "pair", "dir", "confidence", "regime", "payout", "overlap", "precheck", "original_dir", "traded_as", "open", "close", "result"])
            w.writerow([f"{t['entry_at']:%Y-%m-%d %H:%M}", t["pair"], t["signal"], t["confidence"], t["regime"], t["payout"],
                        t.get("overlap"), t.get("precheck_reason", ""), t.get("original_signal", t["signal"]), t.get("kind", ""), o, c, res])

def main():
    miss = C.missing_secrets()
    if miss:
        log.error("Missing required env vars: %s. Set them in Railway > Variables. Exiting.", ", ".join(miss)); sys.exit(2)
    from stream import StreamAdapter, MockTickSource
    mock = None
    if C.DATA_SOURCE == "replay": data = ReplayAdapter(C.REPLAY_CSV)
    elif C.DATA_SOURCE == "mockstream":
        mock = MockTickSource([C.symbol_for(p) for p in C.STREAM_PAIRS[:C.STREAM_MAX_INSTRUMENTS]],
                              (pd.Timestamp(os.environ["MOCK_START"]).to_pydatetime() if os.environ.get("MOCK_START") else datetime(2026, 1, 5, 6, 0, tzinfo=UTC)), seed=int(os.environ.get("MOCK_SEED", "7")))
        data = StreamAdapter(rest=None, mock=mock)
    elif C.MODE == "stream":
        rest = None
        try: rest = OTChartsAdapter()
        except Exception as e: log.warning("REST unavailable at startup (%s); stream-only", e)
        data = StreamAdapter(rest=rest, stop=threading.Event())   # own event: closing the stream on /poll must not stop the bot
    else: data = OTChartsAdapter()
    bot = Bot(data)
    def _shutdown(*_):
        STOP.set()   # Railway sends SIGTERM on restart/redeploy: release the stream slot right away
        try:
            if hasattr(bot.d, "close"): bot.d.close()
        except Exception as e: log.warning("stream close on SIGTERM: %s", e)
    signal.signal(signal.SIGTERM, _shutdown); signal.signal(signal.SIGINT, _shutdown)
    log.info("Started: source=%s book=%s pairs=%d telegram=%s", C.DATA_SOURCE, C.BROKER_BOOK, len(C.PAIRS), C.TELEGRAM_ENABLED)
    bot.tg.set_commands(bot.COMMANDS)
    if bot.tg.on:
        threading.Thread(target=bot.tg.poll_commands, args=(bot.command,), daemon=True).start()
        bot.tg.admin("🤖 Bot started")
    if hasattr(data, "res"):
        bot.tg.admin("🔎 Symbol resolution:\n" + "\n".join(describe(r) for r in data.res.values()))
    n = 0
    if mock is not None:   # simulated clock driven by mocked ticks
        t = mock.now().replace(second=0) + timedelta(minutes=1)
        for n in range(C.DRY_RUN_MINUTES or 600):
            mock.advance_to(t.replace(second=C.SCAN_SECOND))
            try:
                bot.tick()
                if bot.pending and not bot.pending["prechecked"]:
                    mock.advance_to(bot.pending["precheck_at"]); bot.run_precheck()
            except Exception: log.exception("tick failed")
            t += timedelta(minutes=1)
        STOP.set()
    while not STOP.is_set():
        if isinstance(data, ReplayAdapter):
            try:
                bot.tick()
                if bot.pending and not bot.pending["prechecked"]:
                    saved = data.clock; data.clock = bot.pending["precheck_at"]; bot.run_precheck(); data.clock = saved
            except Exception: log.exception("tick failed")
            data.advance(); n += 1
            if C.DRY_RUN_MINUTES and n >= C.DRY_RUN_MINUTES: break
            continue
        now = datetime.now(UTC)
        target = now.replace(second=C.SCAN_SECOND, microsecond=0)
        if target <= now: target += timedelta(minutes=1)
        pc = bot.pending["precheck_at"] if bot.pending and not bot.pending["prechecked"] else None
        if pc and now < pc < target:
            if STOP.wait((pc - now).total_seconds()): break
            try: bot.run_precheck()
            except Exception: log.exception("precheck failed (continuing)")
            continue
        if STOP.wait((target - now).total_seconds()): break
        try: bot.tick()
        except Exception: log.exception("tick failed (continuing)")
    if hasattr(bot.d, "close"): bot.d.close()
    bot.maybe_daily_summary(force=True); bot.save_stats()
    try: bot.save_shadow()
    except Exception as e: log.warning("shadow save: %s", e)
    log.info("Stopped cleanly. %s", bot.status().replace("\n", " | "))

if __name__ == "__main__":
    main()
