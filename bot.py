"""M1 OTC signal bot (NO martingale). Long-lived worker: `python bot.py`.
Data: OTCharts REST (/v1/candles polled once per minute after candle close) or CSV replay.
Telegram: plain Bot API over requests. Logs to stdout."""
from __future__ import annotations
import json, logging, os, signal, sys, threading, time
from datetime import datetime, timedelta, timezone
import pandas as pd, requests
import config as C
from analyzer import analyze, compute, tracker, breakeven, precheck
from resolver import resolve, describe, Resolution
from chart import result_caption, render_result_chart, render_chart, signal_caption

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
    def group(self, t): self.send(C.TELEGRAM_CHANNEL_ID, t)
    def group_photo(self, png: bytes, caption: str, name="result.png"):
        """sendPhoto (multipart). When Telegram is disabled the PNG is written to CHART_DIR."""
        if not self.on or not C.TELEGRAM_CHANNEL_ID:
            os.makedirs(C.CHART_DIR, exist_ok=True); path = os.path.join(C.CHART_DIR, name)
            with open(path, "wb") as f: f.write(png)
            log.info("[TG->group photo] %s (%s, %d bytes)", caption, path, len(png)); return True
        try:
            r = requests.post(f"{self.base}/sendPhoto", data={"chat_id": C.TELEGRAM_CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
                              files={"photo": (name, png, "image/png")}, timeout=20)
            if not r.ok: log.warning("sendPhoto failed %s: %s", r.status_code, r.text[:200])
            return r.ok
        except requests.RequestException as e: log.warning("sendPhoto error: %s", e); return False
    def admin(self, t): self.send(C.TELEGRAM_ADMIN_ID, t, "admin")
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
        self.day = {p: [0, 0, 0] for p in C.PAIRS}; self.day_date = data.now().date()
        self.compound_active = list(C.COMPOUND_PAIRS); self.lost_ladders = {}; self.rotated_out = set(); self.ladder = {}
        self.block_until = None        # cooldown end (datetime)
        self.sessions = parse_sessions(C.SESSIONS_UTC); self.usage_at = 0.0
        self.load_stats()

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

    # ---- admin commands ----
    def command(self, cmd):
        if cmd == "/pause": self.manual_pause = True; self.tg.admin("⏸ Paused by admin")
        elif cmd == "/resume": self.manual_pause = False; self.budget_pause = False; self.tg.admin("▶️ Resumed")
        elif cmd == "/status": self.tg.admin(self.status())
    def status(self):
        lines = [f"Paused: manual={self.manual_pause} budget={self.budget_pause} session={self.in_session()}"]
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
        if frac >= C.BUDGET_PAUSE and not self.budget_pause:
            self.budget_pause = True; self.tg.admin(f"🛑 API budget {frac:.0%} ({used}/{quota}). Auto-paused. /resume after reset.")
        elif frac >= C.BUDGET_WARN and not self.warned_budget:
            self.warned_budget = True; self.tg.admin(f"⚠️ API budget {frac:.0%} used ({used}/{quota}).")
        elif frac < C.BUDGET_WARN: self.warned_budget = False
        log.info("API usage %d/%d (%.0f%%)", used, quota, frac*100)

    # ---- one minute ----
    def tick(self):
        now = self.d.now()
        self.maybe_daily_summary()
        self.check_budget()
        if self.manual_pause or self.budget_pause or not self.in_session():
            log.info("idle (paused/out of session)"); return
        # settle pending trade first
        if self.pending and now >= self.pending["settle_at"]:
            self.settle(); return
        if self.pending or (self.block_until and now < self.block_until):
            return
        try: meta = self.d.pair_meta()
        except BudgetExhausted: self.budget_pause = True; self.tg.admin("🛑 OTCharts 429: quota exhausted. Paused."); return
        best = None
        for pair in C.PAIRS:
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
            s = analyze(df, pair, payout=payout, gaps=gaps)
            if s["signal"]: s["_df"] = df
            if s["signal"] and (best is None or s["confidence"] > best["confidence"]): best = s
        if not best:
            if C.SEND_NO_SETUP: self.tg.admin("⚪ No setup"); return
            return
        entry = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
        best["entry_at"], best["settle_at"] = entry, entry + timedelta(minutes=C.EXPIRY_MIN)
        best["precheck_at"] = entry - timedelta(seconds=60 - C.PRECHECK_SECOND); best["prechecked"] = C.PRECHECK_MODE == "off"
        self.pending = best; self.block_until = now + timedelta(minutes=C.SIGNAL_COOLDOWN_MIN)
        self.post_signal(best)

    # ---------- messages ----------
    def playbook(self, d):
        if d == "CALL":
            return ("If the current candle is GREEN at :57 → enter CALL", "If it's RED strongly → SKIP")
        return ("If the current candle is RED at :57 → enter PUT", "If it's GREEN strongly → SKIP")

    def post_signal(self, s):
        lab = self.d.label(s["pair"]); entry = s["entry_at"]; d = s["signal"]
        arrow = "🟢 CALL ⬆️" if d == "CALL" else "🔴 PUT ⬇️"
        p1, p2 = self.playbook(d)
        cap = signal_caption(lab, d, entry, C.EXPIRY_MIN, (p1, p2) if C.PRECHECK_MODE == "confirm" else None, C.PRECHECK_SECOND)
        sent = False
        if C.SIGNAL_CHART and s.get("_df") is not None:
            try: sent = self.tg.group_photo(render_chart(s["_df"], lab), cap, f"signal_{s['pair']}_{entry:%Y%m%d_%H%M}.png")
            except Exception as e: log.warning("signal chart failed (%s); sending text", e)
        if not sent: self.tg.group(cap)
        s.pop("_df", None)
        a = s["atr"]
        votes = "\n".join(f"  • {k}: {'+1' if v > 0 else ('-1' if v < 0 else ' 0')} {t or '(no vote)'}" for k, (v, t) in s.get("votes", {}).items())
        comp = ""
        if s["pair"] in self.compound_active: comp = f"\n🔥 compound pair: ladder step {self.ladder.get(s['pair'], 0)+1}/{C.COMPOUND_TARGET}"
        self.tg.admin(
            f"🟢 Sent {s['pair']} → {lab} {d}{self.d.note(s['pair'], d)} · entry {entry:%H:%M}\n"
            f"Regime {s['regime']} (ADX {s.get('adx','?')}) · score {s['score']}/{s['max_score']} ({s['confidence']}%) · payout {s['payout']:.0%} (BE {s['breakeven']:.1%})\n"
            f"S {s['support']:.5f} / R {s['resistance']:.5f} · ATR {a:.5f} · overlap={s.get('overlap')}\nVotes:\n{votes}\n"
            f"Playbook reasoning:\n  • '{p1}': confirms if the candle is with the signal, or the opposing body is < {C.PC_SMALL_BODY_ATR} ATR, and price hasn't run > {C.PC_EXHAUST_ATR} ATR (exhausted) or reached S/R\n"
            f"  • '{p2}': cancels if the opposing body is ≥ {C.PC_STRONG_BODY_ATR} ATR, it moved > {C.PC_MAX_ADVERSE_ATR} ATR against, or crossed back through EMA9 / the signal-candle midpoint{comp}")

    def run_precheck(self):
        """Re-read the forming candle just before entry (1 extra request) and confirm or cancel."""
        t = self.pending; t["prechecked"] = True
        try: df, _, _ = self.d.candles(t["pair"], include_forming=True)
        except Exception as e: log.warning("precheck fetch: %s", e); df = None
        fstart = t["entry_at"] - timedelta(minutes=1)
        bar = df.loc[df.index == fstart] if df is not None else None
        if bar is None or bar.empty:
            ok, why = (C.PRECHECK_MODE != "confirm"), "forming candle unavailable"
        else:
            ok, why = precheck(t, float(bar.open.iloc[0]), float(bar.close.iloc[0]))
        t["precheck_ok"], t["precheck_reason"] = ok, why
        self.prechecks += 1
        lab = self.d.label(t["pair"])
        if bar is not None and not bar.empty:
            o_, c_ = float(bar.open.iloc[0]), float(bar.close.iloc[0])
            colour = "GREEN" if c_ > o_ else ("RED" if c_ < o_ else "DOJI"); body = abs(c_-o_)/(t["atr"] or 1e-12)
            cinfo = f"forming candle {colour}, body {body:.2f} ATR"
        else: cinfo = "forming candle unavailable"
        t["precheck_candle"] = cinfo
        if C.PRECHECK_MODE == "confirm":
            if ok:
                self.tg.group(f"✅ CONFIRMED {lab} {t['signal']} — enter now at {t['entry_at']:%H:%M}:00")
                self.tg.admin(f"✅ Precheck OK {t['pair']} ({lab}): {cinfo} → TRADE. {why}")
            else:
                self.tg.group(f"❌ SKIP {lab} {t['signal']}")
                self.tg.admin(f"❌ Precheck CANCEL {t['pair']} ({lab}): {cinfo} → SKIPPED (not a loss). {why}")
                self.day[t["pair"]][2] += 1
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
                if self.n_trades(t["pair"]) == C.COMPOUND_MIN_TRADES:
                    self.tg.admin(f"🎓 {t['pair']} now eligible for compounding ({C.COMPOUND_MIN_TRADES} trades, WR {tracker.total[t['pair']][0]/C.COMPOUND_MIN_TRADES:.0%})")
            self.save_stats(); self.log_trade(t, o, c, res)
            if C.GROUP_SHOW_RESULTS:
                lab = self.d.label(t["pair"]); cap = result_caption(res, lab, t["signal"], o, c); sent = False
                if C.RESULT_CHART:
                    try:
                        png = render_result_chart(df, t["entry_at"], t["signal"], lab, res, C.EXPIRY_MIN)
                        sent = self.tg.group_photo(png, cap, f"{t['pair']}_{t['entry_at']:%Y%m%d_%H%M}.png")
                    except Exception as e: log.warning("chart render failed (%s); sending text", e)
                if not sent: self.tg.group(cap)
            if not tie: self.day[t["pair"]][0 if win else 1] += 1
            self.tg.admin(f"📋 Round {t['entry_at']:%H:%M} {t['pair']} ({self.d.label(t['pair'])}) {t['signal']} · "
                          f"precheck: {t.get('precheck_candle', 'n/a')} → traded · result {res} ({o:.5f} → {c:.5f})")
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
            self.rotated_out.add(pair)
            if repl:
                self.tg.admin(f"🔄 Rotation: {pair} OUT of compounding for today ({self.lost_ladders[pair]} lost ladders) → "
                              f"{repl} IN ({self.n_trades(repl)} trades, rolling WR {tracker.winrate(repl):.0%}). Active: {self.compound_active}")
            else:
                self.tg.admin(f"🔄 Rotation: {pair} OUT of compounding for today ({self.lost_ladders[pair]} lost ladders) → "
                              f"slot left EMPTY: no pair has ≥{C.COMPOUND_MIN_TRADES} settled trades. {self.eligibility_line()}. Active: {self.compound_active}")
            log.info("Rotation %s -> %s", pair, repl)

    def n_trades(self, p):
        w, l = tracker.total[p]; return w + l

    def eligibility_line(self):
        out = []
        for p in C.PAIRS:
            n = self.n_trades(p)
            out.append(f"{p} {n}/{C.COMPOUND_MIN_TRADES} " + ("eligible" if n >= C.COMPOUND_MIN_TRADES else "not eligible"))
        return " · ".join(out)

    def maybe_daily_summary(self, force=False):
        today = self.d.now().date()
        if self.day_date == today and not force: return
        if any(sum(v) for v in self.day.values()):
            lines = [f"📊 Daily summary {self.day_date} (UTC)"]
            tw = tl = tc = 0
            for p in C.PAIRS:
                w, l, c = self.day[p]; tw += w; tl += l; tc += c
                if w or l or c: lines.append(f"  {p}: {w}W / {l}L / {c} skipped" + (f" · {w/(w+l):.0%}" if w+l else ""))
            lines.append(f"  Total {tw}W / {tl}L / {tc} skipped · prechecks {self.prechecks}")
            try: u = self.d.usage(); lines.append(f"  API budget used {u.get('used')}/{u.get('quota')}")
            except Exception: pass
            if self.rotated_out: lines.append(f"  Rotated out today: {sorted(self.rotated_out)}")
            lines.append(f"  Compounding active: {self.compound_active}")
            lines.append("  Eligibility: " + self.eligibility_line())
            self.tg.admin("\n".join(lines))
        self.day = {p: [0, 0, 0] for p in C.PAIRS}; self.day_date = today
        self.compound_active = list(C.COMPOUND_PAIRS); self.lost_ladders = {}; self.rotated_out = set(); self.ladder = {}

    def log_trade(self, t, o, c, res):
        import csv
        new = not os.path.exists(C.TRADE_LOG)
        with open(C.TRADE_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["entry_at", "pair", "dir", "confidence", "regime", "payout", "overlap", "precheck", "open", "close", "result"])
            w.writerow([f"{t['entry_at']:%Y-%m-%d %H:%M}", t["pair"], t["signal"], t["confidence"], t["regime"], t["payout"],
                        t.get("overlap"), t.get("precheck_reason", ""), o, c, res])

def main():
    miss = C.missing_secrets()
    if miss:
        log.error("Missing required env vars: %s. Set them in Railway > Variables. Exiting.", ", ".join(miss)); sys.exit(2)
    data = ReplayAdapter(C.REPLAY_CSV) if C.DATA_SOURCE == "replay" else OTChartsAdapter()
    bot = Bot(data)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set()); signal.signal(signal.SIGINT, lambda *_: STOP.set())
    log.info("Started: source=%s book=%s pairs=%d telegram=%s", C.DATA_SOURCE, C.BROKER_BOOK, len(C.PAIRS), C.TELEGRAM_ENABLED)
    if bot.tg.on:
        threading.Thread(target=bot.tg.poll_commands, args=(bot.command,), daemon=True).start()
        bot.tg.admin("🤖 Bot started")
    if hasattr(data, "res"):
        bot.tg.admin("🔎 Symbol resolution:\n" + "\n".join(describe(r) for r in data.res.values()))
    n = 0
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
    bot.maybe_daily_summary(force=True); bot.save_stats(); log.info("Stopped cleanly. %s", bot.status().replace("\n", " | "))

if __name__ == "__main__":
    main()
