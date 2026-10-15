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
