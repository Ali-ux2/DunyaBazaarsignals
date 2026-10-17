"""Win/loss streak stats per stream (main = public+VIP shared, bug, shadow). Admin-only, read from a local ledger
(STATE_DIR/streaks/results.jsonl, one line per closed trade). No API requests. Draws (TIE/refund) neither extend nor break a streak."""
import os, json
from datetime import datetime, timedelta, timezone
import config as C
esc = lambda s: str(s)   # admin send() applies tghtml.sanitize
STREAMS = (("main", "Main (public+VIP)"), ("bug", "Bug group"), ("shadow", "Shadow"))
KEEP_DAYS = 31
def path():
    d = os.path.join(C.STATE_DIR, "streaks"); os.makedirs(d, exist_ok=True); return os.path.join(d, "results.jsonl")
def _norm(res):
    r = str(res).upper()
    return "W" if r == "WIN" else "L" if r == "LOSS" else "D" if r in ("TIE", "DRAW", "REFUND") else None
def record(stream, res, ts, pair=""):
    r = _norm(res)
    if r is None or stream not in dict(STREAMS): return
    try:
        with open(path(), "a") as f: f.write(json.dumps({"ts": ts.isoformat(), "s": stream, "r": r, "pair": pair}) + "\n")
    except Exception: pass
def load():
    out = []
    try:
        for line in open(path()):
            try: x = json.loads(line); x["t"] = datetime.fromisoformat(x["ts"]); out.append(x)
            except (ValueError, KeyError): pass
    except FileNotFoundError: pass
    return sorted(out, key=lambda x: x["t"])
def prune(now):
    rows = load(); cut = now - timedelta(days=KEEP_DAYS); keep = [r for r in rows if r["t"] >= cut]
    if len(keep) != len(rows):
        with open(path(), "w") as f:
            for r in keep: f.write(json.dumps({k: r[k] for k in ("ts", "s", "r", "pair") if k in r}) + "\n")
def trading_day_start(now):
    """Start of the current trading day (TRADING_DAY_START, UTC+3) as a UTC-aware/naive datetime matching `now`."""
    hh, mm = map(int, C.DAILY_STOP_RESUME.split(":")); loc = now + timedelta(hours=3)
    st = loc.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if loc < st: st -= timedelta(days=1)
    return st - timedelta(hours=3)
def compute(seq):
    """seq: 'W'/'L'/'D' in close order. Returns dict cur ('W3'/'L2'/'—'), max_w, max_l, n (W+L), losses, ll_n, ll_k."""
    wl = [r for r in seq if r in ("W", "L")]
    cur_r, cur_n, mw, ml, run_r, run_n = None, 0, 0, 0, None, 0
    for r in wl:
        run_n = run_n + 1 if r == run_r else 1; run_r = r
        if r == "W": mw = max(mw, run_n)
        else: ml = max(ml, run_n)
    cur = f"{run_r}{run_n}" if run_r else "—"
    pairs = [(a, b) for a, b in zip(wl, wl[1:]) if a == "L"]
    return {"cur": cur, "max_w": mw, "max_l": ml, "n": len(wl), "losses": wl.count("L"),
            "ll_n": len(pairs), "ll_k": sum(1 for _, b in pairs if b == "L"), "draws": seq.count("D")}
def stream_stats(rows, stream, now):
    rows = [r for r in rows if r["s"] == stream and r["t"] <= now]
    win = {"today": trading_day_start(now), "7d": now - timedelta(days=7), "30d": now - timedelta(days=30)}
    out = {k: compute([r["r"] for r in rows if r["t"] >= since]) for k, since in win.items()}   # rows keep given order (shadow: entry order)
    out["all"] = compute([r["r"] for r in rows]); return out
def shadow_rows(now):
    """Shadow stream from the feedback log: posted-only shadow_result rows, entry-time order, signal direction (draws ignored later)."""
    import feedback as FB
    out = []
    for e, ts, res, pair in FB.shadow_posted_seq(KEEP_DAYS, now):
        try: t = datetime.fromisoformat(ts.replace(" ", "T"))
        except ValueError: continue
        out.append({"ts": ts, "t": t, "s": "shadow", "r": _norm(res), "pair": pair})
    return out
def text(now, rows=None):
    rows = load() if rows is None else rows; tz = now.tzinfo
    try:   # shadow: rebuilt from posted feedback results (signal direction, entry order); legacy ledger only if the log has none
        sr = shadow_rows(now)
        if sr: rows = [r for r in rows if r.get("s") != "shadow"] + sr
    except Exception: pass
    rows = [dict(r, t=(r["t"] if (r["t"].tzinfo is None) == (tz is None) else (r["t"].replace(tzinfo=None) if tz is None else r["t"].replace(tzinfo=timezone.utc)))) for r in rows]
    L = [f"🔁 Streaks (admin only · draws ignored · trading day from {esc(C.DAILY_STOP_RESUME)} {esc(C.TZ_LABEL)})"]
    for key, lab in STREAMS:
        s = stream_stats(rows, key, now); a = s["30d"]
        if not a["n"]: L.append(f"  {esc(lab)}: no closed trades yet"); continue
        L.append(f"  {esc(lab)}: now {s['all']['cur']}")
        L.append("    longest W/L · " + " · ".join(f"{k} {s[k]['max_w']}/{s[k]['max_l']}" for k in ("today", "7d", "30d")))
        if a["ll_n"]:
            L.append(f"    30d loss after loss {a['ll_k']/a['ll_n']:.0%} (n={a['ll_n']}) vs overall loss {a['losses']/a['n']:.0%} (n={a['n']})")
        else: L.append(f"    30d loss after loss: n/a (no losses followed by a trade) · overall loss {a['losses']/a['n']:.0%} (n={a['n']})")
    return "\n".join(L)
