"""Persistent store for suppressed feedback (drops, filtered, paper/shadow/warm-up notes, results): STATE_DIR/feedback/YYYY-MM-DD.jsonl,
30-day rotation, CSV/JSON export for /feedback export."""
import os, json, csv, glob
from datetime import datetime, timedelta, timezone
import config as C
FIELDS = ["ts", "kind", "source", "pair", "dir", "text"]
def fdir(): d = os.path.join(C.STATE_DIR, "feedback"); os.makedirs(d, exist_ok=True); return d
def log(kind, text, source="", pair="", dir="", now=None, **extra):
    now = now or datetime.now(timezone.utc)
    rec = {"ts": str(now), "kind": kind, "source": source, "pair": pair, "dir": dir, "text": text, **extra}
    try:
        with open(os.path.join(fdir(), f"{now:%Y-%m-%d}.jsonl"), "a") as f: f.write(json.dumps(rec, default=str) + "\n")
    except Exception: pass
    return rec
def rotate(now=None, keep_days=30):
    cut = f"{(now or datetime.now(timezone.utc)) - timedelta(days=keep_days):%Y-%m-%d}"
    for p in glob.glob(os.path.join(fdir(), "*.jsonl")):
        if os.path.basename(p)[:10] < cut: os.remove(p)
def files(): return sorted(glob.glob(os.path.join(fdir(), "*.jsonl")))
def read(days=1, now=None):
    now = now or datetime.now(timezone.utc); cut = f"{now - timedelta(days=days - 1):%Y-%m-%d}"; out = []
    for p in files():
        if os.path.basename(p)[:10] >= cut:
            for line in open(p):
                try: out.append(json.loads(line))
                except ValueError: pass
    return out
def capped_ids(rows):
    """Ids of shadow signals that lost the one-per-minute pick (never posted): from shadow_capped / shadow_capped_result rows."""
    return {str(r.get("id")) for r in rows if r.get("kind") in ("shadow_capped", "shadow_capped_result") and r.get("id")}
def is_capped(r, ids):
    c = r.get("capped")
    if c is True or str(c).lower() == "true": return True
    return bool(r.get("id")) and str(r.get("id")) in ids
def posted_only(rows, ids=None):
    """Drop capped (unposted) shadow_result rows; all other rows unchanged. Works on old rows via id matching."""
    ids = capped_ids(rows) if ids is None else ids
    return [r for r in rows if not (r.get("kind") == "shadow_result" and (is_capped(r, ids) or is_lowscore(r)))]
def is_lowscore(r):
    v = r.get("lowscore"); return v is True or str(v).lower() == "true"
def shadow_posted_seq(days=31, now=None):
    """Posted shadow results (signal direction, as in the export / shadow stats) in entry-time order: [(entry_ts_str, settle_ts_str, result, pair)]."""
    rows = [r for r in posted_only(read(days, now)) if r.get("kind") == "shadow_result" and r.get("result") in ("WIN", "LOSS", "TIE")]
    out = [(str(r.get("entry") or r.get("ts")), str(r.get("ts")), r["result"], r.get("pair", "")) for r in rows]
    return sorted(out)
def counts(days=1, now=None):
    c = {}
    for r in read(days, now): c[r["kind"]] = c.get(r["kind"], 0) + 1
    return c
def export(days=1, now=None):
    """Writes CSV + JSON under STATE_DIR/feedback/exports; returns (csv_path, json_path, n)."""
    rows = read(days, now); now = now or datetime.now(timezone.utc); ed = os.path.join(fdir(), "exports"); os.makedirs(ed, exist_ok=True)
    base = os.path.join(ed, f"feedback_{days}d_{now:%Y%m%d_%H%M}"); keys = FIELDS + sorted({k for r in rows for k in r} - set(FIELDS))
    with open(base + ".csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); [w.writerow({k: r.get(k, "") for k in keys}) for r in rows]
    with open(base + ".json", "w") as f: json.dump(rows, f, default=str, indent=1)
    return base + ".csv", base + ".json", len(rows)
