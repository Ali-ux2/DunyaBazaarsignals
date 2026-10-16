"""Append-only, hash-chained audit log: STATE_DIR/audit/audit-YYYY-MM-DD.jsonl (UTC day), 90-day retention.
Each line: ts, id, event, pair, dir, channel, pattern, score, payout, details, prev_hash, hash.
hash = sha256(prev_hash + canonical JSON of the line without 'hash'). The chain runs across day files, so editing,
deleting or reordering any line is detected by verify(). Writes never raise (trading must not depend on the audit)."""
import os, json, glob, hashlib, logging, threading, uuid
from datetime import datetime, timedelta, timezone
import config as C
log = logging.getLogger("audit")
GENESIS = "0" * 64
KEEP_DAYS = 90
FIELDS = ["ts", "id", "event", "pair", "dir", "channel", "pattern", "score", "payout", "details"]
_lock = threading.Lock()
_last = {"dir": None, "hash": None}

def _cfgver():
    try:
        import configver; return configver.current()
    except Exception: return "v?"
def adir():
    d = os.path.join(C.STATE_DIR, "audit"); os.makedirs(d, exist_ok=True); return d
def files(): return sorted(glob.glob(os.path.join(adir(), "audit-????-??-??.jsonl")))
_used = {"day": None, "ids": set()}
def new_id(now=None):
    """DBS-YYYYMMDD-XXXX (4 upper hex, unique within the UTC day; checked against today's file after a restart)."""
    now = now or datetime.now(timezone.utc); day = f"{now:%Y%m%d}"
    with _lock:
        if _used["day"] != day:
            _used["day"], _used["ids"] = day, set()
            try:
                p = os.path.join(adir(), f"audit-{now:%Y-%m-%d}.jsonl")
                if os.path.exists(p):
                    for ln in open(p, encoding="utf-8"):
                        try: _used["ids"].add(json.loads(ln).get("id"))
                        except ValueError: pass
            except Exception: pass
        for _ in range(200):
            sid = f"DBS-{day}-{uuid.uuid4().hex[:4].upper()}"
            if sid not in _used["ids"]: _used["ids"].add(sid); return sid
        sid = f"DBS-{day}-{uuid.uuid4().hex[:6].upper()}"; _used["ids"].add(sid); return sid
def _canon(rec): return json.dumps(rec, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
def _digest(prev, rec): return hashlib.sha256((prev + _canon(rec)).encode("utf-8")).hexdigest()

def _tail_hash():
    """Hash of the newest line on disk (survives restarts); GENESIS for an empty log."""
    for p in reversed(files()):
        try:
            with open(p, "rb") as f:
                f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 65536)); lines = f.read().splitlines()
            for ln in reversed(lines):
                try: return json.loads(ln)["hash"]
                except Exception: continue
        except OSError: continue
    return GENESIS

def event(ev, id="", pair="", dir="", channel="", pattern="", score=None, payout=None, details=None, now=None):
    """Append one event. Returns the record, or None on failure (logged as warning, never raised)."""
    try:
        now = now or datetime.now(timezone.utc)
        ts = now.astimezone(timezone.utc) if getattr(now, "tzinfo", None) else now.replace(tzinfo=timezone.utc)
        rec = {"ts": ts.isoformat(timespec="seconds"), "id": id or "", "event": ev, "pair": pair or "", "dir": dir or "",
               "channel": channel or "", "pattern": pattern or "", "score": score, "payout": payout, "details": details or {},
               "config_version": _cfgver()}
        with _lock:
            d = adir()
            if _last["dir"] != d or _last["hash"] is None: _last["dir"], _last["hash"] = d, _tail_hash()
            rec["prev_hash"] = _last["hash"]; rec["hash"] = _digest(rec["prev_hash"], rec)
            line = _canon(rec) + "\n"
            with open(os.path.join(d, f"audit-{ts:%Y-%m-%d}.jsonl"), "a", encoding="utf-8") as f:
                f.write(line); f.flush()
                try: os.fsync(f.fileno())
                except OSError: pass
            _last["hash"] = rec["hash"]
        return rec
    except Exception as e:
        log.warning("audit write failed (%s): %s", ev, e); _last["hash"] = None; return None

def rotate(now=None, keep_days=KEEP_DAYS):
    """Delete whole day files older than keep_days (never edits a file). verify() anchors on the oldest kept line."""
    try:
        cut = f"audit-{(now or datetime.now(timezone.utc)) - timedelta(days=keep_days):%Y-%m-%d}"
        for p in files():
            if os.path.basename(p) < cut: os.remove(p)
    except Exception as e: log.warning("audit rotate: %s", e)

def _iter(paths):
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for i, ln in enumerate(f, 1):
                if ln.strip(): yield os.path.basename(p), i, ln

def verify():
    """(ok, n_lines, message). Checks every line's hash and the prev_hash link across all kept files."""
    prev = None; n = 0
    for name, i, ln in _iter(files()):
        try: rec = json.loads(ln)
        except ValueError: return False, n, f"BROKEN at {name}:{i} (not valid JSON)"
        h = rec.pop("hash", None)
        if prev is not None and rec.get("prev_hash") != prev: return False, n, f"BROKEN at {name}:{i} (prev_hash does not match previous line)"
        if h != _digest(rec.get("prev_hash", ""), rec): return False, n, f"BROKEN at {name}:{i} (line content changed)"
        prev = h; n += 1
    return True, n, f"chain OK ({n} lines, {len(files())} files)"

def read(days=None, now=None, sid=None):
    now = now or datetime.now(timezone.utc); out = []
    cut = f"audit-{now - timedelta(days=days - 1):%Y-%m-%d}" if days else ""
    for name, _, ln in _iter([p for p in files() if os.path.basename(p) >= cut]):
        try: r = json.loads(ln)
        except ValueError: continue
        if sid is None or r.get("id") == sid: out.append(r)
    return out

def trail(sid): return read(sid=(sid or "").upper())

def export(days=1, now=None):
    """Concatenate the raw lines (chain intact) of the last `days` UTC days into one file; returns (path, n)."""
    now = now or datetime.now(timezone.utc); ed = os.path.join(adir(), "exports"); os.makedirs(ed, exist_ok=True)
    cut = f"audit-{now - timedelta(days=days - 1):%Y-%m-%d}"; path = os.path.join(ed, f"audit_{days}d_{now:%Y%m%d_%H%M}.jsonl"); n = 0
    with open(path, "w", encoding="utf-8") as out:
        for _, _, ln in _iter([p for p in files() if os.path.basename(p) >= cut]): out.write(ln if ln.endswith("\n") else ln + "\n"); n += 1
    return path, n

def trail_text(sid):
    rows = trail(sid)
    if not rows: return f"🔎 No audit events for id {sid}"
    r0 = rows[0]; lines = [f"🔎 Audit trail {sid} · {r0.get('pair')} {r0.get('dir')} · {r0.get('pattern') or ''}".rstrip()]
    for r in rows:
        det = r.get("details") or {}
        extra = " ".join(f"{k}={v}" for k, v in det.items() if v not in (None, "", [], {}))
        lines.append(f"{r['ts'][11:19]}Z {r['event']}" + (f" [{r['channel']}]" if r.get("channel") else "")
                     + (f" payout {r['payout']:.0%}" if isinstance(r.get("payout"), (int, float)) else "") + (f" · {extra}" if extra else ""))
    return "\n".join(lines)
