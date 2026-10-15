"""New-signal filters: loss-streak cooldown (live + bug group separately), session-transition blackouts (DST-aware via zoneinfo),
and a high-impact news blackout from the free ForexFactory weekly JSON (cached in STATE_DIR, fetched at most every 6 h; no OTCharts requests).
All of them block NEW signals only."""
from __future__ import annotations
import json, os, time, logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
import config as C
log = logging.getLogger("filters")
UTC = timezone.utc

# ---------------- loss-streak cooldown ----------------
class Streaks:
    def __init__(self, path=None):
        self.path = path or os.path.join(C.STATE_DIR, "streak.json")
        self.s = {"live": {"n": 0, "until": None}, "bug": {"n": 0, "until": None}}
        try: self.s.update(json.load(open(self.path)))
        except Exception: pass
    def save(self):
        try: json.dump(self.s, open(self.path, "w"))
        except Exception as e: log.warning("streak save: %s", e)
    def result(self, kind, res, now):
        """WIN resets, LOSS counts, TIE ignored. Returns the freeze end (datetime) when a cooldown starts."""
        st = self.s[kind]
        if res == "WIN": st["n"] = 0
        elif res == "LOSS":
            st["n"] += 1
            if st["n"] >= C.LOSS_STREAK_N:
                st["n"] = 0; until = now + timedelta(minutes=C.STREAK_COOLDOWN_MIN); st["until"] = until.isoformat(); self.save(); return until
        self.save(); return None
    def frozen(self, kind, now):
        u = self.s[kind].get("until")
        return bool(u) and now < datetime.fromisoformat(u)
    def expired(self, kind, now):
        """True once when a cooldown has just ended (clears it)."""
        u = self.s[kind].get("until")
        if u and now >= datetime.fromisoformat(u): self.s[kind]["until"] = None; self.save(); return True
        return False

# ---------------- session-transition blackouts ----------------
def parse_windows(spec):
    """'Europe/London@07:45-08:30,America/New_York@09:15-09:45,UTC@23:45-00:15' → [(tz, (h,m), (h,m))] (times in that zone's local clock)."""
    out = []
    for part in filter(None, (p.strip() for p in (spec or "").split(","))):
        tz, _, rng = part.partition("@"); a, b = rng.split("-")
        out.append((tz, tuple(map(int, a.split(":"))), tuple(map(int, b.split(":")))))
    return out
def windows_utc(spec, day):
    """Concrete UTC intervals for windows anchored on `day` (date) ±1 day, so cross-midnight windows are covered."""
    res = []
    for tz, a, b in parse_windows(spec):
        z = ZoneInfo(tz)
        for dd in (-1, 0, 1):
            d = day + timedelta(days=dd)
            s = datetime(d.year, d.month, d.day, a[0], a[1], tzinfo=z); e = datetime(d.year, d.month, d.day, b[0], b[1], tzinfo=z)
            if e <= s: e += timedelta(days=1)
            res.append((s.astimezone(UTC), e.astimezone(UTC), tz))
    return res
def session_blackout(now, spec=None):
    """(start, end, tz) of the active window, or None."""
    if not C.BLACKOUT_ON: return None
    for s, e, tz in windows_utc(spec or C.BLACKOUT_WINDOWS, now.astimezone(UTC).date()):
        if s <= now < e: return s, e, tz
    return None

# ---------------- news blackout ----------------
class News:
    def __init__(self, path=None, fetch=None):
        self.path = path or os.path.join(C.STATE_DIR, "ff_calendar.json"); self.fetch_fn = fetch or self._http
        self.events = []; self.fetched_at = 0.0; self.fail_alerted = False
        try:
            j = json.load(open(self.path)); self.events = j.get("events", []); self.fetched_at = j.get("fetched_at", 0.0)
        except Exception: pass
    @staticmethod
    def _http():
        r = requests.get(C.NEWS_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0 (otc-signal-bot)"}); r.raise_for_status(); return r.json()
    def refresh(self, admin=None, force=False):
        """At most every NEWS_REFRESH_H hours. On failure keep the last cache and alert the admin once."""
        if not C.NEWS_FILTER or (not force and time.time() - self.fetched_at < C.NEWS_REFRESH_H * 3600): return False
        try:
            raw = self.fetch_fn(); ev = []
            for e in raw:
                if str(e.get("impact", "")).lower() != "high": continue
                try: t = datetime.fromisoformat(e["date"]).astimezone(UTC)
                except Exception: continue
                ev.append({"t": t.isoformat(), "cur": str(e.get("country", "")).upper(), "title": e.get("title", "")})
            self.events, self.fetched_at, self.fail_alerted = ev, time.time(), False
            json.dump({"events": ev, "fetched_at": self.fetched_at}, open(self.path, "w")); return True
        except Exception as e:
            self.fetched_at = time.time() - (C.NEWS_REFRESH_H - 0.5) * 3600     # retry in ~30 min
            if admin and not self.fail_alerted:
                admin(f"⚠️ News feed unavailable ({e}); using the cached calendar ({len(self.events)} high-impact events)."); self.fail_alerted = True
            return False
    def blocking(self, pair, now):
        """Event dict blocking this pair now (currency in the pair, within ±NEWS_BLACKOUT_MIN), else None."""
        if not C.NEWS_FILTER: return None
        w = timedelta(minutes=C.NEWS_BLACKOUT_MIN); p = pair.upper()
        for e in self.events:
            t = datetime.fromisoformat(e["t"])
            if t - w <= now < t + w and e["cur"] and e["cur"] in p: return e
        return None
    def upcoming(self, now, hours=24):
        return [e for e in self.events if now - timedelta(minutes=C.NEWS_BLACKOUT_MIN) <= datetime.fromisoformat(e["t"]) <= now + timedelta(hours=hours)]
