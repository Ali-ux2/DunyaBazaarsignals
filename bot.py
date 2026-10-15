"""M1 OTC signal bot (NO martingale). Long-lived worker: `python bot.py`.
Data: OTCharts REST (/v1/candles polled once per minute after candle close) or CSV replay.
Telegram: plain Bot API over requests. Logs to stdout."""
from __future__ import annotations
import json, logging, math, os, signal, sys, threading, time
from datetime import datetime, timedelta, timezone
import numpy as np, pandas as pd, requests
import config as C
from analyzer import bug_B1, bug_B2, bug_B3, demarker, rsi as _rsi
from analyzer import (ensemble, analyze_relaxed_ensemble, apply_level, strategies_line, bug_scan, bug_prediction,
                      STRATEGY_NAMES)
from analyzer import analyze, analyze_relaxed, compute, tracker, shadow_tracker, breakeven, precheck, precheck_flip_kind, switch_body_atr
from resolver import resolve, describe, Resolution
from telemetry import Telemetry
from scheduler import Scheduler
from history import HistGate, wilson_ub
from filters import Streaks, News, session_blackout, windows_utc
from analyzer import strat_trend_pullback, strat_range_reversion, strat_sr_rejection
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
    raw_hook = None
    cb_hook = None
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
    kb_log = []   # (method, payload) — also used by tests when Telegram is disabled
    def _kb_call(self, method, payload):
        self.kb_log.append((method, payload))
        if not self.on: log.info("[TG %s] %s", method, str(payload)[:200]); return None
        try:
            r = requests.post(f"{self.base}/{method}", json=payload, timeout=10)
            if not r.ok and "message is not modified" not in r.text: log.warning("%s failed %s: %s", method, r.status_code, r.text[:200])
            return r.json().get("result") if r.ok else None
        except requests.RequestException as e: log.warning("%s error: %s", method, e)
    def admin_kb(self, text, rows, edit_id=None):
        """Inline keyboard to the admin chat; rows = [[(label, callback_data), ...], ...]."""
        mk = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in row] for row in rows]}
        base = {"chat_id": C.TELEGRAM_ADMIN_ID, "text": text, "parse_mode": "HTML", "reply_markup": mk}
        if edit_id: return self._kb_call("editMessageText", dict(base, message_id=edit_id))
        return self._kb_call("sendMessage", base)
    def answer_cb(self, cb_id, text=None):
        self._kb_call("answerCallbackQuery", {"callback_query_id": cb_id, **({"text": text} if text else {})})
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
                r = requests.get(f"{self.base}/getUpdates", params={"timeout": 25, "offset": self.offset, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=35).json()
                for u in r.get("result", []):
                    self.offset = u["update_id"] + 1; m = u.get("message") or {}
                    cq = u.get("callback_query")
                    if cq:   # inline button: admin only
                        if str(cq.get("from", {}).get("id")) == str(C.TELEGRAM_ADMIN_ID) and self.cb_hook: self.cb_hook(cq)
                        else: self.answer_cb(cq.get("id"), "Not allowed")
                        continue
                    if str(m.get("from", {}).get("id")) == str(C.TELEGRAM_ADMIN_ID) or str(m.get("chat", {}).get("id")) == str(C.TELEGRAM_ADMIN_ID):
                        if self.raw_hook and self.raw_hook(m): continue      # /sched (case kept) + sticker/photo/forward capture
                        handler((m.get("text") or "").strip().split("@")[0].lower())
            except Exception as e: log.warning("getUpdates: %s", e); STOP.wait(5)

# ======================= BOT =======================
def parse_sessions(spec):
    out = []
    for part in filter(None, (s.strip() for s in spec.split(","))):
        a, b = part.split("-"); out.append((int(a[:2])*60+int(a[3:]), int(b[:2])*60+int(b[3:])))
    return out

BUG_STRAT_NAMES = {"B1": "5s Gap Rejection", "B2": "1m Momentum Gap to S/R", "B3": "3-Point Indicator Desync",
                   "B4p": "Frozen Candle (feed pause)", "B4f": "Frozen Candle (flat ticks)",
                   "B5s10": "5-Consecutive Exhaustion 10s", "B5s15": "5-Consecutive Exhaustion 15s", "B5m": "5-Consecutive Exhaustion 1m",
                   "B6_14": "Bollinger Out-of-Bounds BB14", "B6_20": "Bollinger Out-of-Bounds BB20",
                   "E": "ZigZag+DeMarker (bug-style)", "F": "Double Oscillator (bug-style)"}
import patterns as PAT
BUG_STRAT_NAMES.update({k: f"Pattern {v}" for k, v in PAT.NAMES.items()})
def _on(k):
    if k.startswith("P"): return C.PATTERNS_ON
    return any(k == s or (k.startswith(s) and s.startswith("B")) for s in C.BUG_STRATS)
import analyzer as _an
def _d(fn):
    def det(x, i):
        r = fn(x, i); return r[0] if r else None
    return det
def _ef(fn):
    def det(x, i):
        ensure_cols(x); r = fn(x, i); return r[0] if r else None
    return det
# replayable candle detectors per strategy (same logic as live). D (full voting engine) is too slow to replay over 30 days and is not used.
HIST_DET = None
def hist_detectors():
    global HIST_DET
    if HIST_DET is None:
        HIST_DET = {"A": _d(strat_trend_pullback), "B": _d(strat_range_reversion), "C": _d(strat_sr_rejection),
                    "E": _ef(strat_E), "F": _ef(strat_F), "B2": _ef(bug_B2), "B3": _ef(bug_B3), "B5m": _d(bug_B5_1m),
                    "B6_14": _d(lambda x, i: bug_B6(x, i, 14, 2.0)), "B6_20": _d(lambda x, i: bug_B6(x, i, 20, 2.0))}
        HIST_DET.update({k: PAT.detector(k) for k in PAT.NAMES if k != "P11"})   # P11 needs 30s bars: no candle history
    return HIST_DET
from bugstrats import sub_bars, double_confirm, strat_E, strat_F, bug_B4, bug_B5_sub, bug_B5_1m, bug_B6, ensure_cols, lb_score
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
        self.tel = Telemetry(); self._attach_tel(); self.hist = HistGate(); self.hist_day = {'filtered': 0, 'passed': 0}; self.load_hist_mode(); self.tier_sent = []; self.tier_day = {}; self.load_tiers()
        self.dc_day = {}; self._dc_note = {}; self.fill_pool = []; self.fill_pair_last = {}; self.p_hist_day = {}; self.streaks = Streaks(); self.news = News(); self.blocked_min = []; self._blk = None; self._news_seen = set(); self.load_filters()
        self.load_day()   # after all per-day counters exist
        self.sched = Scheduler(lambda t: self.tg.admin(t), lambda: self.d.now()); self.tg.raw_hook = self.sched_hook; self.tg.cb_hook = self.on_callback; self._bug_first = None
        self.load_level(); self.load_strat(); self.load_bugs(); self.load_lb()

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
        self.save_day()
    def save_day(self):
        """Today's counters (UTC day). Reloaded at startup only if the UTC day has not rolled over."""
        try:
            with open(C.DAY_STATE_FILE + ".tmp", "w") as f:
                json.dump({"date": str(self.day_date), "day": self.day, "kind_day": self.kind_day, "prechecks_day": self.prechecks_day,
                           "switches_day": self.switches_day, "kept_day": self.kept_day, "ladder": self.ladder,
                           "active_log": self.active_log[-240:], "hist_day": self.hist_day, "tier_day": self.tier_day, "tier_sent": self.tier_sent, "dc_day": self.dc_day}, f, default=str)
            os.replace(C.DAY_STATE_FILE + ".tmp", C.DAY_STATE_FILE)
        except Exception as e: log.warning("day state save: %s", e)
    def load_day(self):
        try:
            with open(C.DAY_STATE_FILE) as f: j = json.load(f)
        except FileNotFoundError: return
        except Exception as e: log.warning("day_state.json unreadable (%s)", e); return
        if j.get("date", "") > str(self.day_date): return      # clock went backwards / foreign file: ignore
        if j.get("date") != str(self.day_date):
            # saved on an earlier UTC day: keep it as that day's data so the normal rollover sends its summary, then resets
            try: self.day_date = datetime.strptime(j["date"], "%Y-%m-%d").date()
            except Exception: return
        for p, v in j.get("day", {}).items(): self.day[p] = list(v)
        self.kind_day = j.get("kind_day", self.kind_day); self.ladder = j.get("ladder", {}); self.hist_day = j.get("hist_day", self.hist_day); self.tier_day = j.get("tier_day", {}); self.tier_sent = j.get("tier_sent", []); self.dc_day = j.get("dc_day", {})
        self.prechecks_day, self.switches_day, self.kept_day = j.get("prechecks_day", 0), j.get("switches_day", 0), j.get("kept_day", 0)
        log.info("Restored today's stats (%s) after restart", j.get("date"))
    # ---- /reset (admin only; two-step confirm within 60 s; backup first) ----
    RESET_SCOPES = {
        "today": "today's W/L, kinds, prechecks, ladders, strategy/shadow day stats, public-session counters",
        "stats": "ALL win/loss stats: per-pair trade counts (30-trade eligibility), recent win rates, shadow, strategy totals, leaderboard weights/mutes, today's stats",
        "bugs": "bug-watcher patterns, samples, forward W/L, open bug signals",
        "telemetry": "telemetry counters and telemetry CSV files",
        "all": "everything above + bench/compound state (keeps channel, mode, level, schedules, bug-signal mode, public-session settings)"}
    def reset_cmd(self, args):
        a = [x.lower() for x in args]
        if not a or (a[0] not in self.RESET_SCOPES and a[0] != "confirm"):
            self.tg.admin("🧹 /reset <scope> then /reset confirm within 60 s. A backup is written to STATE_DIR/backups/ first.\n"
                          + "\n".join(f"  {k}: {v}" for k, v in self.RESET_SCOPES.items())); return
        now = time.time()
        if a[0] != "confirm":
            self._reset_want = (a[0], now)
            self.tg.admin(f"⚠️ Confirm reset of [{a[0]}]: {self.RESET_SCOPES[a[0]]}.\nSend /reset confirm within 60 s (anything else cancels nothing; it just expires)."); return
        want = getattr(self, "_reset_want", None); self._reset_want = None
        if not want or now - want[1] > 60:
            self.tg.admin("⌛ No pending reset (or it expired after 60 s). Start again with /reset <scope>."); return
        self.do_reset(want[0])
    def _reset_files(self, scope):
        f = {"today": [C.DAY_STATE_FILE], "stats": [C.STATS_FILE, C.SHADOW_STATS_FILE, C.STRATEGY_STATS_FILE, os.path.join(C.STATE_DIR, "leaderboard.json"), C.DAY_STATE_FILE],
             "bugs": [C.BUG_FILE], "telemetry": [os.path.join(C.STATE_DIR, p) for p in os.listdir(C.STATE_DIR) if p.startswith("telemetry_") and p.endswith(".csv")]}
        if scope == "all": return sorted(set(sum(f.values(), [])) | {C.BENCH_FILE})
        return f[scope]
    def do_reset(self, scope):
        import shutil
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"); bdir = os.path.join(C.STATE_DIR, "backups"); os.makedirs(bdir, exist_ok=True)
        files = [p for p in self._reset_files(scope) if os.path.exists(p)]; backup = {"scope": scope, "at": ts, "files": {}}
        pubpath = C.PUBLIC_CAP_FILE if scope in ("today", "stats", "all") and os.path.exists(C.PUBLIC_CAP_FILE) else None
        for p in files + ([pubpath] if pubpath else []):
            if p.endswith(".csv"): shutil.copy2(p, os.path.join(bdir, f"{ts}_{os.path.basename(p)}")); backup["files"][os.path.basename(p)] = "copied"
            else:
                try: backup["files"][os.path.basename(p)] = json.load(open(p))
                except Exception: backup["files"][os.path.basename(p)] = open(p).read()
        bpath = os.path.join(bdir, f"reset_{scope}_{ts}.json")
        with open(bpath, "w") as f: json.dump(backup, f, default=str)
        for p in files: os.remove(p)
        done = []
        if scope in ("today", "stats", "all"):
            self.day = {p: [0, 0, 0] for p in self.all_pairs}; self.kind_day = {"confirmed": [0, 0], "switched": [0, 0]}; self.hist_day = {"filtered": 0, "passed": 0}; self.tier_day = {}
            self.prechecks_day = self.switches_day = self.kept_day = 0; self.ladder = {}; self.strat_day = {}; self.shadow_day = {}
            p = self.pubcap(); p.update(w=0, l=0, n=0, closed=None, start_sent=False); self._pc_save(); self.save_day()
            done.append("today's stats + public-session counters")
        if scope in ("stats", "all"):
            tracker.reset(); shadow_tracker.reset(); self.shadow_payout = {}; self.strat_total = {}
            _an.STRAT_WEIGHTS.clear(); _an.STRAT_MUTED.clear(); self.lb_live = False
            self.tier_total = {"1": [0, 0], "2": [0, 0]}; self.save_tiers()
            self.save_stats(); self.save_strat(); self.save_shadow(); done.append("all W/L, trade counts, strategies, leaderboard")
        if scope in ("bugs", "all"):
            self.bug_sigs = []; self.load_bugs(); done.append(f"bug watcher ({len(self.bugs)} built-in strategies re-created at 0W/0L)")
        if scope in ("telemetry", "all"): self.tel = Telemetry(); self._attach_tel(); done.append("telemetry")
        if scope == "all":
            self.rotated_out = set(); self.lost_ladders = {}; self.compound_active = list(C.COMPOUND_PAIRS); self.save_bench(); done.append("bench/compound")
        self.tg.admin(f"🧹 Reset [{scope}] done: {'; '.join(done)}.\nFiles cleared: {', '.join(os.path.basename(p) for p in files) or 'none on disk'}"
                      f"\nBackup: backups/{os.path.basename(bpath)}")
        return bpath
    def persistence_check(self):
        """Loud admin warning if STATE_DIR looks non-persistent (unset/default, or no marker from a previous run)."""
        d = os.path.abspath(C.STATE_DIR); unset = not os.environ.get("STATE_DIR") or C.STATE_DIR in (".", "", "./")
        first = not os.path.exists(C.STATE_MARKER)
        try:
            if first:
                with open(C.STATE_MARKER, "w") as f: f.write(datetime.now(UTC).isoformat())
        except Exception as e: log.warning("state marker: %s", e)
        if unset or first:
            why = "STATE_DIR is unset/default" if unset else f"no marker from a previous run in {d} (first deploy, or the volume was not kept)"
            self.tg.admin(f"⚠️ STATE_DIR not persistent: stats will reset on every deploy ({why}). Add a Railway volume at /data and set STATE_DIR=/data")
            return False
        return True

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
    # ---- PUBLIC session (opens PUBLIC_SESSION_START local; closes at net +target or a safety stop; VIP otherwise) ----
    def pubcap(self):
        now = C.local(self.d.now()); h, m = map(int, (getattr(self, "_pc", None) or {}).get("start", C.PUBLIC_SESSION_START).split(":"))
        d = str(now.date()); self._pc_before = (now.hour, now.minute) < (h, m)   # session = start time → end of the local day
        if getattr(self, "_pc", None) is None:
            self._pc = {"date": d, "w": 0, "l": 0, "n": 0, "target": C.PUBLIC_NET_TARGET, "max": C.PUBLIC_MAX_SIGNALS,
                        "maxloss": C.PUBLIC_MAX_NET_LOSS, "start": C.PUBLIC_SESSION_START, "closed": None}
            try:
                with open(C.PUBLIC_CAP_FILE) as f: j = json.load(f)
                if "n" in j: self._pc.update(j)
            except Exception: pass
        if self._pc["date"] != d: self._pc.update(date=d, w=0, l=0, n=0, closed=None, start_sent=False); self._pc_save()
        return self._pc
    def _pc_save(self):
        try:
            with open(C.PUBLIC_CAP_FILE + ".tmp", "w") as f: json.dump(self._pc, f)
            os.replace(C.PUBLIC_CAP_FILE + ".tmp", C.PUBLIC_CAP_FILE)
        except Exception as e: log.warning("public cap save: %s", e)
    def public_open(self):
        p = self.pubcap()
        if p["target"] <= 0 and p["max"] <= 0: return True                     # cap fully off
        return not self._pc_before and not p["closed"] and (p["max"] <= 0 or p["n"] < p["max"])
    def public_capped(self): return not self.public_open()
    def cap_chats(self, chats):
        """NEW signals: drop PUBLIC outside the session; if that leaves nothing, fall back to VIP."""
        if self.public_open() or not any(c[0] == "public" for c in chats): return chats
        out = [c for c in chats if c[0] != "public"]
        if not out and C.VIP_CHANNEL_ID: out = [("vip", C.VIP_CHANNEL_ID)]
        return out
    def pubcap_posted(self, chats):
        if any(c[0] == "public" for c in (chats or [])): p = self.pubcap(); p["n"] += 1; self._pc_save()
    def pubcap_result(self, chats, res):
        """Every public result counts (losses never hidden); open trades finish even after the session closed."""
        if res not in ("WIN", "LOSS") or not any(c[0] == "public" for c in (chats or [])): return
        p = self.pubcap(); p["w" if res == "WIN" else "l"] += 1; net = p["w"] - p["l"]; self._pc_save()
        if p["closed"]: return
        why = None
        hit = (net >= p["target"]) if C.PUBLIC_CLOSE_RULE == "net" else (p["w"] >= p["target"] and net >= 1)
        if p["target"] > 0 and hit: why = "target"
        elif net <= p["maxloss"]: why = "maxloss"
        elif p["max"] > 0 and p["n"] >= p["max"] and not self._public_open_trades(): why = "max"
        if not why: return
        p["closed"] = why; self._pc_save(); pub = [c for c in chats if c[0] == "public"]
        f = dict(w=p["w"], l=p["l"], net=net, start=p["start"])
        if why == "target":
            self.tg.admin(f"🎯 PUBLIC session closed ({C.PUBLIC_CLOSE_RULE} rule): net +{net} ({p['w']}W/{p['l']}L, {p['n']} signals). Target reached. VIP only until {p['start']} {C.TZ_LABEL} tomorrow.")
            n = self.sched.fire("on_daily_target") + self.sched.fire("on_public_session_close")
            if n == 0 and C.PUBLIC_TARGET_MSG: self.tg.group(C.PUBLIC_TARGET_TEXT.format(**f), chats=pub)
        else:
            self.tg.admin(f"🛡 PUBLIC safety stop ({'net loss ' + str(net) + ' ≤ ' + str(p['maxloss']) if why == 'maxloss' else str(p['n']) + ' signals = max ' + str(p['max'])}): "
                          f"{p['w']}W/{p['l']}L. This is a protection so the public channel doesn't keep trading on a bad day; target +{p['target']} not reached. "
                          f"VIP only until {p['start']} {C.TZ_LABEL} tomorrow.")
            if self.sched.fire("on_public_session_close") == 0 and C.PUBLIC_TARGET_MSG: self.tg.group(C.PUBLIC_STOP_TEXT.format(**f), chats=pub)
    def _public_open_trades(self):
        t = self.pending
        return bool((t and any(c[0] == "public" for c in t.get("chats", []))) or
                    any(g["group"] and any(c[0] == "public" for c in g.get("chats", [])) for g in getattr(self, "bug_sigs", [])))
    def pubcap_line(self):
        p = self.pubcap(); net = p["w"] - p["l"]
        if p["target"] <= 0 and p["max"] <= 0: return "Public session: no cap"
        st = {"target": "CLOSED (target reached)", "maxloss": "CLOSED (safety: max net loss)", "max": "CLOSED (safety: max signals)"}.get(p["closed"])
        if not st: st = "OPEN" if self.public_open() else ("not started yet (VIP only)" if self._pc_before else "max signals used, waiting for results")
        return (f"Public session {p['date']} from {p['start']} {C.TZ_LABEL}: {st} · {p['w']}W/{p['l']}L net {net:+d} (target +{p['target']}, "
                f"stop at {p['maxloss']}) · signals {p['n']}/{p['max']}")
    def publiccap_cmd(self, args):
        p = self.pubcap()
        try:
            if len(args) >= 2 and args[0] in ("target", "max", "maxloss"):
                p[args[0]] = int(args[1])
            elif len(args) >= 2 and args[0] == "start":
                h, m = map(int, args[1].split(":")); assert 0 <= h < 24 and 0 <= m < 60; p["start"] = f"{h:02d}:{m:02d}"
            elif args: raise ValueError
        except Exception:
            self.tg.admin("Use /publiccap · /publiccap target N · /publiccap max N · /publiccap maxloss -N · /publiccap start HH:MM"); return
        self._pc_save(); self.tg.admin("🎯 " + self.pubcap_line())
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
        if not self.dc_ok("shadow", pair, s["signal"], x): return
        entry = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        t = {"pair": pair, "signal": s["signal"], "entry_at": entry, "settle_at": entry + timedelta(minutes=C.EXPIRY_MIN),
             "payout": s["payout"], "confidence": s["confidence"], "regime": s["regime"]}
        self.shadow_open.append(t); self.shadow_next[pair] = now + timedelta(minutes=C.SIGNAL_COOLDOWN_MIN)
        hok, hwhy, hline = self.hist_gate(pair, [k for k in s.get("agree", []) if k != "D"], now, s["payout"], "all")
        t["hist_ok"] = hok
        self.tg.admin(f"👻 Shadow (relaxed){'' if hok else ' 📚 filtered by history'}: {pair} {s['signal']} entry {C.hm(entry)} {C.TZ_LABEL} — not posted "
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
        return "\n".join([f"🎚 Level {self.level} — {names[self.level]} · {'AUTO (1–4)' if self.level_auto else 'MANUAL'} · shadow {self.shadow_label(self.level)}",
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
        self.tg.admin(f"🎚 Level {old} → {lv} ({why}){' · AUTO' if self.level_auto else ''}. Shadow now {self.shadow_label(lv)}."
                      + (" ⚠️ Level 5 is manual only: as shadow it went 8W/14L (36%) vs breakeven ~54%." if lv == 5 else ""))
    @staticmethod
    def shadow_label(lv): return "L4 + single B or C strategy" if int(lv) >= 4 else f"L{min(int(lv) + 1, 4)}"
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
        for k in STRATEGY_NAMES:
            w, l = src.get(k, [0, 0])
            out.append(f"  {k} {STRATEGY_NAMES[k]}: {w}W/{l}L" + (f" · {w/(w+l):.0%}" if w + l else ""))
        return out
    def leaderboard(self):
        """All ensemble members (A-F, scored on traded votes) + all bug strategies/patterns (forward W/L), by Wilson LB - breakeven."""
        live = [self._cached_meta(p)[0] for p in self.all_pairs]; live = [p for p in live if p]
        pay = sum(live) / len(live) if live else C.DEFAULT_PAYOUT; rows = []   # dynamic breakeven 1/(1+P) from live payouts
        for k, (w, l) in self.strat_total.items():
            if k in STRATEGY_NAMES: rows.append(("ens", k, f"{k} {STRATEGY_NAMES[k]}", w, l) + lb_score(w, l, pay))
        for k, b in self.bugs.items():
            w, l = b["fw"]; nm = f"{b['key']} {BUG_STRAT_NAMES.get(b['key'], b.get('desc', ''))}" if b.get("strategy") else f"{b['pair']} {b['key']}"
            rows.append(("bug", k, nm, w, l) + lb_score(w, l, pay))
        return sorted(rows, key=lambda r: (r[7] >= C.LB_MIN_N, r[5]), reverse=True)
    def lb_apply(self, announce=True):
        """Daily: boost/mute ensemble members, promote/mute bug strategies. Bounded weights 0.5-1.5; muted = still paper-tracked."""
        out = []
        for kind, k, nm, w, l, sc, wr, n, be in self.leaderboard():
            if n < C.LB_MIN_N: continue
            if kind == "ens":
                if n >= C.LB_MUTE_N and wr < be: _an.STRAT_MUTED.add(k); _an.STRAT_WEIGHTS[k] = C.LB_W_MIN; out.append(f"🔇 {nm} muted ({w}W/{l}L)")
                else:
                    _an.STRAT_MUTED.discard(k)
                    _an.STRAT_WEIGHTS[k] = round(max(C.LB_W_MIN, min(C.LB_W_MAX, 1.0 + 5 * sc)), 2)
                    if sc > C.LB_BOOST_PTS: out.append(f"⬆️ {nm} weight {_an.STRAT_WEIGHTS[k]}")
            else:
                b = self.bugs[k]
                if n >= C.LB_MUTE_N and wr < be and b.get("status") != "demoted": b["status"] = "demoted"; out.append(f"🔇 {nm} muted")
                elif sc > C.LB_BOOST_PTS and b.get("status") != "candidate": b["status"] = "candidate"; out.append(f"⭐ {nm} → candidate")
        self.save_bugs()
        try:
            with open(os.path.join(C.STATE_DIR, "leaderboard.json"), "w") as f:
                json.dump({"weights": _an.STRAT_WEIGHTS, "muted": sorted(_an.STRAT_MUTED), "live": self.lb_live}, f)
        except Exception: pass
        if announce and out: self.tg.admin("🏆 Leaderboard review:\n" + "\n".join(out))
        return out
    def load_lb(self):
        try:
            with open(os.path.join(C.STATE_DIR, "leaderboard.json")) as f: j = json.load(f)
            _an.STRAT_WEIGHTS.clear(); _an.STRAT_WEIGHTS.update(j.get("weights", {})); _an.STRAT_MUTED.clear(); _an.STRAT_MUTED.update(j.get("muted", []))
            self.lb_live = bool(j.get("live"))
        except Exception: self.lb_live = False
    def best_set(self):
        """Keys allowed to post to the group: Wilson LB ≥ breakeven + 3 pts with ≥ 20 forward trades."""
        return {k for kind, k, nm, w, l, sc, wr, n, be in self.leaderboard() if n >= C.LB_MIN_N and sc >= C.LB_BOOST_PTS}
    def best_live(self):
        b = self.best_set()
        if b and not self.lb_live:
            self.lb_live = True; self.tg.admin(f"🏆 Best-only group filter is LIVE: {', '.join(sorted(b))} qualified. Only best strategies post to the group now.")
            self._save_lb_flag()
        return b
    def _save_lb_flag(self):
        try:
            p = os.path.join(C.STATE_DIR, "leaderboard.json"); j = {}
            if os.path.exists(p): j = json.load(open(p))
            j["live"] = self.lb_live; json.dump(j, open(p, "w"))
        except Exception: pass
    def group_allowed(self, s):
        """GROUP_FILTER=best: weighted vote of best-list members must agree with the signal (bootstrap: plain ensemble)."""
        if C.GROUP_FILTER != "best": return True, ""
        b = {k for k in self.best_live() if k in STRATEGY_NAMES}
        if not self.best_set():
            return (True, "") if C.BEST_BOOTSTRAP == "ensemble" else (False, "no strategy qualified yet (BEST_BOOTSTRAP=silent)")
        votes = s.get("strategies") or {}; w = lambda k: votes[k][1] * _an.STRAT_WEIGHTS.get(k, 1.0)
        pro = sum(w(k) for k in b if votes.get(k) and votes[k][0] == s["signal"])
        con = sum(w(k) for k in b if votes.get(k) and votes[k][0] != s["signal"])
        if pro > 0 and pro > con: return True, ""
        return False, f"no best-list member agrees (best: {', '.join(sorted(b)) or 'none in ensemble'})"
    def paper_ensemble(self, s, now):
        """Unposted ensemble signal: paper-tracked as '*:ENS' and each member's vote is scored, so members can still qualify."""
        self.bugs.setdefault("*:ENS", {"pair": "*", "key": "ENS", "desc": "Ensemble (not sent)", "rule": None, "strategy": True,
                                      "status": "watch", "rate": 0, "n": 0, "fw": [0, 0], "alerted": None})
        e = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        self.bug_sigs.append({"k": "*:ENS", "pair": s["pair"], "dir": s["signal"], "traded": s["signal"], "entry_at": e,
                              "settle_at": e + timedelta(minutes=C.EXPIRY_MIN), "kind": "next", "entry_price": None, "payout": s["payout"],
                              "silent": True, "group": False, "atr": s.get("atr"), "prechecked": True, "desc": "", "votes": s.get("strategies")})
    def best_text(self):
        rows = self.leaderboard()
        fmt = lambda r: f"  {r[2]}: {r[3]}W/{r[4]}L ({r[6]:.0%}, n={r[7]}) · LB−BE {r[5]*100:+.1f} pts" + (" · muted" if r[1] in _an.STRAT_MUTED else "")
        ranked = [r for r in rows if r[7] >= C.LB_MIN_N]; thin = [r for r in rows if 0 < r[7] < C.LB_MIN_N]
        out = [f"🏆 Best strategies (Wilson 90% lower bound − breakeven; ranked only with ≥{C.LB_MIN_N} forward trades)", "Top 5:"]
        out += [fmt(r) for r in ranked[:5]] or ["  none yet"]
        out += ["Bottom 3:"] + ([fmt(r) for r in ranked[-3:][::-1]] if len(ranked) > 5 else ["  —"])
        out.append(f"Collecting data ({len(thin)}): " + ", ".join(f"{r[2]} {r[7]}" for r in sorted(thin, key=lambda r: -r[7])[:8]))
        out.append("Weights: " + (", ".join(f"{k} {v}" for k, v in sorted(_an.STRAT_WEIGHTS.items())) or "all 1.0 (not enough data)")
                   + (f" · muted {sorted(_an.STRAT_MUTED)}" if _an.STRAT_MUTED else ""))
        allowed = sorted(self.best_set())
        out.append(f"Allowed to post to the group (GROUP_FILTER={C.GROUP_FILTER}): " + (", ".join(allowed) if allowed else
                   f"none yet → bootstrap {C.BEST_BOOTSTRAP} ({'ensemble at current level posts' if C.BEST_BOOTSTRAP == 'ensemble' else 'group silent'})"))
        out += self.tier_lines()
        out.append("⚠️ Forward samples are small; a positive score is not proof of an edge.")
        return "\n".join(out)
    def strat_report(self):
        return "\n".join(["🧩 Strategies (scored on their own vote whenever they voted on a traded signal)",
                          f"Group signal needs ≥{C.ENSEMBLE_MIN_AGREE} agreeing and none opposing with score ≥{C.ENSEMBLE_STRONG_OPP} (level {self.level})",
                          "Today:"] + self.strat_lines(True) + ["All time:"] + self.strat_lines(False))

    # ---- bug / glitch watcher (EXPERIMENTAL, admin only, paper-tracked) ----
    # ---- historical confirmation gate ----
    def load_hist_mode(self):
        try:
            with open(os.path.join(C.STATE_DIR, "history_gate.json")) as f: C.HIST_GATE = json.load(f).get("mode", C.HIST_GATE)
        except Exception: pass
    def hist_gate(self, pair, keys, now, payout, kind, force=False):
        """kind 'all' (strategies/ensemble/shadow) or 'bugs'. Returns (ok, why, card_line). Off → always ok (force: P patterns, never bypassed)."""
        if force: pass
        elif C.HIST_GATE == "off" or (C.HIST_GATE == "bugs" and kind != "bugs"): return True, "gate off", ""
        det = hist_detectors(); nd = [(k, det[k]) for k in keys if k in det]
        if not nd: return False, f"no replayable candle history for {','.join(keys)} (tick/D-only)", ""
        try: return self.hist.check(pair, nd, compute, pd.Timestamp(now), payout)
        except Exception as e: log.warning("hist gate %s: %s", pair, e); return False, f"history check error ({e})", ""
    # ---- new-signal filters: loss streak / session blackout / news ----
    def load_filters(self):
        try:
            j = json.load(open(os.path.join(C.STATE_DIR, "filters.json")))
            C.LOSS_STREAK_N, C.STREAK_COOLDOWN_MIN = j.get("n", C.LOSS_STREAK_N), j.get("min", C.STREAK_COOLDOWN_MIN)
            C.BLACKOUT_WINDOWS, C.BLACKOUT_ON, C.NEWS_FILTER = j.get("windows", C.BLACKOUT_WINDOWS), j.get("blackout", C.BLACKOUT_ON), j.get("news", C.NEWS_FILTER)
            C.DOUBLE_CONFIRM = j.get("dc", C.DOUBLE_CONFIRM); C.PATTERNS_ON = j.get("patterns", C.PATTERNS_ON)
            C.DAILY_STOP_NET, C.DAILY_STOP_RESUME = j.get("dstop", C.DAILY_STOP_NET), j.get("dstop_resume", C.DAILY_STOP_RESUME)
            C.DAILY_PROFIT_ALERT, C.DAILY_PROFIT_CLOSE = j.get("palert", C.DAILY_PROFIT_ALERT), j.get("pclose", C.DAILY_PROFIT_CLOSE)
        except Exception: pass
    def save_filters(self):
        try: json.dump({"n": C.LOSS_STREAK_N, "min": C.STREAK_COOLDOWN_MIN, "windows": C.BLACKOUT_WINDOWS, "blackout": C.BLACKOUT_ON, "news": C.NEWS_FILTER, "dc": C.DOUBLE_CONFIRM, "patterns": C.PATTERNS_ON, "dstop": C.DAILY_STOP_NET, "dstop_resume": C.DAILY_STOP_RESUME, "palert": C.DAILY_PROFIT_ALERT, "pclose": C.DAILY_PROFIT_CLOSE},
                       open(os.path.join(C.STATE_DIR, "filters.json"), "w"))
        except Exception as e: log.warning("filters save: %s", e)
    # Filter precedence (never clash; the first that applies decides):
    #  1 hard blocks: /pause → news (per pair) → session blackout → loss-streak cooldown → daily stop
    #  2 payout floor (77%, re-checked at send time)  3 history gate (mandatory for P patterns)
    #  4 double confirmation (Tier-2 fill: relaxed structure)  5 ensemble/level thresholds  6 Tier-2 fill (only if 1–3 pass and
    #  history not clearly negative)  7 the :57 never-skip line (CONFIRM / SWITCH ≥0.8 ATR) applies only after a card is sent.
    PRECEDENCE = ["pause", "news", "blackout", "cooldown", "daily stop", "payout floor", "history gate", "double-confirm",
                  "ensemble/level", "Tier-2 fill", ":57 never-skip (after send)"]
    def live_block(self, now):
        """Reason NEW live (public/VIP) signals are blocked for all pairs, or None. Order: blackout → cooldown → daily stop."""
        b = session_blackout(now)
        if b: return f"session blackout {C.hm(b[0])}–{C.hm(b[1])} {C.TZ_LABEL} ({b[2]})"
        if self.streaks.frozen("live", now): return f"loss-streak cooldown until {C.hm(datetime.fromisoformat(self.streaks.s['live']['until']))} {C.TZ_LABEL}"
        return self.dstop_block("live", now)
    @staticmethod
    def trading_day(now):
        """Trading day for the daily stops: resets at DAILY_STOP_RESUME (TRADING_DAY_START, 08:00 UTC+3)."""
        hh, mm = map(int, C.DAILY_STOP_RESUME.split(":"))
        return (now + timedelta(hours=3) - timedelta(hours=hh, minutes=mm)).date().isoformat()
    def _ds(self, kind, now):
        key = "dstop" if kind == "live" else "dstop_bug"; ds = self.streaks.s.get(key) or {}
        if ds.get("day") != self.trading_day(now): ds = {"day": self.trading_day(now), "w": 0, "l": 0, "alerted": False, "until": ds.get("until"), "why": ds.get("why")}
        self.streaks.s[key] = ds; return ds
    def dstop_block(self, kind, now):
        ds = self.streaks.s.get("dstop" if kind == "live" else "dstop_bug") or {}
        if ds.get("until") and now < datetime.fromisoformat(ds["until"]):
            return f"daily {ds.get('why', 'loss')} stop ({ds.get('w', 0)}W/{ds.get('l', 0)}L) until {C.hm(datetime.fromisoformat(ds['until']))} {C.TZ_LABEL}"
        return None
    def dstop_result(self, res, kind="live"):
        """Daily loss/profit stop on net trades (W − L, ties ignored) per trading day (08:00 → 08:00 UTC+3, TRADING_DAY_START).
        Loss: L − W ≥ DAILY_STOP_NET → stop. Profit: W − L ≥ DAILY_PROFIT_ALERT → one admin alert; ≥ DAILY_PROFIT_CLOSE → close.
        Separate counters for live (public/VIP) and the bug group. Temporary: lifts at the next trading-day start."""
        if res not in ("WIN", "LOSS"): return
        now = self.d.now(); ds = self._ds(kind, now); ds["w" if res == "WIN" else "l"] += 1; net = ds["w"] - ds["l"]
        lab = "public/VIP" if kind == "live" else "bug group"; wl = f"{ds['w']}W/{ds['l']}L"
        hh, mm = map(int, C.DAILY_STOP_RESUME.split(":"))
        until = datetime.fromisoformat(ds["day"]).replace(tzinfo=timezone.utc) + timedelta(days=1, hours=hh - 3, minutes=mm)
        active = ds.get("until") and now < datetime.fromisoformat(ds["until"])
        if C.DAILY_PROFIT_ALERT and net >= C.DAILY_PROFIT_ALERT and not ds.get("alerted"):
            ds["alerted"] = True; self.tg.admin(f"💰 You're +{net} today ({wl}, {lab}) — consider taking profits")
        if not active and C.DAILY_STOP_NET and -net >= C.DAILY_STOP_NET:
            ds["until"], ds["why"] = until.isoformat(), "loss"
            self.tg.admin(f"🛑 Daily stop ({lab}): {-net} net losses today ({wl}) → no {lab} signals until {C.hm(until)} {C.TZ_LABEL} tomorrow "
                          f"(open trades finish; scheduled posts and private tracking continue) · /dailystop to adjust")
        elif not active and C.DAILY_PROFIT_CLOSE and net >= C.DAILY_PROFIT_CLOSE:
            ds["until"], ds["why"] = until.isoformat(), "profit"
            self.tg.admin(f"🏁 Daily profit close ({lab}): +{net} today ({wl}) → {lab} signals closed until {C.hm(until)} {C.TZ_LABEL} tomorrow "
                          f"(scheduled posts and private tracking continue) · /dailystop to adjust")
            if kind == "live": self.public_close_for_day(ds)
        self.streaks.save()
    def public_close_for_day(self, ds):
        """Profit close: if the public session is open, close it with the normal closing post (nothing else to groups)."""
        p = self.pubcap()
        if p.get("closed") or getattr(self, "_pc_before", False): return
        p["closed"] = "profit"; self._pc_save()
        pub = [c for c in self.chats_for("both") if c[0] == "public"]
        f = dict(w=p["w"], l=p["l"], net=p["w"] - p["l"], start=p["start"])
        if self.sched.fire("on_public_session_close") == 0 and C.PUBLIC_TARGET_MSG and pub:
            self.tg.group((C.PUBLIC_TARGET_TEXT if p["w"] - p["l"] > 0 else C.PUBLIC_STOP_TEXT).format(**f), chats=pub)
    def dailystop_cmd(self, args):
        try:
            a = [x.lower() for x in args]
            if a and a[0] == "off": C.DAILY_STOP_NET = 0
            elif a and a[0] == "clear":
                for k in ("dstop", "dstop_bug"):
                    if (a[1:2] or ["all"])[0] in ("all", "live" if k == "dstop" else "bug"): self.streaks.s[k] = {}
                self.streaks.save()
            elif a and a[0] == "resume": C.DAILY_STOP_RESUME = datetime.strptime(a[1], "%H:%M").strftime("%H:%M")
            elif a and a[0] == "loss": C.DAILY_STOP_NET = max(0, int(a[1]))
            elif a and a[0] == "alert": C.DAILY_PROFIT_ALERT = max(0, int(a[1]))
            elif a and a[0] in ("close", "profit"): C.DAILY_PROFIT_CLOSE = max(0, int(a[1]))
            elif a: C.DAILY_STOP_NET = max(1, int(a[0]))
        except Exception: self.tg.admin("Use /dailystop · /dailystop loss 5 · /dailystop alert 15 · /dailystop close 20 · /dailystop off · /dailystop clear [live|bug] · /dailystop resume 08:00"); return
        self.save_filters(); now = self.d.now(); rows = []
        for k, lab in (("live", "public/VIP"), ("bug", "bug group")):
            ds = self._ds(k, now); b_ = self.dstop_block(k, now)
            rows.append(f"  {lab}: today {ds['w']}W/{ds['l']}L (net {ds['w'] - ds['l']:+d}) · " + (f"STOPPED — {b_}" if b_ else "active"))
        self.streaks.save()
        self.tg.admin(f"🛑 Daily stops (trading day resets {C.DAILY_STOP_RESUME} {C.TZ_LABEL}; ties ignored): loss {C.DAILY_STOP_NET or 'off'} net losses · "
                      f"profit alert +{C.DAILY_PROFIT_ALERT or 'off'} · profit close +{C.DAILY_PROFIT_CLOSE or 'off'}\n" + "\n".join(rows))
    def filters_cmd(self, args=None):
        now = self.d.now()
        on = lambda v: "on" if v else "off"
        self.tg.admin("🧭 Filter precedence (first match decides; they never clash):\n" + "\n".join(f"  {i+1}. {p}" for i, p in enumerate(self.PRECEDENCE))
                      + f"\nNow: pause {on(self.manual_pause)} · news {on(C.NEWS_FILTER)} · blackout {on(C.BLACKOUT_ON)} · cooldown {C.LOSS_STREAK_N}L/{C.STREAK_COOLDOWN_MIN}m"
                      f" · daily stop −{C.DAILY_STOP_NET or 'off'}/+{C.DAILY_PROFIT_CLOSE or 'off'} · payout ≥{C.MIN_PAYOUT:.0%} · history {C.HIST_GATE} (P: always) · DC {on(C.DOUBLE_CONFIRM)}"
                      f" · level {self.level} · fill {on(C.FILL_MIN_HOUR)} · :57 switch ≥{C.SWITCH_MIN_BODY_ATR} ATR"
                      + (f"\nBlocking now: {self.live_block(now)}" if self.live_block(now) else ""))
    def pair_news(self, pair, now):
        e = self.news.blocking(pair, now)
        return f"news blackout: {e['cur']} {e['title']} at {C.hm(datetime.fromisoformat(e['t']))} {C.TZ_LABEL}" if e else None
    def streak_result(self, kind, res):
        now = self.d.now(); until = self.streaks.result(kind, res, now)
        if until:
            self.tg.admin(f"❄️ {C.LOSS_STREAK_N} {'bug-group ' if kind == 'bug' else ''}losses in a row, paused until {C.hm(until)} {C.TZ_LABEL} "
                          f"({'bug group' if kind == 'bug' else 'public/VIP'} only; open trades finish; candidates still tracked here)")
    def filters_tick(self, now):
        """Start/end notes (admin only), news refresh, blocked-minute log for the hourly quota."""
        for k, lab in (("live", "public/VIP"), ("bug", "bug group")):
            if self.streaks.expired(k, now): self.tg.admin(f"✅ Loss-streak cooldown over ({lab}): signals resume")
        b = session_blackout(now); key = b[0].isoformat() if b else None
        if key != self._blk:
            if b: self.tg.admin(f"🌗 Session blackout {C.hm(b[0])}–{C.hm(b[1])} {C.TZ_LABEL} ({b[2]}): no new signals")
            elif self._blk: self.tg.admin("🌗 Session blackout over: signals resume")
            self._blk = key
        try: self.news.refresh(self.tg.admin)
        except Exception as e: log.warning("news: %s", e)
        for e in self.news.upcoming(now, hours=0.3):
            t = datetime.fromisoformat(e["t"]); k = e["t"] + e["cur"]
            if t - timedelta(minutes=C.NEWS_BLACKOUT_MIN) <= now and k not in self._news_seen:
                self._news_seen.add(k); n = sum(1 for p in self.all_pairs if e["cur"] in p)
                self.tg.admin(f"📰 News blackout {e['cur']} '{e['title']}' {C.hm(t - timedelta(minutes=C.NEWS_BLACKOUT_MIN))}–{C.hm(t + timedelta(minutes=C.NEWS_BLACKOUT_MIN))} {C.TZ_LABEL}: {n} pairs blocked")
            if now >= t + timedelta(minutes=C.NEWS_BLACKOUT_MIN) and k in self._news_seen and k + "end" not in self._news_seen:
                self._news_seen.add(k + "end"); self.tg.admin(f"📰 News blackout {e['cur']} over")
        m = now.replace(second=0, microsecond=0)
        if self.live_block(now): self.blocked_min = [t for t in self.blocked_min if t > m - timedelta(hours=2)] + [m]
    def cooldown_cmd(self, args):
        try:
            if len(args) >= 2 and args[0] == "n": C.LOSS_STREAK_N = max(1, int(args[1]))
            elif len(args) >= 2 and args[0] == "min": C.STREAK_COOLDOWN_MIN = max(1, int(args[1]))
            elif len(args) >= 2 and args[0] == "clear" and args[1] in ("live", "bug"): self.streaks.s[args[1]] = {"n": 0, "until": None}; self.streaks.save()
            elif args: raise ValueError
        except Exception: self.tg.admin("Use /cooldown · /cooldown n 3 · /cooldown min 30 · /cooldown clear live|bug"); return
        self.save_filters(); now = self.d.now()
        f = lambda k: (f"FROZEN until {C.hm(datetime.fromisoformat(self.streaks.s[k]['until']))}" if self.streaks.frozen(k, now) else "active") + f", streak {self.streaks.s[k]['n']}"
        self.tg.admin(f"❄️ Loss-streak cooldown: {C.LOSS_STREAK_N} losses → {C.STREAK_COOLDOWN_MIN} min · public/VIP {f('live')} · bug group {f('bug')}")
    def blackout_cmd(self, args):
        if args and args[0] in ("on", "off"): C.BLACKOUT_ON = args[0] == "on"
        elif len(args) >= 2 and args[0] == "set":
            try: windows_utc(args[1], self.d.now().date()); C.BLACKOUT_WINDOWS = args[1]
            except Exception as e: self.tg.admin(f"⚠️ Bad windows ({e}). Format: Europe/London@07:45-08:30,America/New_York@09:15-09:45,UTC@23:45-00:15"); return
        self.save_filters(); now = self.d.now()
        ws = sorted((s, e, tz) for s, e, tz in windows_utc(C.BLACKOUT_WINDOWS, now.date()) if s.date() == now.date() or e.date() == now.date())
        self.tg.admin(f"🌗 Session blackouts {'ON' if C.BLACKOUT_ON else 'OFF'} (DST-aware) today, {C.TZ_LABEL}: "
                      + ", ".join(f"{C.hm(s)}–{C.hm(e)} ({tz.split('/')[-1]})" for s, e, tz in ws)
                      + f"\nSpec: {C.BLACKOUT_WINDOWS}\n/blackout on|off · /blackout set <spec>")
    def news_cmd(self, args):
        if args and args[0] in ("on", "off"): C.NEWS_FILTER = args[0] == "on"; self.save_filters()
        if args and args[0] == "refresh": self.news.refresh(self.tg.admin, force=True)
        now = self.d.now(); up = self.news.upcoming(now, 24)
        rows = [f"📰 News filter {'ON' if C.NEWS_FILTER else 'OFF'} · ±{C.NEWS_BLACKOUT_MIN} min around high-impact events · cache {len(self.news.events)} events"]
        for e in up[:15]:
            t = datetime.fromisoformat(e["t"]); n = sum(1 for p in self.all_pairs if e["cur"] in p)
            rows.append(f"  {C.hm(t - timedelta(minutes=C.NEWS_BLACKOUT_MIN))}–{C.hm(t + timedelta(minutes=C.NEWS_BLACKOUT_MIN))} {e['cur']} {e['title']} → {n} pairs")
        if not up: rows.append("  no high-impact events in the next 24 h")
        self.tg.admin("\n".join(rows))
    def dc_ok(self, src, pair, side, x, fill=False):
        """Double confirmation (momentum AND structure in the signal direction). Drops are logged as 'single-confirmation'."""
        if not C.DOUBLE_CONFIRM or x is None or len(x) < 30: return True
        try: m, st_, txt = double_confirm(x, len(x) - 1, side, fill=fill)
        except Exception as e: log.warning("double confirm %s: %s", pair, e); return True
        self.dc_day["checked"] = self.dc_day.get("checked", 0) + 1
        if m and st_: return True
        if fill: return False   # fill candidates are a wider pool; their drops are not logged
        self.dc_day["dropped"] = self.dc_day.get("dropped", 0) + 1
        last = self._dc_note.get(pair); nowt = time.time()
        if not last or nowt - last > 300: self._dc_note[pair] = nowt; self.tg.admin(f"➖ single-confirmation, dropped: {src} {pair} {side} · {txt}")
        return False
    def patterns_cmd(self, args):
        if args and args[0] in ("on", "off"): C.PATTERNS_ON = args[0] == "on"; self.save_filters()
        be = breakeven(C.DEFAULT_PAYOUT)
        rows = [f"🕯 Candle patterns P: {'ON' if C.PATTERNS_ON else 'OFF'} · history check mandatory (never bypassed) · "
                f"admin only until ≥{C.P_LIVE_MIN} forward trades ≥ breakeven · no martingale · DC: pattern = structure (S) or momentum (M), other from indicators"]
        for k, nm in PAT.NAMES.items():
            b = self.bugs.get(f"*:{k}", {}); w, l = b.get("fw", [0, 0]); n = w + l
            st = "muted" if b.get("status") == "demoted" else ("proven → bug group" if n >= C.P_LIVE_MIN and w / n >= be else "admin only")
            rows.append(f"  {k} [{PAT.DC_ROLE[k]}] {nm} · fwd {w}W/{l}L" + (f" {w/n:.0%}" if n else "") + f" · {st}"
                        + (f" · {self.p_hist_day[k]} filtered by history today" if self.p_hist_day.get(k) else "") + (" · 2m expiry" if k in PAT.EXPIRY else ""))
        self.tg.admin("\n".join(rows))
    # ---- weekly report + self-tune ----
    def week_record(self):
        f = os.path.join(C.STATE_DIR, "week.json")
        try: wk = json.load(open(f))
        except Exception: wk = []
        w = sum(v[0] for v in self.day.values()); l = sum(v[1] for v in self.day.values())
        wk = [d for d in wk if d["date"] != str(self.day_date)] + [{"date": str(self.day_date), "w": w, "l": l, "tier": self.tier_day,
              "dc": self.dc_day, "hist": self.hist_day}]
        json.dump(wk[-35:], open(f, "w"))
    def weekly_report(self):
        try: wk = json.load(open(os.path.join(C.STATE_DIR, "week.json")))[-7:]
        except Exception: wk = []
        w = sum(d["w"] for d in wk); l = sum(d["l"] for d in wk); pay = C.DEFAULT_PAYOUT; be = breakeven(pay)
        lines = [f"📅 Weekly report ({wk[0]['date'] if wk else '—'} → {wk[-1]['date'] if wk else '—'})",
                 f"  Live: {w}W/{l}L" + (f" · WR {w/(w+l):.1%} vs breakeven {be:.1%} · expectancy {(w*pay-l)/(w+l):+.3f}/signal" if w + l else " · no trades")]
        for d in wk: lines.append(f"  {d['date']}: {d['w']}W/{d['l']}L · single-confirmation dropped {d.get('dc', {}).get('dropped', 0)}")
        top = [r for r in self.leaderboard() if r[7] >= C.LB_MIN_N][:5]
        lines += ["  " + t for t in self.tier_lines()]
        lines.append("  Top strategies: " + (", ".join(f"{r[1].split(':')[-1]} {r[6]:.0%} (n {r[7]})" for r in top) or "none with enough trades"))
        pp = [(k, self.bugs[f"*:{k}"]["fw"]) for k in PAT.NAMES if f"*:{k}" in self.bugs and sum(self.bugs[f"*:{k}"]["fw"])]
        lines.append("  Patterns: " + (", ".join(f"{k} {a}W/{b}L" for k, (a, b) in pp) or "no forward trades yet"))
        self.tg.admin("\n".join(lines))
    def weekly_cmd(self, args):
        if args and args[0] == "tune": self.weekly_tune(background=not isinstance(self.d, ReplayAdapter))
        else: self.weekly_report()
    def weekly_tune(self, background=True, rows=None):
        """Re-run the filter ablation on stored candles (live + cached), report, and auto-apply only SAFE changes
        (double-confirm on/off, level 3↔4) when the winner has positive expectancy with Wilson LB ≥ breakeven. Hard blocks never loosened."""
        def job():
            try:
                import ablation as AB
                rr = rows
                if rr is None:
                    rr = []
                    for p in self.all_pairs:
                        df = self.hist.store.get(p)
                        if df is not None and len(df) > 500: rr += AB.stage1(p, df.iloc[-7 * 1440:])
                if len(rr) < 1000: self.tg.admin("🔧 Weekly self-tune: not enough stored candles yet (need ≥1000 bars); nothing changed"); return
                pay = C.DEFAULT_PAYOUT; res = AB.table(rr, pay, [(pd.Timestamp(e["t"]), e["cur"]) for e in self.news.events])
                pr = AB.propose(res); msg = ["🔧 Weekly self-tune (ablation on stored candles; random/OTC data can't prove edge)", AB.fmt(res)]
                if not pr: msg.append("No config with enough trades: nothing changed.")
                else:
                    name, cfg, m, safe = pr; from history import wilson_lb as _lb
                    good = m["exp"] > 0 and _lb(round(m["wr"] * m["n"]), m["n"]) >= m["be"]
                    msg.append(f"Best: {name} · E {m['exp']:+.3f}/signal · {m['per_hour']:.1f}/h")
                    hard = [k for k in ("blackout", "news", "cooldown", "dailystop") if cfg.get(k) is False]
                    if hard: msg.append(f"Proposal touches hard blocks ({', '.join(hard)}): NOT applied (never loosened automatically).")
                    if good and C.WEEKLY_TUNE_APPLY:
                        ch = []
                        if "dc" in safe and safe["dc"] != C.DOUBLE_CONFIRM: C.DOUBLE_CONFIRM = safe["dc"]; ch.append(f"double-confirm {'on' if safe['dc'] else 'off'}")
                        lv = min(4, max(3, safe.get("level", 3)))
                        if lv != self.level and not self.level_auto: self.level_want = ("manual", lv); ch.append(f"level → {lv} (applied next minute)")
                        elif lv != self.level: ch.append(f"level {lv} suggested (auto-level is on, left to auto)")
                        self.save_filters(); msg.append("Auto-applied (safe bounds): " + (", ".join(ch) or "no change needed"))
                    else: msg.append("Not applied: winner is not clearly above breakeven. Proposal only — change with /confirm2, /level.")
                self.tg.admin("\n".join(msg))
            except Exception as e: self.tg.admin(f"🔧 Weekly self-tune failed: {e}")
        if background: threading.Thread(target=job, daemon=True).start()
        else: job()
    def dc_cmd(self, args):
        if args and args[0] in ("on", "off"): C.DOUBLE_CONFIRM = args[0] == "on"; self.save_filters()
        self.tg.admin(f"🔗 Double confirmation {'ON' if C.DOUBLE_CONFIRM else 'OFF'}: momentum trigger AND structure (BB / swing S/R / ZigZag pivot / rejection wick) "
                      f"· today {self.dc_day.get('dropped', 0)} dropped of {self.dc_day.get('checked', 0)} checked")
    def hour_sent(self, now):
        h = now.replace(minute=0, second=0, microsecond=0)
        return sum(1 for t in self.tier_sent if pd.Timestamp(t) >= h)
    def fill_due(self, now):
        """Behind the hourly minimum from :FILL_START_MIN, scaled to elapsed time (ceil(MIN × minute/60))."""
        if not C.FILL_MIN_HOUR or now.minute < C.FILL_START_MIN or self.live_block(now): return False
        h = now.replace(minute=0, second=0, microsecond=0); blocked = sum(1 for t in self.blocked_min if t >= h)
        # paced: one per FILL_SLOT_MIN slot; Tier 1 has the first FILL_SLOT_WAIT minutes of each slot; missed slots may catch up
        slot0 = h + timedelta(minutes=(now.minute // C.FILL_SLOT_MIN) * C.FILL_SLOT_MIN)
        if now < slot0 + timedelta(minutes=C.FILL_SLOT_WAIT) and any(pd.Timestamp(t) >= slot0 for t in self.tier_sent): return False
        if now < slot0 + timedelta(minutes=C.FILL_SLOT_WAIT): need = now.minute // C.FILL_SLOT_MIN
        else: need = now.minute // C.FILL_SLOT_MIN + 1
        need = min(need, math.ceil(C.MIN_SIGNALS_PER_HOUR * max(1, now.minute - blocked) / 60))   # never catch up for blocked minutes
        return self.hour_sent(now) < need
    def volume_due(self, now):
        """Tier 3: behind the 10-min pace toward VOLUME_TARGET…VOLUME_MAX_HOUR this hour (blocked minutes don't count)."""
        if not C.VOLUME_MODE or self.live_block(now): return False
        h = now.replace(minute=0, second=0, microsecond=0); blocked = sum(1 for t in self.blocked_min if t >= h)
        sent = self.hour_sent(now)
        if sent >= C.VOLUME_MAX_HOUR: return False
        slot0 = h + timedelta(minutes=(now.minute // C.VOLUME_SLOT_MIN) * C.VOLUME_SLOT_MIN)
        if any(pd.Timestamp(t) >= slot0 for t in self.tier_sent): return False          # max 1 per 10-min slot
        if now < slot0 + timedelta(minutes=min(C.FILL_SLOT_WAIT, C.VOLUME_SLOT_MIN - 1)): return False   # Tier 1/2 get the slot start
        need = min(C.VOLUME_MAX_HOUR, now.minute // C.VOLUME_SLOT_MIN + 1,
                   math.ceil(C.VOLUME_MAX_HOUR * max(1, now.minute - blocked) / 60))
        return sent < need
    def volume_cmd(self, args):
        if args and args[0] in ("on", "off"): C.VOLUME_MODE = args[0] == "on"; self.save_tiers()
        w, l = self.tier_total.get("3", [0, 0])
        self.tg.admin(f"📙 Volume mode (Tier 3): {'ON' if C.VOLUME_MODE else 'OFF'} · target {C.VOLUME_TARGET}/h, cap {C.VOLUME_MAX_HOUR}/h, ≈1 per {C.VOLUME_SLOT_MIN} min, "
                      f"max 1 per pair per {C.FILL_PAIR_GAP_MIN} min · level-4 candidates, fill structure, history only 'not clearly negative' · "
                      f"hard blocks always apply · Tier 3 so far {w}W/{l}L" + (f" {w/(w+l):.0%}" if w + l else ""))
    def load_tiers(self):
        self.tier_total = {"1": [0, 0], "2": [0, 0], "3": [0, 0]}; self.vol_warned = False
        try:
            j = json.load(open(C.TIERS_FILE)); self.tier_total.update(j.get("total", {})); C.FILL_MIN_HOUR = j.get("fill", C.FILL_MIN_HOUR)
            C.VOLUME_MODE = j.get("volume", C.VOLUME_MODE); self.vol_warned = j.get("vol_warned", False)
        except Exception: pass
    def save_tiers(self):
        try: json.dump({"total": self.tier_total, "fill": C.FILL_MIN_HOUR, "volume": C.VOLUME_MODE, "vol_warned": getattr(self, "vol_warned", False)}, open(C.TIERS_FILE, "w"))
        except Exception as e: log.warning("tiers save: %s", e)
    def tier_record(self, t, win):
        k = str(t.get("tier", 1))
        for d in (self.tier_total, self.tier_day): d.setdefault(k, [0, 0])[0 if win else 1] += 1
        if k == "3":   # safeguard: alert (never auto-disable) once Tier 3 is below breakeven after ≥ VOLUME_GUARD_N trades
            w, l = self.tier_total["3"]; be = breakeven(t.get("payout") or C.DEFAULT_PAYOUT)
            if w + l >= C.VOLUME_GUARD_N and w / (w + l) < be and not self.vol_warned:
                self.vol_warned = True
                self.tg.admin(f"⚠️ Tier 3 (volume) is below breakeven: {w}W/{l}L = {w/(w+l):.1%} < {be:.1%} after {w+l} live trades. "
                              f"Suggestion: turn volume mode off with /volume off (not disabled automatically).")
            elif w + l >= C.VOLUME_GUARD_N and w / (w + l) >= be: self.vol_warned = False
        self.save_tiers()
    def tier_lines(self):
        f = lambda d, k: (lambda w, l: f"{w}W/{l}L" + (f" {w/(w+l):.0%}" if w + l else ""))(*d.get(k, [0, 0]))
        return [f"Tier 1 ✅ history-confirmed: today {f(self.tier_day, '1')} · all {f(self.tier_total, '1')}",
                f"Tier 2 📘 standard (fill to {C.MIN_SIGNALS_PER_HOUR:g}/h, {'ON' if C.FILL_MIN_HOUR else 'OFF'}): today {f(self.tier_day, '2')} · all {f(self.tier_total, '2')}",
                f"Tier 3 📙 volume ({C.VOLUME_TARGET}–{C.VOLUME_MAX_HOUR}/h, {'ON' if C.VOLUME_MODE else 'OFF'}): today {f(self.tier_day, '3')} · all {f(self.tier_total, '3')}"]
    def fill_cmd(self, args):
        if args and args[0] in ("on", "off"): C.FILL_MIN_HOUR = args[0] == "on"; self.save_tiers()
        self.tg.admin(f"🔁 Tier-2 fill to {C.MIN_SIGNALS_PER_HOUR:g}/h: {'ON' if C.FILL_MIN_HOUR else 'OFF'} (from :{C.FILL_START_MIN}, never with clearly negative history)")
    def hist_count(self, ok):
        self.hist_day["passed" if ok else "filtered"] += 1
    def history_cmd(self, args):
        if args and args[0] in ("all", "bugs", "off"):
            C.HIST_GATE = args[0]
            try: json.dump({"mode": C.HIST_GATE}, open(os.path.join(C.STATE_DIR, "history_gate.json"), "w"))
            except Exception as e: log.warning("history mode save: %s", e)
        cov = ", ".join(f"{p} {self.hist.store.coverage_days(p):.1f}d" for p in self.all_pairs if self.hist.store.coverage_days(p) > 0) or "none yet"
        self.tg.admin(f"📚 History gate: {C.HIST_GATE.upper()} (all|bugs|off) · need 30d n≥{C.HIST_MIN_N}, Wilson LB ≥ BE+{C.HIST_MARGIN*100:.0f} pts "
                      f"(7d ×{C.HIST_W7}), 7d ≥ BE when n≥5, ≥{C.HIST_MIN_COVER_DAYS:g} days stored\n"
                      f"Today: {self.hist_day['passed']} passed · {self.hist_day['filtered']} filtered · backfill requests {self.hist.req_n}/{C.HIST_BACKFILL_MAX_REQ}\n"
                      f"Stored candles: {cov}\nThe gate wins over MIN_SIGNALS_PER_HOUR (it is never loosened to hit the hourly target).")
    def hist_feed(self, pair, df):
        try: self.hist.store.add(pair, df)
        except Exception as e: log.warning("candle store %s: %s", pair, e)
    def hist_housekeeping(self):
        fetch = getattr(self.d, "fetch_candles", None) or getattr(getattr(self.d, "rest", None), "fetch_candles", None)
        try: self.hist.backfill_step([p for p in self.all_pairs if p in C.PAIRS or True], fetch, str(self.day_date))
        except Exception as e: log.warning("backfill: %s", e)
        if C.HIST_GATE != "off":
            try: self.hist.warm([p for p in self.all_pairs if self.hist.store.coverage_days(p) > 0], hist_detectors(), compute,
                                pd.Timestamp(self.d.now()), C.HIST_WARM_BUDGET_S)
            except Exception as e: log.warning("history warm: %s", e)
        self.hist.store.flush()
    def sched_hook(self, m):
        t = (m.get("text") or "").strip()
        if t.lower().startswith("/sched"): self.tg.admin(self.sched.command(t)); return True
        if t.startswith("/"): return False
        r = self.sched.capture(m)
        if r: self.tg.admin(r)
        return bool(r)
    def session_start_check(self):
        p = self.pubcap()
        if p.get("start_sent") or not self.public_open() or (p["target"] <= 0 and p["max"] <= 0): return
        if not any(c[0] == "public" for c in (self.tg.chats or [])): return
        p["start_sent"] = True; self._pc_save()
        if self.sched.fire("on_public_session_start") == 0 and C.PUBLIC_TARGET_MSG:
            self.tg.group(C.PUBLIC_START_TEXT, chats=[c for c in self.tg.chats if c[0] == "public"])
        self.tg.admin(f"🟢 PUBLIC session started ({p['start']} {C.TZ_LABEL})")
    def _attach_tel(self):
        for src in (self.d, getattr(self, "poll_adapter", None)):
            if src is not None and hasattr(src, "books"): src.telemetry = self.tel if C.TELEMETRY else None
    def telemetry_cmd(self):
        self.tg.admin(self.tel.summary() if C.TELEMETRY else "📡 Telemetry is OFF (TELEMETRY=0).")
    def bug_ox(self, pair, x, now, payout, gaps):
        """(3) over-extension bins that pass the same Bonferroni test become bug patterns and fire through bug_fire."""
        if not C.TELEMETRY: return
        if not self.is_stream(): self.tel.on_bar_seen(pair, x.index[-1].timestamp(), time.time())
        self.tel.on_candles(pair, x)
        if gaps or payout is None: return
        sig = {k: (r, n) for p, k, r, n, _ in self.tel.ox_patterns() if p == pair}
        if not sig: return
        f = self.tel.features(x, len(x) - 1)
        for key, rev in self.tel.keys_for(f):
            if key not in sig: continue
            r, n = sig[key]; k = f"{pair}:ox:{key}"
            b = self.bugs.setdefault(k, {"pair": pair, "key": "ox:" + key, "desc": f"over-extension {key} → reversal", "rule": None, "ox": True,
                                         "status": "watch", "rate": r, "n": n, "fw": [0, 0], "alerted": None})
            b["rate"], b["n"] = round(r, 3), n
            self.bug_fire(k, pair, "CALL" if rev > 0 else "PUT", f"{key} → reversal {r:.0%} ({n} samples)", now, payout, x=x)
    def load_bugs(self):
        self.bugs = {}; self.bug_open = []; self.bug_scan_at = {}; self.bug_alerts = []
        self.bug_sigs = []; self.bug_last_pair = {}; self.bug_last_pat = {}; self.bug_mode = C.BUG_SIGNALS
        self.bug_day = {}
        try:
            with open(C.BUG_FILE) as f: j = json.load(f)
            self.bugs = j.get("patterns", {}); self.bug_alerts = j.get("alerts", [])
            if not C.BUG_TIME_PATTERNS:   # drop stored hour/minute colour-bias patterns (false-positive source)
                self.bugs = {k: v for k, v in self.bugs.items() if not (v.get("key", "").startswith(("hour", "min")) and not v.get("strategy"))}
        except FileNotFoundError: pass
        except Exception as e: log.warning("bug_watch.json unreadable (%s)", e)
        try:
            with open(C.BUG_SIGNALS_FILE) as f: _j = json.load(f)
            self.bug_mode = _j.get("mode", self.bug_mode)
            if self.bug_mode == "bug": self.bug_mode = "admin"
        except FileNotFoundError: pass
        except Exception as e: log.warning("bug_signals.json unreadable (%s)", e)
        for k, nm in BUG_STRAT_NAMES.items():   # named bug strategies are tracked like patterns (pair "*")
            self.bugs.setdefault(f"*:{k}", {"pair": "*", "key": k, "desc": nm, "rule": None, "strategy": True, "status": "watch",
                                             "rate": 0, "n": 0, "fw": [0, 0], "alerted": None})
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
    def bug_feed(self, pair, x, now, payout=None, gaps=0):
        if len(x) < max(C.MIN_BARS, 50): return
        # 1) live bug signals for detected patterns whose condition holds on the latest closed candle
        if gaps == 0 and payout is not None:
            for key, b in list(self.bugs.items()):
                if b["pair"] != pair or b.get("rule") is None: continue
                pred = bug_prediction(x, b["key"], tuple(b["rule"]))
                if pred is not None:
                    self.bug_fire(key, pair, "CALL" if pred > 0 else "PUT", self.bug_text(pair, b["key"], b["desc"], tuple(b["rule"]), b["rate"], b["n"])
                                  .replace("🐞 Bug watch (experimental): " + pair + " ", ""), now, payout, x=x)
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
    # ---- live bug signals (B-strategies + detected patterns): admin by default; /bugsignals routes them ----
    def bug_route_group(self, b, k=None):
        """PUBLIC/VIP routing (the dedicated bug group is separate)."""
        if C.GROUP_FILTER == "best":   # best-only: /bugsignals group/candidates/bug → only best-list keys reach public/VIP
            return self.bug_mode in ("group", "candidates") and k in self.best_live()
        return self.bug_mode == "group" or (self.bug_mode == "candidates" and b.get("status") == "candidate")
    def bug_fire(self, k, pair, d, desc, now, payout, kind="next", entry_price=None, x=None):
        """Fire (or silently paper-track) a bug signal. 77% floor; never on a pair with a pending group trade; cooldowns."""
        b = self.bugs.get(k)
        if b is None or payout is None or payout < C.MIN_PAYOUT: return None
        isP = b.get("key", "").startswith("P")
        if isP and x is not None and C.DOUBLE_CONFIRM:
            pok, ptxt = PAT.p_double_confirm(x, len(x) - 1, b["key"], d); self.dc_day["checked"] = self.dc_day.get("checked", 0) + 1
            if not pok:
                self.dc_day["dropped"] = self.dc_day.get("dropped", 0) + 1
                last = self._dc_note.get(pair)
                if not last or time.time() - last > 300: self._dc_note[pair] = time.time(); self.tg.admin(f"➖ single-confirmation, dropped: pattern {b['key']} {pair} {d} · {ptxt}")
                return None
        elif x is not None and not self.dc_ok(f"bug {k.split(':')[-1]}", pair, d, x): return None
        if self.pending and self.pending.get("pair") == pair: return None
        entry = now if kind == "now" else now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if any(o["k"] == k and o["pair"] == pair and o["entry_at"] == entry for o in self.bug_sigs): return None
        cool = (self.bug_last_pair.get(pair) and now - self.bug_last_pair[pair] < timedelta(minutes=C.BUG_PAIR_COOLDOWN_MIN)) or \
               (self.bug_last_pat.get(k) and now - self.bug_last_pat[k] < timedelta(minutes=C.BUG_SIGNAL_COOLDOWN_MIN))
        fw_n = sum(b["fw"]); unproven = not b.get("strategy") and (fw_n < C.BUG_LIVE_MIN_FW or b["fw"][0] / fw_n < breakeven(payout))
        silent = b.get("status") == "demoted" or bool(cool) or self.manual_pause or unproven or \
                 (self.bug_mode == "off" and not C.BUG_CHANNEL_ID)
        atr = float(x.atr.iloc[-1]) if x is not None and len(x) else None
        exp = PAT.EXPIRY.get(b.get("key"), C.EXPIRY_MIN) if isP else C.EXPIRY_MIN
        sig = {"k": k, "pair": pair, "dir": d, "traded": d, "entry_at": entry, "settle_at": entry + timedelta(minutes=exp), "expiry": exp,
               "kind": kind, "entry_price": entry_price, "payout": payout, "silent": silent, "group": False, "atr": atr,
               "prechecked": kind == "now", "desc": desc}
        if not silent and (C.HIST_GATE != "off" or isP):   # P patterns: history check is mandatory (no bypass)
            if b.get("strategy"): hok, hwhy, hline = self.hist_gate(pair, [b["key"]], now, payout, "bugs", force=isP)
            else:   # statistical patterns ARE history: use their scanned sample (30d window) with the same thresholds
                nn = int(b.get("n", 0)); hs = {"n30": nn, "w30": int(round(b.get("rate", 0) * nn)), "n7": 0, "w7": 0}
                hok, hwhy, hline = self.hist.decide(hs, payout, self.hist.store.coverage_days(pair))
            self.hist_count(hok); sig["hist_line"] = hline
            if not hok:
                silent = sig["silent"] = True
                if isP: self.p_hist_day[b["key"]] = self.p_hist_day.get(b["key"], 0) + 1   # counted, not spammed (shown in /patterns)
                else: self.tg.admin(f"📚 Filtered by history: 🐞 {pair} {d} {b['key']} · {hwhy}" + (f" · {hline}" if hline else "") + " · paper-tracked")
        self.bug_sigs.append(sig)
        if silent: return sig
        self.bug_last_pair[pair] = now; self.bug_last_pat[k] = now
        w, l = b["fw"]; strat = b.get("strategy")
        p_unproven = isP and (w + l < C.P_LIVE_MIN or w / (w + l) < breakeven(payout))   # admin-only until proven
        what = f"{b['key']} {b['desc']} — {desc}" if strat else desc
        when = f"ENTER NOW {C.hm(entry)}:{entry.second:02d}" if kind == "now" else f"entry {C.hm(entry)}"
        if self.bug_mode != "off": self.tg.admin(f"🐞 BUG SIGNAL {pair} {d} — {when} {C.TZ_LABEL} · {sig['expiry']} min · pattern: {what} (fwd {w}W/{l}L)"
                      f" · payout {payout:.0%} · {self.tel.lag_tag(pair)}" + (f" · 📚 {sig['hist_line']}" if sig.get("hist_line") else "") + (" · admin only until proven (≥%d fwd ≥ BE)" % C.P_LIVE_MIN if p_unproven else (" · → GROUP" if self.bug_route_group(b, k) else "")) + " · experimental, patterns can be false")
        blk = session_blackout(now) or self.pair_news(pair, now)
        if p_unproven: blk = blk or "pattern unproven (admin only)"
        pnow = self._cached_meta(pair)[0]
        if pnow is not None and pnow < C.MIN_PAYOUT: blk = blk or f"payout {pnow:.0%} at send time"
        main = self.cap_chats(list(self.tg.chats or self.chats_for(self.target))) if self.bug_route_group(b, k) and not blk and not self.streaks.frozen("live", now) and not self.dstop_block("live", now) else []
        bugch = [("bug", C.BUG_CHANNEL_ID)] if C.BUG_CHANNEL_ID and not blk and not self.streaks.frozen("bug", now) and not self.dstop_block("bug", now) else []   # bug group: always on (only temporary cooldown/blackout)
        if main or bugch:
            sig["group"] = True; sig["chats"] = main + bugch; lab = self.d.label(pair)
            self.pubcap_posted(main)
            if bugch and self._bug_first != self.day_date: self._bug_first = self.day_date; self.sched.fire("on_bug_signal_first_of_day")
            if kind == "now":
                self.tg.group(f"🐞 BUG SIGNAL · {display_name(lab)} {'🟢 CALL ⬆️' if d == 'CALL' else '🔴 PUT ⬇️'}\n⚡ ENTER NOW {C.hm(entry)}:{entry.second:02d} {C.TZ_LABEL} · ⌛ {sig['expiry']} min", chats=sig["chats"])
            else:
                p1, p2 = self.playbook(d)
                cap = "🐞 BUG SIGNAL\n" + signal_caption(lab, d, entry, sig['expiry'], (p1, p2) if C.PRECHECK_MODE in ("confirm", "direction") else None,
                                                  C.PRECHECK_SECOND, self.wait_line())
                self.tg.group(cap, chats=sig["chats"])
        return sig
    def bug_precheck_due(self):
        """:57 line for GROUP bug trades: same 0.8 ATR switch rule (never skip). Stats keep the pattern's own direction."""
        now = self.d.now()
        for g in self.bug_sigs:
            if g["prechecked"] or not g["group"] or now < g["entry_at"] - timedelta(seconds=60 - C.PRECHECK_SECOND): continue
            g["prechecked"] = True; name = display_name(self.d.label(g["pair"]))
            try: df, _, _ = self.d.candles(g["pair"], include_forming=True)
            except Exception: df = None
            bar = df.loc[df.index == g["entry_at"] - timedelta(minutes=1)] if df is not None else None
            opp = switch_body_atr({"signal": g["dir"], "atr": g["atr"] or 1e-12}, float(bar.open.iloc[0]), float(bar.close.iloc[0])) \
                if bar is not None and not bar.empty and g["atr"] else None
            if opp is not None and opp >= C.SWITCH_MIN_BODY_ATR:
                g["traded"] = "PUT" if g["dir"] == "CALL" else "CALL"
                self.tg.group(f"🔄 SWITCH 🐞 BUG {name} {g['traded']} — enter now", chats=g["chats"])
            else: self.tg.group(f"✅ CONFIRMED 🐞 BUG {name} {g['dir']} — enter now", chats=g["chats"])
            self.tg.admin(f"🐞 Bug :57 {g['pair']}: opposite candle {max(opp or 0, 0):.2f} ATR → {'SWITCH ' + g['traded'] if g['traded'] != g['dir'] else 'kept ' + g['dir']}")
    def _tick_price(self, pair, t_end):
        b = getattr(self.d, "books", {}).get(pair)
        if b is None or not getattr(b, "ticks", None): return None, None
        ts = t_end.timestamp(); last = None; after = False
        for t, p in b.ticks:
            if t <= ts: last = p
            else: after = True; break
        return (last if after else None), after
    def bug_resolve(self):
        if not self.bug_sigs: return
        now = self.d.now(); keep = []; changed = False
        for g in self.bug_sigs:
            if now < g["settle_at"] + timedelta(seconds=1): keep.append(g); continue
            if g["kind"] == "now":
                o, c = g["entry_price"], self._tick_price(g["pair"], g["settle_at"])[0]
                if o is None or c is None:
                    if now - g["settle_at"] < timedelta(minutes=3): keep.append(g)
                    continue
                df = None
            else:
                df = self.cached_candles(g["pair"]); bar = df.loc[df.index == g["entry_at"]] if df is not None else None
                if bar is None or bar.empty:
                    if now - g["settle_at"] < timedelta(minutes=C.SHADOW_MAX_WAIT_MIN): keep.append(g)
                    continue
                o, c = float(bar.open.iloc[0]), float(bar.close.iloc[0])
                if g.get("expiry", 1) > 1:   # multi-minute expiry (P13): close of the last bar before settle
                    lb_ = df.loc[df.index == g["settle_at"] - timedelta(minutes=1)]
                    if lb_.empty:
                        if now - g["settle_at"] < timedelta(minutes=C.SHADOW_MAX_WAIT_MIN): keep.append(g)
                        continue
                    c = float(lb_.close.iloc[0])
            if c == o or g["k"] not in self.bugs: continue
            b = self.bugs[g["k"]]; win = (c > o) == (g["dir"] == "CALL"); b["fw"][0 if win else 1] += 1; changed = True
            if g.get("votes"): self.strat_record({"strategies": g["votes"]}, o, c)
            if not g["silent"]:
                strat = b["key"] if b.get("strategy") else b["key"]
                self.tg.admin(f"🐞 Bug result {g['pair']}: {'WIN' if win else 'LOSS'} ({o:.5f}→{c:.5f}) · {strat} {g['dir']} {C.hm(g['entry_at'])} {C.TZ_LABEL}"
                              f" · fwd {b['fw'][0]}W/{b['fw'][1]}L")
                if g["group"]:
                    tw = (c > o) == (g["traded"] == "CALL"); res = "WIN" if tw else "LOSS"; lab = self.d.label(g["pair"])
                    cap = "🐞 BUG SIGNAL\n" + result_caption(res, lab, g["traded"], o, c); sent = False
                    if C.RESULT_CHART and df is not None:
                        try: sent = self.tg.group_photo(render_result_chart(df, g["entry_at"], g["traded"], lab, res, C.EXPIRY_MIN), cap,
                                                       f"bug_{g['pair']}_{g['entry_at']:%Y%m%d_%H%M}.png", chats=g["chats"])
                        except Exception as e: log.warning("bug chart: %s", e)
                    if not sent: self.tg.group(cap, chats=g["chats"])
                    self.pubcap_result(g["chats"], res)
                    if any(ch[0] == "bug" for ch in g["chats"]): self.bug_day.setdefault(b["key"], [0, 0])[0 if res == "WIN" else 1] += 1; self.streak_result("bug", res); self.dstop_result(res, "bug")
                    if any(ch[0] in ("public", "vip") for ch in g["chats"]): self.streak_result("live", res); self.dstop_result(res)
            w, l = b["fw"]; be = breakeven(g["payout"])
            if w + l >= C.BUG_DEMOTE_MIN_FW and w/(w + l) < be and b.get("status") != "demoted":
                b["status"] = "demoted"
                self.tg.admin(f"⬇️ Bug pattern {b['pair']} {b['key']} demoted: forward {w}W/{l}L {w/(w+l):.0%} < breakeven {be:.1%} after "
                              f"≥{C.BUG_DEMOTE_MIN_FW} trades — it stops firing (still paper-tracked)")
        self.bug_sigs = keep
        if changed: self.save_bugs()
    def bug_strats(self, pair, x, now, payout, gaps):
        """B2/B3 on the closed candle (candles already in memory)."""
        if gaps or payout is None or len(x) < max(C.MIN_BARS, 50): return
        ensure_cols(x); i = len(x) - 1
        reg = (("B2", bug_B2), ("B3", bug_B3), ("B5m", bug_B5_1m), ("B6_14", lambda x, i: bug_B6(x, i, 14, 2.0)),
               ("B6_20", lambda x, i: bug_B6(x, i, 20, 2.0)), ("E", strat_E), ("F", strat_F))
        for k, fn in reg:
            if not _on(k): continue
            try: r = fn(x, i)
            except Exception as e: log.warning("%s %s: %s", k, pair, e); continue
            if r: self.bug_fire(f"*:{k}", pair, r[0], r[1] if isinstance(r[1], str) else f"score {r[1]}", now, payout, x=x)
        if C.PATTERNS_ON:
            try: hits = PAT.detect(PAT.bars_from(x), i) + PAT.p13_on_1m(x, i)
            except Exception as e: log.warning("patterns %s: %s", pair, e); hits = []
            for k, dd, desc in hits: self.bug_fire(f"*:{k}", pair, dd, desc, now, payout, x=x)
    def bug_tick_check(self):
        """B1 at ~:30 on live STREAM pairs only (5 s bars from ticks in memory; no requests)."""
        if not (C.BUG_WATCH and self.is_stream()) or self.manual_pause or self.budget_pause: return
        now = self.d.now()
        for pair in list(self.d.pairs):
            if pair in self.rotated_out: continue
            book = self.d.books.get(pair)
            if book is None or len(book.ticks) < 50: continue
            pay, op = self._cached_meta(pair)
            if pay is None: pay = self.d.pair_meta().get(pair, (None, False))[0]
            if not op or pay is None or pay < C.MIN_PAYOUT: continue
            df, gaps, open_ = self.d.candles(pair)
            if gaps or not open_ or df is None or len(df) < 30: continue
            tk = list(book.ticks); ts = now.timestamp(); x1 = compute(df)
            try:
                if _on("B1") and (r := bug_B1(tk, ts, x1)): self.bug_fire("*:B1", pair, r[0], r[1], now, pay, kind="now", entry_price=r[2], x=x1)
                if _on("B4") and (r := bug_B4(tk, ts, x1)):
                    self.bug_fire("*:B4" + ("p" if r[3] == "pause" else "f"), pair, r[0], r[1], now, pay, kind="now", entry_price=r[2], x=x1)
                if C.PATTERNS_ON:
                    sb = sub_bars(tk, ts, 30)
                    if len(sb) >= 6:
                        bb = PAT.Bars([q[1] for q in sb], [q[2] for q in sb], [q[3] for q in sb], [q[4] for q in sb])
                        for k, dd, desc in PAT.detect_p11(bb, len(sb) - 1):
                            self.bug_fire("*:P11", pair, dd, desc + " (30s)", now, pay, kind="now", entry_price=sb[-1][4], x=x1)
                for sec in C.B5_SUB_SECONDS:
                    if _on(f"B5s{sec}") and (r := bug_B5_sub(tk, ts, sec, x1)):
                        self.bug_fire(f"*:B5s{sec}", pair, r[0], r[1], now, pay, kind="now", entry_price=r[2], x=x1)
            except Exception as e: log.warning("tick strategies %s: %s", pair, e)
    def bug_pass_stream(self, now):
        """Stream mode: bug watch + B2/B3 on every closed candle for live pairs (free), even while a group trade is open."""
        try: meta = self.d.pair_meta()
        except Exception: return
        for pair in list(self.d.pairs):
            if pair in self.rotated_out: continue
            pay, op = meta.get(pair, (None, False))
            if not op or pay is None or pay < C.MIN_PAYOUT: continue
            df, gaps, open_ = self.d.candles(pair)
            if df is None or len(df) < max(C.MIN_BARS, 50) or not open_: continue
            self.hist_feed(pair, df); x = compute(df)
            try: self.bug_feed(pair, x, now, pay, gaps); self.bug_strats(pair, x, now, pay, gaps); self.bug_ox(pair, x, now, pay, gaps)
            except Exception as e: log.warning("bug pass %s: %s", pair, e)
    def bug_group_line(self):
        return f"Bug signals: {self.bug_mode.upper()} · bug group {'ON (always)' if C.BUG_CHANNEL_ID else 'not set (BUG_CHANNEL_ID)'}"
    def bug_day_summary(self):
        """03:00 UTC+3: short W/L per bug strategy to the bug group (results that were posted there)."""
        if not (C.BUG_CHANNEL_ID and self.bug_day): self.bug_day = {}; return
        parts = [f"{k} {w}W/{l}L" for k, (w, l) in sorted(self.bug_day.items())]
        tw = sum(v[0] for v in self.bug_day.values()); tl = sum(v[1] for v in self.bug_day.values())
        self.tg.group(f"🐞 BUG SIGNAL daily results: {tw}W/{tl}L" + (f" ({tw/(tw+tl):.0%})" if tw + tl else "") + "\n" + " · ".join(parts),
                      chats=[("bug", C.BUG_CHANNEL_ID)])
        self.bug_day = {}
    def bugsignals_cmd(self, args, quiet=False):
        if not args or args[0] not in ("admin", "group", "candidates", "off"):
            self.tg.admin(f"🐞 Bug signals: {self.bug_mode.upper()}. Use /bugsignals admin|group|candidates|off "
                          "(modes set EXTRA routing only: admin copy, public/VIP (GROUP_FILTER=best needs the /best list). The 🐞 bug group BUG_CHANNEL_ID always gets bug signals; only /pause stops it)"); return
        self.bug_mode = args[0]
        try:
            with open(C.BUG_SIGNALS_FILE + ".tmp", "w") as f: json.dump({"mode": self.bug_mode}, f)
            os.replace(C.BUG_SIGNALS_FILE + ".tmp", C.BUG_SIGNALS_FILE)
        except Exception as e: log.warning("bug mode save: %s", e)
        if quiet: return
        self.tg.admin(f"🐞 Bug signals → {self.bug_mode.upper()}" + (" ⚠️ experimental patterns can be false; they will post to the group" if self.bug_mode in ("group", "candidates") else ""))
    def bug_review(self):
        """Midnight UTC (03:00 UTC+3): forward paper W/L vs breakeven; promote to candidate / demote / drop. Admin only."""
        if not C.BUG_WATCH or not self.bugs: return
        rows = [f"🐞 Daily bug review {self.day_date} (EXPERIMENTAL, admin only, never posted)"]; drop = []
        for k, b in sorted(self.bugs.items()):
            w, l = b["fw"]; n = w + l; pay = self._cached_meta(b["pair"])[0] or C.DEFAULT_PAYOUT; be = breakeven(pay)
            st = b.get("status", "watch"); rate = w/n if n else None
            if (b.get("rule") is not None or b.get("strategy") or b.get("ox")) and n >= C.BUG_PROMOTE_MIN_FW:
                if rate >= be + C.BUG_PROMOTE_MARGIN and st != "candidate": st = "candidate"; tag = "⬆️ promoted to CANDIDATE"
                elif rate < be and st == "candidate": st = "watch"; tag = "⬇️ demoted to watch"
                elif rate < be and b.get("strategy"): st = "demoted"; tag = "⬇️ demoted (strategy kept, stops firing)"
                elif rate < be: drop.append(k); tag = "🗑 dropped"
                else: tag = "kept"
            else: tag = f"kept (needs {C.BUG_PROMOTE_MIN_FW} forward, has {n})" if (b.get("rule") is not None or b.get("strategy") or b.get("ox")) else "info only"
            b["status"] = st
            rows.append(f"  {b['pair']} {b['key']} [{st}]: forward {w}W/{l}L" + (f" {rate:.0%} vs BE {be:.1%}" if n else "") + f" → {tag}")
        for k in drop: self.bugs.pop(k, None)
        cands = [f"{b['pair']} {b['key']}" for b in self.bugs.values() if b.get("status") == "candidate"]
        rows.append(f"  Candidates carried over: {', '.join(cands) or 'none'} (admin only; never auto-posted)")
        self.save_bugs(); self.tg.admin("\n".join(rows))

    def bug_report(self):
        if not C.BUG_WATCH: return "🐞 Bug watch is OFF (BUG_WATCH=0)."
        rows = [f"🐞 Bug watch (EXPERIMENTAL; patterns can be false). Signals: {self.bug_mode.upper()} · variant {C.BUG_STRAT_VARIANT}",
                "Bug strategies:"]
        for k, nm in BUG_STRAT_NAMES.items():
            b = self.bugs.get(f"*:{k}", {"fw": [0, 0], "status": "watch"}); w, l = b["fw"]
            rows.append(f"  {k} {nm}{'' if k in C.BUG_STRATS else ' (off)'} [{b.get('status')}]: {w}W/{l}L" + (f" {w/(w+l):.0%}" if w + l else ""))
        rows.append("Detected patterns (history rate → forward W/L):")
        cands = [f"{b['pair']} {b['key']}" for b in self.bugs.values() if b.get("status") == "candidate"]
        rows.append(f"  Candidates: {', '.join(cands) or 'none yet'} (≥{C.BUG_PROMOTE_MIN_FW} forward at ≥ breakeven+{C.BUG_PROMOTE_MARGIN*100:.0f} pts)")
        for k, b in sorted(((k, b) for k, b in self.bugs.items() if not b.get("strategy")), key=lambda kv: -kv[1]["rate"])[:25]:
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
        ("Stats",    "/bugsignals", "bug signal routing: /bugsignals admin|bug|group|candidates|off (experimental)"),
        ("Channels", "/publiccap", "PUBLIC session: state/W/L/net/signals; /publiccap target N | max N | maxloss -N | start HH:MM"),
        ("Channels", "/sched",    "scheduled posts + event hooks (UTC+3): /sched add|on|del|test|pause|resume — /sched help"),
        ("Stats",    "/menu",     "admin button menu (also /start): status, stats, best, bugs, telemetry, pause, level, channel, mode, public, schedules, reset"),
        ("Stats",    "/reset",    "admin: /reset today|stats|bugs|telemetry|all, then /reset confirm within 60 s (backup first)"),
        ("Stats",    "/cooldown", "loss-streak cooldown: /cooldown · n N · min M · clear live|bug"),
        ("Stats",    "/blackout", "session-transition blackouts (DST-aware): /blackout on|off · set <spec>"),
        ("Stats",    "/news",     "high-impact news blackout: upcoming blocks · /news on|off|refresh"),
        ("Stats",    "/volume", "volume mode (Tier 3 fills toward 4–6/h; hard blocks still apply): /volume on|off"),
        ("Stats",    "/dailystop", "daily loss/profit stops (live + bug group separately): /dailystop loss 5 | alert 15 | close 20 | off | clear | resume 08:00"),
        ("Stats",    "/filters", "filter precedence and current state"),
        ("Stats",    "/weekly", "weekly report now; /weekly tune runs the ablation self-tune (proposals; safe bounds only)"),
        ("Stats",    "/patterns", "candle-pattern family P: forward record, status, history rule (/patterns on|off)"),
        ("Stats",    "/confirm2", "double confirmation (momentum + structure) for all signals: /confirm2 on|off"),
        ("Stats",    "/fill",     "Tier-2 fill to MIN_SIGNALS_PER_HOUR: /fill on|off"),
        ("Stats",    "/history",  "historical confirmation gate: /history all|bugs|off · coverage, passed/filtered today"),
        ("Stats",    "/best",     "leaderboard of all strategies (A-F, B1-B6, patterns): top 5, bottom 3, sample sizes"),
        ("Stats",    "/telemetry", "admin: data delivery, lag (median/p95), tick heatmap by second, over-extension bins"),
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
            "/telemetry": self.telemetry_cmd,
            "/best": lambda: self.tg.admin(self.best_text()),
            "/history": lambda: self.history_cmd(self._args),
            "/fill": lambda: self.fill_cmd(self._args), "/confirm2": lambda: self.dc_cmd(self._args), "/patterns": lambda: self.patterns_cmd(self._args), "/dailystop": lambda: self.dailystop_cmd(self._args), "/volume": lambda: self.volume_cmd(self._args),
            "/filters": lambda: self.filters_cmd(self._args), "/weekly": lambda: self.weekly_cmd(self._args),
            "/cooldown": lambda: self.cooldown_cmd(self._args), "/blackout": lambda: self.blackout_cmd(self._args), "/news": lambda: self.news_cmd(self._args),
            "/reset": lambda: self.reset_cmd(self._args),
            "/sched": lambda: self.tg.admin(self.sched.command("/sched " + " ".join(self._args))),
            "/publiccap": lambda: self.publiccap_cmd(self._args),
            "/bugsignals": lambda: self.bugsignals_cmd(self._args),
            "/level": lambda: self.level_cmd(self._args),
            "/channel": lambda: self.tg.admin(self.channel_info()),
            "/public": lambda: self.set_channel("public"), "/vip": lambda: self.set_channel("vip"), "/both": lambda: self.set_channel("both"),
            "/mode": lambda: self.tg.admin(self.mode_info()),
            "/stream": lambda: self.request_mode("stream"), "/poll": lambda: self.request_mode("poll"), "/auto": lambda: self.request_mode("auto"),
            "/test": self.test_card, "/testcard": self.test_card,
            "/commands": lambda: self.tg.admin(self.commands_text()), "/help": lambda: self.tg.admin(self.commands_text()),
            "/menu": self.menu_cmd, "/start": self.menu_cmd,
        }
    # ---- admin button menu (/menu, /start): callbacks map to the SAME handlers as typed commands ----
    def menu_view(self, name):
        B = ("⬅️ Back", "m:main")
        if name == "main":
            return "🤖 Admin menu", [
                [("📟 Status", "c:/status"), ("📊 Stats", "c:/stats"), ("🏆 Best", "c:/best")],
                [("🐞 Bugs", "c:/bugs"), ("📡 Telemetry", "c:/telemetry"), ("🗓 Schedules", "c:/sched")],
                [("▶️ Resume" if self.manual_pause else "⏸ Pause", "q:/resume" if self.manual_pause else "q:/pause"), ("🎚 Level", "m:level")],
                [("📣 Channel", "m:channel"), ("🔀 Mode", "m:mode"), ("🎯 Public session", "c:/publiccap")],
                [("📚 History", "m:history"), (f"🔁 Fill {C.MIN_SIGNALS_PER_HOUR:g}/h: {'ON' if C.FILL_MIN_HOUR else 'OFF'}", "q:/fill " + ("off" if C.FILL_MIN_HOUR else "on"))],
                [("❄️ Cooldown", "c:/cooldown"), ("🌗 Blackouts", "m:blackout"), ("📰 News", "c:/news")],
                [(f"🔗 Double-confirm: {'ON' if C.DOUBLE_CONFIRM else 'OFF'}", "q:/confirm2 " + ("off" if C.DOUBLE_CONFIRM else "on")), ("🕯 Patterns", "c:/patterns")],
                [(f"📙 Volume: {'ON' if C.VOLUME_MODE else 'OFF'}", "q:/volume " + ("off" if C.VOLUME_MODE else "on"))],
                [("🛑 Daily stop", "c:/dailystop"), ("🧭 Filters", "c:/filters"), ("📅 Weekly", "c:/weekly")],
                [("🧹 Reset", "m:reset"), ("📋 Commands", "c:/commands")]]
        if name == "blackout":
            return f"🌗 Session blackouts ({'ON' if C.BLACKOUT_ON else 'OFF'})", [[("On", "q:/blackout on"), ("Off", "q:/blackout off"), ("ℹ️ show", "c:/blackout")],
                                                                                [("News on", "q:/news on"), ("News off", "q:/news off")], [B]]
        if name == "history":
            return f"📚 History gate (now {C.HIST_GATE})", [[("All", "q:/history all"), ("Bugs", "q:/history bugs"), ("Off", "q:/history off")],
                                                           [("ℹ️ show", "c:/history")], [B]]
        if name == "level":
            return f"🎚 Level (now {self.level}{' auto' if self.level_auto else ''})", [
                [(str(i), f"q:/level {i}") for i in range(1, 6)], [("auto", "q:/level auto"), ("ℹ️ show", "c:/level")], [B]]
        if name == "channel":
            return f"📣 Channel (now {self.target_label()})", [[("Public", "q:/public"), ("VIP", "q:/vip"), ("Both", "q:/both")], [("ℹ️ show", "c:/channel")], [B]]
        if name == "mode":
            return "🔀 Data mode", [[("Stream", "q:/stream"), ("Poll", "q:/poll"), ("Auto", "q:/auto")], [("ℹ️ show", "c:/mode")], [B]]
        if name == "reset":
            return "🧹 Reset — pick a scope (a backup is written first)", [[(k, f"q:/reset {k}") for k in ("today", "stats", "bugs")],
                                                                        [(k, f"q:/reset {k}") for k in ("telemetry", "all")], [B]]
        return self.menu_view("main")
    def menu_cmd(self, edit_id=None):
        t, rows = self.menu_view("main"); self.tg.admin_kb(t, rows, edit_id)
    def on_callback(self, cq):
        """CallbackQuery handler: c: = run now (read-only), m: = sub-menu, q: = ask Yes/No, y: = confirmed run."""
        if str(cq.get("from", {}).get("id")) != str(C.TELEGRAM_ADMIN_ID): self.tg.answer_cb(cq.get("id"), "Not allowed"); return
        data = cq.get("data") or ""; mid = (cq.get("message") or {}).get("message_id"); self.tg.answer_cb(cq.get("id"))
        kind, _, cmd = data.partition(":")
        if kind == "m": t, rows = self.menu_view(cmd); self.tg.admin_kb(t, rows, mid)
        elif kind == "c": self.command(cmd)
        elif kind == "q":
            what = self.RESET_SCOPES.get(cmd.split()[-1], "") if cmd.startswith("/reset") else ""
            self.tg.admin_kb(f"❓ Confirm <b>{cmd}</b>?" + (f"\nThis clears: {what}" if what else ""),
                             [[("✅ Yes", f"y:{cmd}"), ("❌ No", "m:main")]], mid)
        elif kind == "y":
            if cmd.startswith("/reset "): self.reset_cmd([cmd.split()[1]]); self.reset_cmd(["confirm"])
            else: self.command(cmd)
            self.menu_cmd(mid)
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
        lines.append(f"{self.bug_group_line()} (experimental) · {self.pubcap_line()}")
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
            self.poll_adapter, self.d, self.stream_since = rest, sa, sa.now().date(); self._attach_tel()
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
        try: self.hist_housekeeping()
        except Exception as e: log.warning("history: %s", e)
        try: self.filters_tick(self.d.now())
        except Exception as e: log.warning("filters: %s", e)
        try: self.sched.tick(); self.session_start_check()
        except Exception as e: log.warning("scheduler: %s", e)
        try: self._tick()
        finally:
            if C.SHADOW_RELAXED:
                try: self.shadow_resolve()
                except Exception: log.exception("shadow resolve failed")
            if C.BUG_WATCH:
                try: self.bug_resolve()
                except Exception: log.exception("bug resolve failed")

    def fill_pool_add(self, pair, df, x, payout, gaps, now):
        """Wider Tier-2 pool: level-FILL_LEVEL thresholds, relaxed structure; still 77% floor, gaps, history-not-negative."""
        if now - self.fill_pair_last.get(pair, now - timedelta(hours=1)) < timedelta(minutes=C.FILL_PAIR_GAP_MIN): return
        from analyzer import _overrides, level_overrides
        with _overrides(level_overrides(C.FILL_LEVEL)): s = ensemble(df, pair, payout=payout, gaps=gaps, x=x)
        if s.get("signal") and self.dc_ok("fill", pair, s["signal"], x, fill=True): s["_df"] = df; s["_fill"] = True; self.fill_pool.append(s)
    def select_tier(self, cands, now):
        """Tier 1 = history-confirmed (best confidence first); Tier 2 = fill when behind the hourly minimum. None = nothing to send."""
        t1 = fill = None; notes = []
        for c_ in cands:
            if not self.group_allowed(c_)[0]: continue
            self.hist.last = None
            hok, hwhy, hline = self.hist_gate(c_["pair"], [k for k in c_.get("agree", []) if k != "D"], now, c_["payout"], "all")
            hs = dict(self.hist.last or {}) if C.HIST_GATE != "off" else {}
            if hok:
                t1 = c_; c_["tier"] = 1
                c_["hist_line"] = (f"✅ history-confirmed ({hs.get('w30')}/{hs.get('n30')})" if hs.get("n30") else "")
                break
            notes.append((c_, hwhy, hline))
        best = None; vol = None
        fdue = not t1 and self.fill_due(now) and self.hour_sent(now) < C.VOLUME_MAX_HOUR
        vdue = not t1 and not fdue and self.volume_due(now)
        if fdue or vdue:   # rank every eligible candidate (strict + wide pool) by score, best per slot
            pool = []; pool3 = []
            for c_ in cands + [f for f in self.fill_pool if f["pair"] not in {c["pair"] for c in cands}]:
                if now - self.fill_pair_last.get(c_["pair"], now - timedelta(hours=1)) < timedelta(minutes=C.FILL_PAIR_GAP_MIN): continue
                if self.pair_news(c_["pair"], now) or not self.group_allowed(c_)[0]: continue
                self.hist.last = None
                self.hist_gate(c_["pair"], [k for k in c_.get("agree", []) if k != "D"], now, c_["payout"], "all")
                hs = dict(self.hist.last or {}) if C.HIST_GATE != "off" else {}
                fok, fwhy = self.hist.fill_ok(hs, c_["payout"])
                be = 1 / (1 + c_["payout"]); hb = (hs["w30"] / hs["n30"] - be) if hs.get("n30") else 0.0
                c_["fill_score"] = c_["confidence"] / 100 + hb + (0.05 if not c_.get("_fill") else 0)
                if fok: c_["fill_why"] = fwhy; pool.append(c_)
                elif not hs.get("n30") or wilson_ub(hs["w30"], hs["n30"]) >= be:   # Tier 3: only 'not clearly negative'
                    c_["fill_why"] = f"history {hs.get('w30', 0)}/{hs.get('n30', 0)}, not clearly negative"; pool3.append(c_)
            if fdue and pool: fill = max(pool, key=lambda c: c["fill_score"])
            elif C.VOLUME_MODE and (pool or pool3) and (vdue or self.volume_due(now)):
                vol = max(pool + pool3, key=lambda c: c["fill_score"])
        if t1: best = t1; self.hist_count(True)
        elif fill is not None:
            best = fill; best["tier"] = 2; best["hist_line"] = "📘 standard"; self.hist_count(False); self.fill_pair_last[best["pair"]] = now
            self.tg.admin(f"📘 Tier-2 fill: {best['pair']} {best['signal']} ({best['fill_why']}) · {self.hour_sent(now)} sent this hour < target")
        elif vol is not None:
            best = vol; best["tier"] = 3; best["hist_line"] = "📙 volume"; self.hist_count(False); self.fill_pair_last[best["pair"]] = now
            self.tg.admin(f"📙 Tier-3 volume: {best['pair']} {best['signal']} ({best['fill_why']}) · {self.hour_sent(now)} sent this hour, pace {C.VOLUME_TARGET}–{C.VOLUME_MAX_HOUR}/h")
        else:
            for c_, hwhy, hline in notes[:3]:
                self.tg.admin(f"📚 Filtered by history: {c_['pair']} {c_['signal']} · agreed {','.join(c_.get('agree', []))} · {hwhy}"
                              + (f" · {hline}" if hline else "") + " · paper-tracked")
            self.hist_count(False)
            if cands: self.paper_ensemble(cands[0], now)
            return None
        return best
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
        if C.BUG_WATCH and self.is_stream(): self.bug_pass_stream(now)
        # settle pending trade first
        if self.pending and now >= self.pending["settle_at"]:
            self.settle(); return
        if self.pending or (self.block_until and now < self.block_until):
            return
        try: meta = self.d.pair_meta()
        except BudgetExhausted: self.budget_pause = True; self.tg.admin("🛑 OTCharts 429: quota exhausted. Paused."); return
        best = None; cands = []; self.fill_pool = []
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
            self.hist_feed(pair, df); x = compute(df)
            if C.BUG_WATCH and not self.is_stream():
                try: self.bug_feed(pair, x, now, payout, gaps); self.bug_strats(pair, x, now, payout, gaps); self.bug_ox(pair, x, now, payout, gaps)
                except Exception as e: log.warning("bug watch %s: %s", pair, e)
            s = ensemble(df, pair, payout=payout, gaps=gaps, x=x)
            if C.SHADOW_RELAXED and not s["signal"]: self.shadow_eval(df, x, pair, payout, gaps, now)
            if s["signal"]: s["_df"] = df
            if s["signal"] and self.dc_ok("ensemble " + "+".join(s.get("agree", [])), pair, s["signal"], x): cands.append(s)
            elif (C.FILL_MIN_HOUR or C.VOLUME_MODE) and C.FILL_LEVEL > self.level: self.fill_pool_add(pair, df, x, payout, gaps, now)
        pre = None
        if not cands and self.fill_pool and not self.live_block(now) and (self.fill_due(now) or self.volume_due(now)):
            pre = self.select_tier([], now)
            if pre is None: return
            cands = [pre]
        if not cands:
            if C.SEND_NO_SETUP: self.tg.admin("⚪ No setup"); return
            return
        cands.sort(key=lambda s: -s["confidence"]); best = cands[0]
        lb = self.live_block(now); nb = {c_["pair"]: self.pair_news(c_["pair"], now) for c_ in cands}
        if lb or all(nb.values()):
            why = lb or nb[best["pair"]]
            self.tg.admin(f"⛔ Not sent ({why}): {best['pair']} {best['signal']} · agreed {','.join(best.get('agree', []))} · tracked privately")
            self.paper_ensemble(best, now); return
        cands = [c_ for c_ in cands if not nb[c_["pair"]]]; best = cands[0]
        ok, why = self.group_allowed(best)
        if ok and pre is not None: best = pre
        elif ok:
            best = self.select_tier(cands, now)
            if best is None: return
        tg_chats = self.cap_chats(list(self.tg.chats or self.chats_for(self.target)))
        if ok and not tg_chats: ok, why = False, "PUBLIC daily win target reached"
        if not ok:
            self.tg.admin(f"🔒 Not sent (not yet proven): {best['pair']} {best['signal']} · agreed {','.join(best.get('agree', []))} · {why} · paper-tracked")
            self.paper_ensemble(best, now); return
        pnow = self._cached_meta(best["pair"])[0]
        if pnow is not None and pnow < C.MIN_PAYOUT:   # precedence 2: payout re-checked at send time
            self.tg.admin(f"💸 Not sent: {best['pair']} payout dropped to {pnow:.0%} (< {C.MIN_PAYOUT:.0%}) at send time · paper-tracked")
            self.paper_ensemble(best, now); return
        best["payout"] = pnow if pnow is not None else best.get("payout")
        entry = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
        best["entry_at"], best["settle_at"] = entry, entry + timedelta(minutes=C.EXPIRY_MIN)
        best["precheck_at"] = entry - timedelta(seconds=60 - C.PRECHECK_SECOND); best["prechecked"] = C.PRECHECK_MODE == "off"
        best["level"] = self.level
        best["chats"] = tg_chats   # :57 line + result follow the card (PUBLIC dropped once its daily cap is hit)
        self.pending = best; self.block_until = now + timedelta(minutes=C.SIGNAL_COOLDOWN_MIN)
        best.setdefault("tier", 1); self.tier_sent = [t for t in self.tier_sent if pd.Timestamp(t) > now - timedelta(hours=2)] + [str(now)]
        self.pubcap_posted(best["chats"])
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
        if s.get("hist_line") or s.get("tier"):   # admin-only mirror: group cards never show history/tier text
            self.tg.admin(f"🏷 {s['pair']} {d} {C.hm(entry)} {C.TZ_LABEL} · Tier {s.get('tier', 1)} · {s.get('hist_line') or 'no history line'}")
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
            f"🟢 Sent {s['pair']} → {lab} {d}{self.d.note(s['pair'], d)} · entry {C.hm(entry)} {C.TZ_LABEL} · {self.tel.lag_tag(s['pair'])}\n"
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
        if C.TELEMETRY: self.tel.on_precheck(t["pair"], t["entry_at"].timestamp(), time.time())
        try: df, _, _ = self.d.candles(t["pair"], include_forming=True)
        except Exception as e: log.warning("precheck fetch: %s", e); df = None
        fstart = t["entry_at"] - timedelta(minutes=1)
        bar = df.loc[df.index == fstart] if df is not None else None
        if bar is None or bar.empty:
            ok, why = (C.PRECHECK_MODE != "confirm"), "forming candle unavailable"   # direction mode: keep original
        else:
            ok, why = precheck(t, float(bar.open.iloc[0]), float(bar.close.iloc[0]))
        t["precheck_ok"], t["precheck_reason"] = ok, why
        self.prechecks += 1; self.prechecks_day += 1; self.save_day()
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
                self.day.setdefault(t["pair"], [0, 0, 0])[2] += 1; self.save_day()
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
            if not tie: self.tier_record(t, win)
            if C.GROUP_SHOW_RESULTS:
                lab = self.d.label(t["pair"]); cap = result_caption(res, lab, t["signal"], o, c); sent = False
                if C.RESULT_CHART:
                    try:
                        png = render_result_chart(df, t["entry_at"], t["signal"], lab, res, C.EXPIRY_MIN)
                        sent = self.tg.group_photo(png, cap, f"{t['pair']}_{t['entry_at']:%Y%m%d_%H%M}.png", chats=t.get("chats"))
                    except Exception as e: log.warning("chart render failed (%s); sending text", e)
                if not sent: self.tg.group(cap, chats=t.get("chats"))
            self.pubcap_result(t.get("chats"), res)
            if any(c_[0] in ("public", "vip", "group") for c_ in (t.get("chats") or [])): self.streak_result("live", res); self.dstop_result(res)
            if not tie:
                self.day.setdefault(t["pair"], [0, 0, 0])[0 if win else 1] += 1
                k = "switched" if t.get("kind", "").startswith("switched") else "confirmed"
                self.kind_day[k][0 if win else 1] += 1
            self.tg.admin(f"📋 Round {C.hm(t['entry_at'])} {C.TZ_LABEL} {t['pair']} ({self.d.label(t['pair'])}) {t.get('original_signal', t['signal'])}"
                          f"{(' → traded ' + t['signal'] + ' (' + t.get('kind', '') + ')') if t.get('original_signal', t['signal']) != t['signal'] else ' (confirmed)'} · "
                          f"precheck: {t.get('precheck_candle', 'n/a')} → traded · result {res} ({o:.5f} → {c:.5f})\n"
                          f"  strategies agreed: {','.join(t.get('agree', [])) or '—'} · {strategies_line(t)} · level {t.get('level', '?')}")
            if not tie: self.ladder_update(t["pair"], win)
            self.save_day(); self.save_bench()
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
                    and self.n_trades(p) >= C.COMPOUND_MIN_TRADES and not tracker.gate(p, self._cached_meta(p)[0] or C.DEFAULT_PAYOUT)]
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
        lines.append("  " + self.pubcap_line())
        lines += ["  " + l for l in self.tier_lines()]
        lines.append(f"  🔗 Double-confirm {'on' if C.DOUBLE_CONFIRM else 'off'}: {self.dc_day.get('dropped', 0)} single-confirmation dropped of {self.dc_day.get('checked', 0)}")
        lines.append(f"  📚 History gate {C.HIST_GATE}: {self.hist_day['passed']} passed · {self.hist_day['filtered']} filtered by history today")
        lines += ["  🧩 Strategies (today):"] + ["  " + l for l in self.strat_lines(True)]
        if C.SHADOW_RELAXED:
            lines += ["  👻 Shadow (relaxed) — not posted:"] + ["  " + l for l in self.shadow_lines(True)]
        return "\n".join(lines)

    def maybe_daily_summary(self, force=False):
        """Rolls over ONLY when the UTC day changed. force (shutdown) just reports, never resets."""
        today = self.d.now().date()
        if self.day_date == today:
            if force and (any(sum(v) for v in self.day.values())):
                self.tg.admin(self.summary_text(f"📊 Stats so far {self.day_date} (restart/shutdown — NOT reset; continues after restart)"))
            if force: self.save_day()
            return
        if any(sum(v) for v in self.day.values()) or (C.SHADOW_RELAXED and any(sum(v) for v in self.shadow_day.values())):
            self.tg.admin(self.summary_text())
        if C.TELEMETRY:
            try: self.tg.admin(self.tel.summary(short=True))
            except Exception as e: log.warning("telemetry summary: %s", e)
        try: self.bug_day_summary()
        except Exception as e: log.warning("bug day summary: %s", e)
        try: self.lb_apply()
        except Exception as e: log.warning("leaderboard: %s", e)
        try: self.bug_review()
        except Exception as e: log.warning("bug review: %s", e)
        try: self.week_record()
        except Exception as e: log.warning("week record: %s", e)
        self.shadow_day = {}; self.strat_day = {}; self.save_strat()
        self.day = {p: [0, 0, 0] for p in self.all_pairs}; self.day_date = today
        if today.weekday() == 6:   # 00:00 UTC Sunday = 03:00 UTC+3 Sunday review
            try: self.weekly_report()
            except Exception as e: log.warning("weekly report: %s", e)
            if C.WEEKLY_TUNE: self.weekly_tune(background=True)
        self.kind_day = {"confirmed": [0, 0], "switched": [0, 0]}; self.prechecks_day = self.switches_day = self.kept_day = 0
        self.hist_day = {"filtered": 0, "passed": 0}; self.tier_day = {}; self.dc_day = {}; self.p_hist_day = {}; self.save_day()
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
        bot.persistence_check()
    if hasattr(data, "res"):
        bot.tg.admin("🔎 Symbol resolution:\n" + "\n".join(describe(r) for r in data.res.values()))
    n = 0
    if mock is not None:   # simulated clock driven by mocked ticks
        t = mock.now().replace(second=0) + timedelta(minutes=1)
        for n in range(C.DRY_RUN_MINUTES or 600):
            mock.advance_to(t.replace(second=C.SCAN_SECOND))
            try:
                bot.tick()
                for sc in C.TICK_CHECK_SECONDS: mock.advance_to(t.replace(second=sc)); bot.bug_tick_check()
                mock.advance_to(t.replace(second=C.PRECHECK_SECOND))
                if bot.pending and not bot.pending["prechecked"]: bot.run_precheck()
                bot.bug_precheck_due()
            except Exception: log.exception("tick failed")
            t += timedelta(minutes=1)
        STOP.set()
    while not STOP.is_set():
        if isinstance(data, ReplayAdapter):
            try:
                bot.tick()
                if bot.pending and not bot.pending["prechecked"]:
                    saved = data.clock; data.clock = bot.pending["precheck_at"]; bot.run_precheck(); data.clock = saved
                saved = data.clock; data.clock = data.clock.replace(second=C.PRECHECK_SECOND); bot.bug_precheck_due(); data.clock = saved
            except Exception: log.exception("tick failed")
            data.advance(); n += 1
            if C.DRY_RUN_MINUTES and n >= C.DRY_RUN_MINUTES: break
            continue
        now = datetime.now(UTC)
        target = now.replace(second=C.SCAN_SECOND, microsecond=0)
        if target <= now: target += timedelta(minutes=1)
        pc = bot.pending["precheck_at"] if bot.pending and not bot.pending["prechecked"] else None
        bpc = [g["entry_at"] - timedelta(seconds=60 - C.PRECHECK_SECOND) for g in bot.bug_sigs if g["group"] and not g["prechecked"]]
        pc = min([p for p in [pc] + bpc if p is not None and p > now], default=None)
        b1 = min([now.replace(second=sc, microsecond=0) + (timedelta(minutes=1) if now.replace(second=sc, microsecond=0) <= now else timedelta(0))
                  for sc in C.TICK_CHECK_SECONDS])
        b1 = b1 if (C.BUG_WATCH and bot.is_stream()) else None
        nxt = min([p for p in (pc, b1) if p is not None and p < target], default=None)
        if nxt:
            if STOP.wait((nxt - now).total_seconds()): break
            try:
                if nxt == b1: bot.bug_tick_check()
                if bot.pending and not bot.pending["prechecked"] and datetime.now(UTC) >= bot.pending["precheck_at"] - timedelta(seconds=1): bot.run_precheck()
                bot.bug_precheck_due()
            except Exception: log.exception("precheck/bug check failed (continuing)")
            continue
        if STOP.wait((target - now).total_seconds()): break
        try: bot.tick()
        except Exception: log.exception("tick failed (continuing)")
    if hasattr(bot.d, "close"): bot.d.close()
    bot.maybe_daily_summary(force=True); bot.save_stats(); bot.save_strat(); bot.save_bench(); bot.hist.store.flush(force=True)
    try: bot.save_shadow()
    except Exception as e: log.warning("shadow save: %s", e)
    log.info("Stopped cleanly. %s", bot.status().replace("\n", " | "))

if __name__ == "__main__":
    main()
