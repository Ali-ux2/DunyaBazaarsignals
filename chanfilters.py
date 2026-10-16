"""Per-channel quality filters for VIP and public (bug only if configured): session windows, 15-min spacing,
minimum score, multi-timeframe (M5/M15 from stored 1-min candles, no API calls). A channel that fails is dropped from
that signal (it goes to shadow/paper); shadow is never filtered, only tagged with pass/fail per filter.
Toggles persist in STATE_DIR/chanfilters.json. Fail-closed: missing data = fail for VIP/public."""
import os, json, logging
from datetime import timedelta
import numpy as np, pandas as pd
import config as C
log = logging.getLogger("chanfilters")
E = os.environ.get
DEFAULTS = {
    "sessions_on": E("SESSIONS", "on") == "on", "windows": E("SESSION_WINDOWS", "10:00-11:30,16:30-19:00"),
    "sessions_bug": E("SESSIONS_BUG", "off") == "on",
    "schedule": {
        "london":  {"on": True,  "time": "10:00-11:30", "pairs": ["USDBRL", "USDMXN", "USDCOP", "USDARS"]},
        "ny_open": {"on": True,  "time": "16:30-18:00", "pairs": ["USDMXN", "USDBRL", "USDARS"]},
        "ny_late": {"on": True,  "time": "20:00-21:30", "pairs": ["USDMXN", "USDINR"]},
        "asian":   {"on": False, "time": "03:00-05:00", "pairs": ["USDINR", "USDPKR", "USDBDT"]}},
    "spacing_min": int(E("SPACING_MIN", "15")), "min_score": float(E("MIN_SCORE", "6")),
    "mtf_on": E("MTF", "on") == "on", "channels": ["vip", "public", "group"]}
FILTERS = ("session", "spacing", "min_score", "mtf")

def parse_windows(spec):
    out = []
    for part in filter(None, (p.strip() for p in str(spec).split(","))):
        a, b = part.split("-"); ha, ma = map(int, a.split(":")); hb, mb = map(int, b.split(":")); out.append((ha * 60 + ma, hb * 60 + mb))
    return out
def in_windows(local_dt, wins):
    m = local_dt.hour * 60 + local_dt.minute
    return any((a <= m < b) if a <= b else (m >= a or m < b) for a, b in wins)
def norm_pair(p):
    p = str(p).upper().replace("/", "").replace("_OTC", "").replace("-OTC", "")
    return p
def window_for(sched, local_dt, pair=None, enabled_only=True):
    """Name of the (enabled) window containing local_dt [and listing pair], else None."""
    for name, w in (sched or {}).items():
        if enabled_only and not w.get("on"): continue
        try:
            if not in_windows(local_dt, parse_windows(w["time"])): continue
        except Exception: continue
        if pair is None or norm_pair(pair) in [norm_pair(x) for x in w.get("pairs", [])]: return name
    return None
def score7(sig):
    """Map the bot's scoring to the 7-vote scale: confidence% × 7 / 100 (confidence = score / max_score). None if unscored."""
    c = sig.get("confidence")
    if c is None and sig.get("score") is not None and sig.get("max_score"): c = 100 * sig["score"] / sig["max_score"]
    return None if c is None else round(float(c) * 7 / 100, 2)
def tf_trend(df1, rule, need=25):
    """'up' / 'down' / 'flat' / None(insufficient). close vs EMA20 and EMA20 slope on the resampled timeframe (closed bars only)."""
    if df1 is None or len(df1) < 30: return None
    try:
        r = df1[["open", "high", "low", "close"]].resample(rule, label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        if len(r) > 1: r = r.iloc[:-1]   # drop the forming higher-TF bar
        if len(r) < need: return None
        ema = r.close.ewm(span=20, adjust=False).mean(); c, e, e0 = float(r.close.iloc[-1]), float(ema.iloc[-1]), float(ema.iloc[-3])
        return "up" if c > e and e > e0 else ("down" if c < e and e < e0 else "flat")
    except Exception as ex: log.warning("tf trend: %s", ex); return None

class ChanFilters:
    def cf_load(self):
        import copy
        try: self.cf = dict(copy.deepcopy(DEFAULTS), **json.load(open(os.path.join(C.STATE_DIR, "chanfilters.json"))))
        except Exception: self.cf = copy.deepcopy(DEFAULTS)
        self._cf_spacing = {}
    def cf_save(self):
        try:
            p = os.path.join(C.STATE_DIR, "chanfilters.json"); json.dump(self.cf, open(p + ".tmp", "w")); os.replace(p + ".tmp", p)
        except Exception as e: log.warning("chanfilters save: %s", e)
    def cf_mtf(self, pair, direction, df=None):
        try:
            base = self.hist.store.get(pair)
            if df is not None and len(df): base = df if base is None else pd.concat([base, df]).loc[lambda d: ~d.index.duplicated(keep="last")].sort_index()
            if base is None: return False, "no stored candles"
            base = base.iloc[-(15 * 40):]
            t5, t15 = tf_trend(base, "5min"), tf_trend(base, "15min"); want = "up" if direction == "CALL" else "down"
            if t5 is None or t15 is None: return False, f"insufficient M5/M15 candles (M5 {t5}, M15 {t15})"
            return (t5 == want and t15 == want), f"M5 {t5}, M15 {t15}"
        except Exception as e: return False, f"mtf error {e}"
    def cf_eval(self, sig, entry, df=None, kind="main"):
        """{filter: (ok, why)} for this signal (independent of channel except spacing, evaluated separately)."""
        loc = C.local(entry); out = {}
        try:
            if self.cf["sessions_on"]:
                wn = window_for(self.cf["schedule"], loc, sig.get("pair")); sig["window"] = wn or window_for(self.cf["schedule"], loc) or ""
                out["session"] = (wn is not None, f"{loc:%H:%M} {sig.get('pair')} → {wn or 'not in an enabled window for this pair'}")
            else: out["session"] = (True, "off")
        except Exception as e: out["session"] = (False, f"bad schedule {e}")
        s7 = score7(sig); ms = float(self.cf["min_score"])
        out["min_score"] = (s7 is not None and s7 >= ms, f"score {s7}/7 vs min {ms:g}") if ms > 0 else (True, "off")
        out["mtf"] = self.cf_mtf(sig.get("pair"), sig.get("signal") or sig.get("dir"), df) if self.cf["mtf_on"] else (True, "off")
        return out
    def cf_spacing_ok(self, ch, entry):
        sp = int(self.cf.get("spacing_min") or 0)
        if sp <= 0: return True
        blk = int(entry.timestamp() // (sp * 60)); return self._cf_spacing.get(ch) != blk
    def cf_apply(self, sig, chats, entry, df=None, kind="main"):
        """Returns (kept_chats, results). VIP/public(/legacy group) must pass all; bug only checks sessions when sessions_bug is on."""
        try:
            res = self.cf_eval(sig, entry, df, kind); kept = []; sig["cf"] = {k: bool(v[0]) for k, v in res.items()}
            for ch in chats:
                name = ch[0]
                if getattr(self, "dd_paused", {}).get("vip" if name == "group" else name) or (hasattr(self, "ddl_paused") and self.ddl_paused("vip" if name == "group" else name)): ok = False; res["drawdown"] = (False, f"{name} auto-paused by drawdown"); continue
                if name in self.cf["channels"] and hasattr(self, "pg_excluded") and self.pg_excluded(sig.get("pair")): res["pair_guard"] = (False, f"{sig.get('pair')} excluded (30d live below break-even)"); continue
                if name in self.cf["channels"]:
                    ok = all(v[0] for v in res.values()) and self.cf_spacing_ok(name, entry)
                    if not self.cf_spacing_ok(name, entry): res["spacing"] = (False, f"{name} already had a signal this {self.cf['spacing_min']}-min block")
                elif name == "bug" and self.cf["sessions_bug"]: ok = res["session"][0]
                else: ok = True
                if ok: kept.append(ch)
            res.setdefault("spacing", (True, "ok"))
            for ch in kept:
                if ch[0] in self.cf["channels"] and int(self.cf.get("spacing_min") or 0) > 0:
                    self._cf_spacing[ch[0]] = int(entry.timestamp() // (int(self.cf["spacing_min"]) * 60))
            return kept, res
        except Exception as e:
            log.warning("chanfilters: %s", e); return [c for c in chats if c[0] not in self.cf.get("channels", [])], {"error": (False, str(e))}   # fail closed for VIP/public
    def cf_tags(self, sig, entry, df=None):
        """Shadow: pass/fail of each VIP/public filter (never blocks)."""
        try:
            r = self.cf_eval(sig, entry, df); r["spacing"] = (self.cf_spacing_ok("vip", entry), "vip block")
            out = {k: bool(v[0]) for k, v in r.items()}
            out["in_window"] = window_for(self.cf["schedule"], C.local(entry), sig.get("pair")) is not None
            out["window"] = sig.get("window", "")
            return out
        except Exception: return {}
    def cf_line(self):
        c = self.cf
        on = [n for n, w in c["schedule"].items() if w.get("on")]
        return (f"VIP/public filters: windows {('on: ' + ', '.join(on)) if c['sessions_on'] else 'OFF'}{' (+bug)' if c['sessions_bug'] else ''} · spacing {c['spacing_min']}m · "
                f"min score {c['min_score']:g}/7 · MTF {'on' if c['mtf_on'] else 'off'} · freeze {os.environ.get('FREEZE_MIN_VIP', '120')}m VIP / {os.environ.get('FREEZE_MIN_PUBLIC', '120')}m public")
    # ---- commands ----
    def windows_text(self):
        c = self.cf; out = [f"🕰 VIP/public pair windows ({C.TZ_LABEL}) — filter {'ON' if c['sessions_on'] else 'OFF'}{' · also bug group' if c['sessions_bug'] else ''}"]
        for n, w in c["schedule"].items(): out.append(f"  {'✅' if w.get('on') else '⏸'} {n} {w['time']}: {', '.join(w.get('pairs', []))}")
        out.append("/windows on|off <name> · /windows set <name> HH:MM-HH:MM PAIR,PAIR · /windows off · /windows on · /windows bug on|off")
        return "\n".join(out)
    def windows_cmd(self, args):
        a = list(args); sch = self.cf["schedule"]
        if len(a) == 1 and a[0] in ("on", "off"): self.cf["sessions_on"] = a[0] == "on"
        elif len(a) >= 2 and a[0] == "bug": self.cf["sessions_bug"] = a[1] == "on"
        elif len(a) >= 2 and a[0] in ("on", "off"):
            if a[1] not in sch: self.tg.admin(f"Unknown window {a[1]}. " + ", ".join(sch)); return
            sch[a[1]]["on"] = a[0] == "on"
        elif len(a) >= 4 and a[0] == "set":
            name, tm = a[1], a[2]; pairs = [norm_pair(p) for p in ",".join(a[3:]).split(",") if p.strip()]
            try: parse_windows(tm)
            except Exception: self.tg.admin("Usage: /windows set <name> HH:MM-HH:MM PAIR,PAIR"); return
            sch[name] = {"on": sch.get(name, {}).get("on", True), "time": tm, "pairs": pairs}
        elif len(a) >= 2 and a[0] in ("del", "remove") and a[1] in sch: sch.pop(a[1])
        self.cf_save(); self.tg.admin(self.windows_text())
    def sessions_cmd(self, args): self.windows_cmd(args)   # legacy alias
    def spacing_cmd(self, args):
        if args and args[0].isdigit(): self.cf["spacing_min"] = int(args[0]); self.cf_save()
        self.tg.admin(f"↔️ VIP/public spacing: max 1 signal per {self.cf['spacing_min']} min each (0 = off). /spacing <min>")
    def minscore_cmd(self, args):
        try:
            if args: self.cf["min_score"] = max(0.0, min(7.0, float(args[0]))); self.cf_save()
        except ValueError: pass
        self.tg.admin(f"🎯 VIP/public min score {self.cf['min_score']:g}/7 (= confidence ≥ {self.cf['min_score'] / 7:.0%}). /minscore <0-7>")
    def mtf_cmd(self, args):
        if args and args[0] in ("on", "off"): self.cf["mtf_on"] = args[0] == "on"; self.cf_save()
        self.tg.admin(f"📐 Multi-timeframe (M5+M15 must agree, fail-closed): {'ON' if self.cf['mtf_on'] else 'OFF'}. /mtf on|off")
