"""Conservative statistical guards for VIP / public / bug (shadow is never blocked, only tagged):
correlation cap, hour seasonality block, ATR-percentile throttle, Bayesian Beta pattern pause, pattern decay retirement,
Benjamini-Hochberg promotion, regime labels, reproducibility hashes and a features.jsonl for future meta-labelling.
Every Bot-side entry point is wrapped: a failure here never blocks the trading loop (it fails OPEN only for these
extra filters; the existing hard rules — 77% payout fail-closed, :57 line, freezes — are untouched)."""
import os, json, math, time, hashlib, logging, glob
from datetime import datetime, timedelta, timezone
import numpy as np
import config as C
log = logging.getLogger("quant")
E = lambda k, d: os.environ.get(k, d)
CORR_CAP = E("CORR_CAP", "on") == "on"; CORR_BARS = int(E("CORR_BARS", "120")); CORR_THRESH = float(E("CORR_THRESH", "0.6"))
HOUR_BLOCK = E("HOUR_BLOCK", "on") == "on"; HOUR_MIN_N = int(E("HOUR_MIN_N", "30"))
ATR_THROTTLE = E("ATR_THROTTLE", "on") == "on"; ATR_LO = float(E("ATR_LO_PCT", "20")); ATR_HI = float(E("ATR_HI_PCT", "80")); ATR_MIN_DAYS = float(E("ATR_MIN_DAYS", "3"))
BAYES = E("BAYES_PAUSE", "on") == "on"; BAYES_PRIOR = float(E("BAYES_PRIOR", "10")); BAYES_MIN_N = int(E("BAYES_MIN_N", "20"))
BAYES_PAUSE_P = float(E("BAYES_PAUSE_P", "0.1")); BAYES_RESUME_P = float(E("BAYES_RESUME_P", "0.5"))
DECAY = E("PATTERN_DECAY", "on") == "on"; DECAY_DAYS = int(E("DECAY_DAYS", "14")); DECAY_MIN_N = int(E("DECAY_MIN_N", "30")); DECAY_SD = float(E("DECAY_SD", "2"))
MT_METHOD = E("MT_METHOD", "bh"); MT_FDR = float(E("MT_FDR", "0.10")); MT_WILSON_MARGIN = float(E("MT_WILSON_MARGIN", "0.02"))
WF_HOLDOUT = float(E("WF_HOLDOUT", "0.30"))
_SRC = ("analyzer.py", "patterns.py", "bugstrats.py", "quant.py")
def analyzer_version():
    h = hashlib.sha256()
    for f in _SRC:
        try: h.update(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), f), "rb").read())
        except OSError: pass
    return h.hexdigest()[:12]
ANALYZER_VERSION = analyzer_version()

# ---------- stats helpers ----------
def wilson(w, n, z=1.96):
    if n <= 0: return 0.0, 1.0
    p = w / n; d = 1 + z * z / n; c = p + z * z / (2 * n); m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d, (c + m) / d
def beta_prob_above(a, b, x, steps=2000):
    """P(p > x) for p ~ Beta(a, b), numeric (log-space midpoint rule; no scipy)."""
    if x <= 0: return 1.0
    if x >= 1: return 0.0
    lg = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b); tot = above = 0.0
    for i in range(steps):
        p = (i + 0.5) / steps; v = math.exp(lg + (a - 1) * math.log(p) + (b - 1) * math.log(1 - p))
        tot += v; above += v if p > x else 0.0
    return above / tot if tot else 0.0
def binom_p_greater(w, n, p0):
    """One-sided exact binomial p-value P(X >= w | n, p0)."""
    if n <= 0: return 1.0
    lp = lambda k: math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * math.log(p0) + (n - k) * math.log(1 - p0)
    return min(1.0, sum(math.exp(lp(k)) for k in range(int(w), n + 1)))
def bh_select(pvals, q=0.10):
    """Benjamini-Hochberg: {key: p} → set of keys significant at FDR q."""
    items = sorted(pvals.items(), key=lambda kv: kv[1]); m = len(items); cut = 0
    for i, (_, p) in enumerate(items, 1):
        if p <= q * i / m: cut = i
    return {k for k, _ in items[:cut]}
def promoted_set(cands, be_of):
    """cands {key: (w, n)} → keys that pass multiple-testing-corrected promotion (method MT_METHOD)."""
    if MT_METHOD == "off": return {k for k, (w, n) in cands.items() if n and w / n >= be_of(k)}
    if MT_METHOD == "wilson": return {k for k, (w, n) in cands.items() if wilson(w, n)[0] >= be_of(k) + MT_WILSON_MARGIN}
    return bh_select({k: binom_p_greater(w, n, be_of(k)) for k, (w, n) in cands.items() if n}, MT_FDR)

# ---------- market features ----------
def candles_sha(df, n=200):
    try:
        d = df[["open", "high", "low", "close"]].tail(n)
        return hashlib.sha256(np.ascontiguousarray(d.values.astype("float64")).tobytes() + str(list(d.index[[0, -1]])).encode()).hexdigest()
    except Exception: return ""
def atr_series(df, n=14):
    h, l, c = df.high.values, df.low.values, df.close.values; pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return np.convolve(tr, np.ones(n) / n, mode="valid")
def atr_percentile(cur, ref):
    if ref is None or len(ref) < 100 or cur is None or not np.isfinite(cur): return None
    return float(np.searchsorted(ref, cur, side="right") / len(ref) * 100)
def regime_label(x, atr_pct):
    """volatile / quiet (ATR percentile) take precedence, else trending (ADX≥25 or ER≥0.4) / ranging."""
    try:
        if atr_pct is not None and atr_pct > ATR_HI: return "volatile"
        if atr_pct is not None and atr_pct < ATR_LO: return "quiet"
        r = x.iloc[-1]; adx = float(r.get("adx", 0) or 0); er = float(r.get("er", 0) or 0)
        return "trending" if adx >= 25 or er >= 0.4 else "ranging"
    except Exception: return "unknown"
def corr_clusters(closes, thresh=CORR_THRESH, bars=CORR_BARS):
    """{pair: Series close} → {pair: cluster_id} by union-find over |corr of 1-min returns| > thresh (last `bars`)."""
    import pandas as pd
    rets = pd.DataFrame({p: s.tail(bars + 1).pct_change() for p, s in closes.items() if s is not None and len(s) > bars // 2}).dropna(how="all")
    pairs = list(closes); par = {p: p for p in pairs}
    def f(p):
        while par[p] != p: par[p] = par[par[p]]; p = par[p]
        return p
    if len(rets.columns) > 1:
        cm = rets.corr(min_periods=max(30, bars // 2))
        cols = list(cm.columns)
        for i, a in enumerate(cols):
            for b in cols[i + 1:]:
                v = cm.at[a, b]
                if v == v and abs(v) > thresh: par[f(a)] = f(b)
    return {p: f(p) for p in pairs}

# ---------- Bot mixin ----------
class QuantGuards:
    def q_path(self): return os.path.join(C.STATE_DIR, "quant_state.json")
    def q_load(self):
        try: self.q = json.load(open(self.q_path()))
        except Exception: self.q = {}
        for k, v in (("pat", {}), ("paused", {}), ("retired", {}), ("hour_unblock", []), ("corr_on", CORR_CAP)): self.q.setdefault(k, v)
        self._corr = {"at": 0, "map": {}}; self._corr_slots = {}; self._atr_ref = {}; self._hours = {"at": 0, "blocked": {}}
    def q_save(self):
        try:
            tmp = self.q_path() + ".tmp"; json.dump(self.q, open(tmp, "w")); os.replace(tmp, self.q_path())
        except Exception as e: log.warning("quant save: %s", e)
    # ---- (3) ATR percentile vs last 30 days of stored candles ----
    def q_atr_pct(self, pair, cur_atr):
        try:
            if self.hist.store.coverage_days(pair) < ATR_MIN_DAYS: return None
            ref = self._atr_ref.get(pair)
            if ref is None or time.time() - ref[0] > 3600:
                df = self.hist.store.get(pair)
                if df is None or len(df) < 1000: return None
                ref = (time.time(), np.sort(atr_series(df.iloc[-30 * 1440:]))); self._atr_ref[pair] = ref
            return atr_percentile(cur_atr, ref[1])
        except Exception as e: log.warning("atr pct %s: %s", pair, e); return None
    # ---- (2) hour seasonality ----
    def q_hour_blocked(self, now):
        try:
            if not HOUR_BLOCK: return {}
            if time.time() - self._hours["at"] > 3600:
                import adminjobs as AJ
                rows = [r for r in AJ.results(30, now) if r[0] in ("vip", "public", "bug") and r[3] != "TIE"]
                be = 1 / (1 + C.DEFAULT_PAYOUT); out = {}
                for h in range(24):
                    rr = [r for r in rows if r[2].hour == h]; w = sum(r[3] == "WIN" for r in rr); n = len(rr)
                    if n >= HOUR_MIN_N and wilson(w, n)[1] < be and h not in self.q["hour_unblock"]: out[h] = (w, n)
                self._hours = {"at": time.time(), "blocked": out}
            return self._hours["blocked"]
        except Exception as e: log.warning("hour block: %s", e); return {}
    # ---- (4)+(5) per-pattern live record ----
    def q_record(self, key, win, now=None):
        try:
            if not key: return
            now = now or self.d.now(); rec = self.q["pat"].setdefault(key, [])
            rec.append([now.isoformat(timespec="seconds"), 1 if win else 0]); del rec[:-3000]
            self.q_eval(key, now); self.q_save()
        except Exception as e: log.warning("q_record: %s", e)
    def q_eval(self, key, now):
        rec = self.q["pat"].get(key, []); w = sum(r[1] for r in rec); n = len(rec); be = 1 / (1 + C.DEFAULT_PAYOUT)
        if BAYES and n >= BAYES_MIN_N:
            p = beta_prob_above(BAYES_PRIOR + w, BAYES_PRIOR + n - w, be)
            if key not in self.q["paused"] and p < BAYES_PAUSE_P:
                self.q["paused"][key] = {"at": now.isoformat(timespec="seconds"), "p": round(p, 3), "rec": f"{w}W/{n - w}L"}
                self._q_alert(f"🧮 Pattern {key} paused for VIP/public/bug: P(win rate > BE {be:.1%}) = {p:.2f} < {BAYES_PAUSE_P} ({w}W/{n - w}L). Still tracked in shadow.")
            elif key in self.q["paused"] and p > BAYES_RESUME_P:
                self.q["paused"].pop(key); self._q_alert(f"🧮 Pattern {key} resumed: P(win rate > BE) = {p:.2f} > {BAYES_RESUME_P}.")
        if DECAY and key not in self.q["retired"]:
            cut = (now - timedelta(days=DECAY_DAYS)).isoformat(timespec="seconds")
            old = [r[1] for r in rec if r[0] < cut]; new = [r[1] for r in rec if r[0] >= cut]
            if len(new) >= DECAY_MIN_N and len(old) >= DECAY_MIN_N:
                base = sum(old) / len(old); wr = sum(new) / len(new); sd = math.sqrt(max(base * (1 - base), 1e-6) / len(new))
                if wr < base - DECAY_SD * sd:
                    self.q["retired"][key] = {"at": now.isoformat(timespec="seconds"), "recent": round(wr, 3), "baseline": round(base, 3), "n": len(new)}
                    self._q_alert(f"📉 Pattern {key} RETIRED (shadow-only): last {DECAY_DAYS}d {wr:.0%} over {len(new)} vs baseline {base:.0%} (> {DECAY_SD:g} SD below). /block remove {key} to un-retire.")
    def _q_alert(self, t):
        try: self.tg.admin(t); self.audit("quant", text=t)
        except Exception: pass
    # ---- combined gate for VIP/public/bug ----
    def q_block(self, pair, keys, now, x=None):
        """Reason string when an extra guard blocks VIP/public/bug, else None. Shadow is never blocked by this."""
        try:
            hb = self.q_hour_blocked(now); h = C.local(now).hour
            if h in hb: return f"hour {h:02d}:00 blocked (seasonality {hb[h][0]}W/{hb[h][1] - hb[h][0]}L, Wilson UB < BE)"
            for k in keys or []:
                if k in self.q["retired"]: return f"pattern {k} retired (decay)"
                if k in self.q["paused"]: return f"pattern {k} paused (Bayesian P(>BE) < {BAYES_PAUSE_P})"
            if ATR_THROTTLE and x is not None:
                pc = self.q_atr_pct(pair, float(x.atr.iloc[-1]))
                if pc is not None and (pc < ATR_LO or pc > ATR_HI): return f"ATR percentile {pc:.0f} outside {ATR_LO:g}–{ATR_HI:g}"
        except Exception as e: log.warning("q_block: %s", e)
        return None
    # ---- (1) correlation cap ----
    def q_cluster(self, pair):
        try:
            if time.time() - self._corr["at"] > 900:
                closes = {}
                for p in C.PAIRS:
                    df = self.cached_candles(p)
                    if df is not None and len(df): closes[p] = df.close
                self._corr = {"at": time.time(), "map": corr_clusters(closes)}
            return self._corr["map"].get(pair, pair)
        except Exception as e: log.warning("corr: %s", e); return pair
    def q_corr_take(self, pair, entry, channels):
        """Channels still free for this pair's correlation cluster in this entry minute (and reserve them)."""
        if not self.q.get("corr_on", CORR_CAP): return list(channels)
        cl = self.q_cluster(pair); m = entry.replace(second=0, microsecond=0); free = []
        for ch in channels:
            k = (m.isoformat(), ch[0], cl)
            if k in self._corr_slots: continue
            self._corr_slots[k] = pair; free.append(ch)
        if len(self._corr_slots) > 500: self._corr_slots = dict(list(self._corr_slots.items())[-200:])
        return free
    # ---- (7)+(8)+(9) reproducibility, regime, features ----
    def q_features(self, t, x=None, df=None, pattern=""):
        """Adds atr_pct/regime/hour to t and returns the feature dict (logged in audit 'created' + features.jsonl)."""
        try:
            now = self.d.now(); pc = self.q_atr_pct(t.get("pair"), float(x.atr.iloc[-1])) if x is not None and len(x) else None
            reg = regime_label(x, pc) if x is not None and len(x) else "unknown"; t["atr_pct"] = pc; t["regime_label"] = reg
            votes = {k: (v[0] if isinstance(v, (list, tuple)) else v) for k, v in (t.get("strategies") or t.get("votes") or {}).items()} if isinstance(t.get("strategies") or t.get("votes"), dict) else {}
            r = x.iloc[-1] if x is not None and len(x) else {}
            f = {"id": t.get("id"), "ts": now.isoformat(timespec="seconds"), "pair": t.get("pair"), "dir": t.get("signal") or t.get("dir"),
                 "pattern": pattern, "score": t.get("score"), "confidence": t.get("confidence"), "votes": votes, "regime": reg,
                 "atr_pct": pc, "hour": C.local(now).hour, "weekday": C.local(now).weekday(), "payout": t.get("payout"),
                 "adx": float(r.get("adx", 0) or 0) if len(r) else None, "er": float(r.get("er", 0) or 0) if len(r) else None,
                 "analyzer_version": ANALYZER_VERSION, "candles_sha256": candles_sha(df if df is not None else x) if (df is not None or x is not None) else ""}
            return f
        except Exception as e: log.warning("features: %s", e); return {}
    def q_feature_log(self, row):
        try:
            import configver as CV
            row = dict(row, config_version=CV.current())
            with open(os.path.join(C.STATE_DIR, "features.jsonl"), "a") as fh: fh.write(json.dumps(row, default=str) + "\n")
        except Exception as e: log.warning("features.jsonl: %s", e)
    def q_outcome(self, t, res, channel=""):
        self.q_feature_log({"type": "outcome", "id": t.get("id"), "ts": self.d.now().isoformat(timespec="seconds"), "result": res, "channel": channel,
                            "regime": t.get("regime_label")})
    # ---- commands / status ----
    def corrcap_cmd(self, args):
        a = (args[0].lower() if args else "")
        if a in ("on", "off"): self.q["corr_on"] = a == "on"; self.q_save()
        m = self._corr.get("map") or {}; cl = {}
        for p, c in m.items(): cl.setdefault(c, []).append(p)
        groups = " · ".join("+".join(v) for v in cl.values() if len(v) > 1) or "none yet"
        self.tg.admin(f"🔗 Correlation cap {'ON' if self.q['corr_on'] else 'OFF'} (corr > {CORR_THRESH} over {CORR_BARS} bars → 1 signal per cluster per minute per channel). Clusters: {groups}")
    def hours_cmd(self, args):
        a = [x.lower() for x in args]
        if len(a) >= 2 and a[0] == "unblock" and a[1].isdigit():
            h = int(a[1]) % 24
            if h not in self.q["hour_unblock"]: self.q["hour_unblock"].append(h)
            self._hours["at"] = 0; self.q_save(); self.tg.admin(f"🕐 Hour {h:02d}:00 unblocked (manual override)."); return
        if len(a) >= 2 and a[0] == "reblock" and a[1].isdigit():
            h = int(a[1]) % 24; self.q["hour_unblock"] = [x for x in self.q["hour_unblock"] if x != h]; self._hours["at"] = 0; self.q_save()
            self.tg.admin(f"🕐 Hour {h:02d}:00 auto-block allowed again."); return
        hb = self.q_hour_blocked(self.d.now())
        self.tg.admin(f"🕐 Hour auto-block {'ON' if HOUR_BLOCK else 'OFF'} (≥{HOUR_MIN_N} trades and Wilson UB < BE, VIP/public/bug, {C.TZ_LABEL}). Blocked: "
                      + (", ".join(f"{h:02d}:00 ({w}W/{n - w}L)" for h, (w, n) in sorted(hb.items())) or "none")
                      + (f" · manual unblocks: {sorted(self.q['hour_unblock'])}" if self.q["hour_unblock"] else "") + " · /hours unblock <h> · /hours reblock <h>")
    def q_line(self):
        try:
            hb = self.q_hour_blocked(self.d.now())
            return (f"Guards: corrcap {'on' if self.q.get('corr_on') else 'off'} · hours blocked {sorted(hb) or 'none'} · ATR {ATR_LO:g}–{ATR_HI:g} pct "
                    f"{'on' if ATR_THROTTLE else 'off'} · paused patterns {sorted(self.q['paused']) or 'none'} · retired {sorted(self.q['retired']) or 'none'} · promotion {MT_METHOD}")
        except Exception: return "Guards: n/a"

def walk_forward_ok(rows, table_fn, propose_fn, pay, news, holdout=WF_HOLDOUT):
    """Tune on the older (1-holdout) part, validate on the newest part. Returns (ok, detail, proposal_from_train)."""
    rows = sorted(rows, key=lambda r: r["t"]); cut = int(len(rows) * (1 - holdout))
    train, test = rows[:cut], rows[cut:]
    if len(train) < 700 or len(test) < 300: return False, "not enough rows for walk-forward", None
    pr = propose_fn(table_fn(train, pay, news))
    if not pr: return False, "no proposal on training window", None
    name = pr[0]; tv = {n: m for n, _, m in table_fn(test, pay, news)}
    base = tv.get(next(iter(tv))); cand = tv.get(name)
    if not cand or cand["n"] < 30: return False, f"'{name}' has too few held-out trades", pr
    ok = cand["exp"] > 0 and cand["exp"] > base["exp"]
    return ok, f"held-out {len(test)} bars: '{name}' E {cand['exp']:+.3f} vs baseline {base['exp']:+.3f}", pr
