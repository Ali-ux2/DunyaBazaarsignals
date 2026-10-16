"""Config versioning: STATE_DIR/config_versions.jsonl (append-only). The effective config (config.py values incl. runtime
changes + bot runtime settings) is hashed; on change a new version vN+1 is appended with ts + diff of changed keys.
Secret-looking keys are never stored. Never raises into trading."""
import os, json, hashlib, logging, threading
from datetime import datetime, timezone
import config as C
log = logging.getLogger("configver")
SECRET_WORDS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "PASS", "CRED", "AUTH", "API_ID", "HASH")
SKIP = {"STATE_DIR"}
CURRENT = {"version": None, "hash": None, "snap": None}
_lock = threading.Lock()
def path(): return os.path.join(C.STATE_DIR, "config_versions.jsonl")
def _simple(v):
    if isinstance(v, (bool, int, float, str)) or v is None: return v
    if isinstance(v, (list, tuple, set)): return [_simple(x) for x in (sorted(v, key=str) if isinstance(v, set) else v)]
    if isinstance(v, dict): return {str(k): _simple(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    return None
def is_secret(k): return any(w in k.upper() for w in SECRET_WORDS)
def snapshot(extra=None):
    snap = {}
    for k in dir(C):
        if not k.isupper() or k in SKIP or is_secret(k) or k.endswith(("_FILE", "_LOG", "_DIR", "_CSV")): continue
        v = getattr(C, k)
        if callable(v): continue
        sv = _simple(v)
        if sv is None and v is not None: continue
        snap[k] = sv
    for k, v in (extra or {}).items():
        if not is_secret(k): snap["runtime." + k] = _simple(v)
    return snap
def digest(snap): return hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()[:16]
def history(n=None):
    out = []
    try:
        with open(path(), encoding="utf-8") as f:
            for ln in f:
                try: out.append(json.loads(ln))
                except ValueError: pass
    except OSError: pass
    return out[-n:] if n else out
def _last_full():
    """Rebuild the latest full snapshot from history (v1 stores everything; later lines store diffs)."""
    snap = {}; last = None
    for r in history():
        if r.get("full"): snap = dict(r["full"])
        for k, (_, new) in (r.get("diff") or {}).items():
            if new is None and k in snap: snap.pop(k)
            elif new is not None: snap[k] = new
        last = r
    return last, snap
def check(extra=None, why="", now=None):
    """Returns the current version string ('v3'); appends a new version when the effective config changed."""
    try:
        with _lock:
            snap = snapshot(extra); h = digest(snap)
            if CURRENT["hash"] == h: return CURRENT["version"]
            if CURRENT["snap"] is None:
                last, prev = _last_full()
                if last: CURRENT.update(version=last["version"], hash=last["hash"], snap=prev)
                if last and last["hash"] == h: return CURRENT["version"]
            prev = CURRENT["snap"] or {}
            n = int(str(CURRENT["version"] or "v0")[1:]) + 1
            diff = {k: [prev.get(k), snap.get(k)] for k in sorted(set(prev) | set(snap)) if prev.get(k) != snap.get(k)}
            rec = {"version": f"v{n}", "ts": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), "hash": h, "why": why}
            if n == 1 or not prev: rec["full"] = snap; rec["diff"] = {}
            else: rec["diff"] = diff
            os.makedirs(C.STATE_DIR, exist_ok=True)
            with open(path(), "a", encoding="utf-8") as f: f.write(json.dumps(rec, default=str) + "\n"); f.flush()
            CURRENT.update(version=rec["version"], hash=h, snap=snap)
            return rec["version"]
    except Exception as e:
        log.warning("config version: %s", e); return CURRENT["version"] or "v?"
def current(): return CURRENT["version"] or "v?"
def history_text(n=10):
    rows = history(n)
    if not rows: return "⚙️ No config versions recorded yet."
    out = [f"⚙️ Config versions (latest {len(rows)}; current {current()})"]
    for r in reversed(rows):
        d = r.get("diff") or {}
        body = "initial snapshot (" + str(len(r.get("full") or {})) + " keys)" if r.get("full") else \
            "; ".join(f"{k}: {json.dumps(a, default=str)[:40]} → {json.dumps(b, default=str)[:40]}" for k, (a, b) in list(d.items())[:8]) + (f" (+{len(d) - 8} more)" if len(d) > 8 else "")
        out.append(f"{r['version']} · {r['ts'][:16].replace('T', ' ')}Z{(' · ' + r['why']) if r.get('why') else ''}\n  {body}")
    return "\n".join(out)
