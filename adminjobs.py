"""Admin-only scheduled jobs: weekly results report, daily state backup, VIP results promo (admin-approved).
Everything goes to the ADMIN private chat; the only channel post is a promo the admin explicitly approves."""
from __future__ import annotations
import json, os, time, zipfile, logging
from datetime import datetime, timedelta, timezone
import config as C
import feedback as FB
log = logging.getLogger("adminjobs")
UTC = timezone.utc
JOBS_FILE = lambda: os.path.join(C.STATE_DIR, "admin_jobs.json")
RESULT_KINDS = {"shadow_result": "shadow", "bug_group_result": "bug", "vip_result": "vip", "public_result": "public", "main_result": "vip"}
BACKUP_MAX_FILE = 40 * 1024 * 1024
SKIP_DIRS = {"backups", "charts", "__pycache__", "daily_backups"}
SECRET_HINTS = (".env", "secret", "token", "key", "cred")

def load():
    try: return json.load(open(JOBS_FILE()))
    except Exception: return {"promo_on": C.PROMO_DEFAULT_ON, "sent": {}, "promos": {}}
def save(j):
    tmp = JOBS_FILE() + ".tmp"
    with open(tmp, "w") as f: json.dump(j, f, default=str)
    os.replace(tmp, JOBS_FILE())

def _ts(r):
    try:
        t = datetime.fromisoformat(str(r.get("ts")))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except Exception: return None
def results(days, now):
    """(channel, pair, local_dt, 'WIN'|'LOSS'|'TIE') rows from the stored feedback log."""
    out, cut = [], now - timedelta(days=days)
    for r in FB.posted_only(FB.read(days + 1, now)):
        ch = RESULT_KINDS.get(r.get("kind")); res = str(r.get("result") or "").upper()
        if not ch or res not in ("WIN", "LOSS", "TIE"): continue
        t = _ts(r)
        if t is None or t < cut or t > now: continue
        out.append((ch, r.get("pair") or "?", C.local(t), res, r.get("config_version") or "v?"))
    return out

def _wr(w, l):
    return f"{w}W/{l}L" + (f" · {w/(w+l):.0%}" if w + l else "")
def _tally(rows, key):
    d = {}
    for row in rows:
        k = key(row); v = d.setdefault(k, [0, 0, 0]); v[{"WIN": 0, "LOSS": 1, "TIE": 2}[row[3]]] += 1
    return d
def weekly_text(now, payout=None):
    rows = results(7, now); be = 1 / (1 + (payout or C.DEFAULT_PAYOUT))
    lines = [f"📅 Weekly results (last 7 days, to {C.hm(now)} {C.TZ_LABEL} {C.local(now):%a %d %b}) · breakeven ≈{be:.1%}",
             "Private admin report · from stored feedback log · past results only, no guarantee"]
    if not rows: return "\n".join(lines + ["No stored results in the last 7 days."])
    lines.append("\n<b>By channel</b>")
    for ch in ("vip", "public", "bug", "shadow"):
        v = _tally([r for r in rows if r[0] == ch], lambda r: 0).get(0)
        lines.append(f"  {ch}: " + (_wr(v[0], v[1]) + (f" · {v[2]} tie" if v[2] else "") if v else "no results"))
    lines.append("\n<b>By pair</b> (all channels)")
    tp = _tally(rows, lambda r: r[1])
    for p, v in sorted(tp.items(), key=lambda kv: -(kv[1][0] / max(1, kv[1][0] + kv[1][1]))):
        lines.append(f"  {p}: {_wr(v[0], v[1])}" + (" ✅" if v[0] + v[1] and v[0] / (v[0] + v[1]) >= be else ""))
    try:
        import drawdown as DD
        lines += seasonality_lines(rows, be) + regime_lines(now, be) + score_lines(now, be) + window_lines(rows, be) + __import__('pairguard').expectancy_lines(now) + ["\n<b>Drawdown</b>"] + DD.lines(7, now)[1:]
    except Exception as e: log.warning("weekly extras: %s", e)
    lines.append("\n<b>By config version</b> (all channels)")
    for v_, t_ in sorted(_tally(rows, lambda r: r[4] if len(r) > 4 else "v?").items(), key=lambda kv: (len(kv[0]), kv[0])):
        lines.append(f"  {v_}: {_wr(t_[0], t_[1])}" + (" ✅" if t_[0] + t_[1] and t_[0] / (t_[0] + t_[1]) >= be else ""))
    lines.append(f"\n<b>By hour</b> ({C.TZ_LABEL}, all channels)")
    for h, v in sorted(_tally(rows, lambda r: r[2].hour).items()):
        lines.append(f"  {h:02d}:00 {_wr(v[0], v[1])}")
    real = [r for r in rows if r[0] in ("vip", "public", "bug")]
    if real:
        lines.append("\n<b>By pair</b> (VIP + public + bug only)")
        for p, v in sorted(_tally(real, lambda r: r[1]).items()): lines.append(f"  {p}: {_wr(v[0], v[1])}")
    return "\n".join(lines)

WD = "Mon Tue Wed Thu Fri Sat Sun".split()
def seasonality_lines(rows, be):
    out = [f"\n<b>Seasonality per channel</b> ({C.TZ_LABEL})"]
    for ch in ("vip", "public", "bug", "shadow"):
        rr = [r for r in rows if r[0] == ch]
        if not rr: continue
        hrs = _tally(rr, lambda r: r[2].hour); wds = _tally(rr, lambda r: r[2].weekday())
        out.append(f"  {ch} by hour: " + ", ".join(f"{h:02d} {v[0]}-{v[1]}" for h, v in sorted(hrs.items())))
        out.append(f"  {ch} by weekday: " + ", ".join(f"{WD[d]} {v[0]}-{v[1]}" for d, v in sorted(wds.items())))
    return out if len(out) > 1 else []
def regime_lines(now, be, days=7):
    """Join features.jsonl signal rows with their outcome rows by id → W/L per regime per channel."""
    p = os.path.join(C.STATE_DIR, "features.jsonl"); sig, res = {}, {}
    if not os.path.exists(p): return []
    cut = (now - timedelta(days=days)).isoformat()
    for ln in open(p):
        try: r = json.loads(ln)
        except ValueError: continue
        if str(r.get("ts", "")) < cut or not r.get("id"): continue
        (sig if r.get("type") == "signal" else res)[r["id"]] = r
    t = {}
    for i, r in res.items():
        s = sig.get(i); rg = (s or {}).get("regime") or r.get("regime") or "unknown"; ch = (s or {}).get("channel", "?")
        v = t.setdefault((ch, rg), [0, 0])
        if r.get("result") == "WIN": v[0] += 1
        elif r.get("result") == "LOSS": v[1] += 1
    if not t: return []
    return ["\n<b>By regime</b> (from features.jsonl)"] + [f"  {ch} · {rg}: {_wr(w, l)}" + (" ✅" if w + l and w / (w + l) >= be else "") for (ch, rg), (w, l) in sorted(t.items())]

def score_lines(now, be, days=7):
    """W/L by score bucket (7-vote scale = confidence × 7 / 100) per channel, from features.jsonl."""
    p = os.path.join(C.STATE_DIR, "features.jsonl"); sig, res = {}, {}
    if not os.path.exists(p): return []
    cut = (now - timedelta(days=days)).isoformat()
    for ln in open(p):
        try: r = json.loads(ln)
        except ValueError: continue
        if str(r.get("ts", "")) >= cut and r.get("id"): (sig if r.get("type") == "signal" else res)[r["id"]] = r
    t = {}
    for i, r in res.items():
        s = sig.get(i) or {}
        if s.get("confidence") is None: continue
        b = int(min(7, float(s["confidence"]) * 7 / 100)); v = t.setdefault((s.get("channel", "?"), b), [0, 0])
        if r.get("result") == "WIN": v[0] += 1
        elif r.get("result") == "LOSS": v[1] += 1
    if not t: return []
    return ["\n<b>By score</b> (7-vote scale)"] + [f"  {ch} · {b}–{b + 1}/7: {_wr(w, l)}" + (" ✅" if w + l and w / (w + l) >= be else "") for (ch, b), (w, l) in sorted(t.items())]

def window_lines(rows, be):
    """W/L per pair window and per pair inside each window (current schedule; enabled or not), VIP/public/bug + shadow."""
    try:
        import chanfilters as CFm
        j = {}
        try: j = json.load(open(os.path.join(C.STATE_DIR, "chanfilters.json")))
        except Exception: pass
        sch = j.get("schedule") or CFm.DEFAULTS["schedule"]; out = ["\n<b>By pair window</b> (" + C.TZ_LABEL + ")"]
        for ch_set, lab in ((("vip", "public"), "VIP+public"), (("shadow",), "shadow")):
            rr = [r for r in rows if r[0] in ch_set]
            for name, w in sch.items():
                inw = [r for r in rr if CFm.in_windows(r[2], CFm.parse_windows(w["time"]))]
                listed = [r for r in inw if CFm.norm_pair(r[1]) in [CFm.norm_pair(p) for p in w.get("pairs", [])]]
                if not inw: continue
                t = _tally(listed, lambda r: 0).get(0, [0, 0, 0]); tp = _tally(listed, lambda r: CFm.norm_pair(r[1]))
                out.append(f"  {lab} · {name} {w['time']}{'' if w.get('on') else ' (off)'}: {_wr(t[0], t[1])}"
                           + (" — " + ", ".join(f"{p} {v[0]}-{v[1]}" for p, v in sorted(tp.items())) if tp else ""))
        return out if len(out) > 1 else []
    except Exception as e: log.warning("window lines: %s", e); return []

def backup_zip(now):
    d = os.path.abspath(C.STATE_DIR); out_dir = os.path.join(d, "daily_backups"); os.makedirs(out_dir, exist_ok=True)
    for f in sorted(os.listdir(out_dir))[:-3]:   # keep the last 3 locally
        try: os.remove(os.path.join(out_dir, f))
        except OSError: pass
    path = os.path.join(out_dir, f"dbs_state_{C.local(now):%Y%m%d_%H%M}.zip"); n = skipped = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for f in files:
                p = os.path.join(root, f); low = f.lower()
                if any(h in low for h in SECRET_HINTS) or low.endswith(".tmp"): continue
                if not low.endswith((".json", ".jsonl", ".csv", ".txt")): continue
                if os.path.getsize(p) > BACKUP_MAX_FILE: skipped += 1; continue
                z.write(p, os.path.relpath(p, d)); n += 1
    return path, n, skipped

def promo_text(w, l):
    return f"📊 VIP results today: {w}W–{l}L ✅\nReal results, posted daily. Past results don't guarantee future ones.\n💎 Join VIP: @DBSdesk"

class AdminJobs:
    """Mixed into Bot (needs self.tg, self.d, self.command)."""
    def adminjobs_tick(self):
        now = self.d.now(); loc = C.local(now); j = load(); sent = j.setdefault("sent", {}); day = f"{loc:%Y-%m-%d}"; ch = False
        hm_ = loc.hour * 60 + loc.minute
        def due(key, h, m): return hm_ >= h * 60 + m and sent.get(key) != day
        if loc.weekday() == 6 and due("weekly", *C.WEEKLY_REPORT_AT):
            sent["weekly"] = day; ch = True; self.tg.admin(weekly_text(now))
        if due("backup", *C.BACKUP_AT):
            sent["backup"] = day; ch = True; self.backup_cmd([])
        if due("promo", *C.PROMO_AT):
            sent["promo"] = day; ch = True
            if j.get("promo_on", C.PROMO_DEFAULT_ON): save(j); self.promo_build(); j = load(); j["sent"] = sent
        for pid, p in list(j.get("promos", {}).items()):
            if p.get("state") == "pending" and time.time() - p.get("at", 0) > C.PROMO_EXPIRE_H * 3600:
                p["state"] = "expired"; ch = True; self.tg.admin(f"⌛ VIP promo {pid} expired unapproved — nothing posted.")
        if ch: save(j)
    def weekly_results_cmd(self): self.tg.admin(weekly_text(self.d.now()))
    def backup_cmd(self, args):
        try:
            path, n, skipped = backup_zip(self.d.now())
            ok = self.tg.admin_document(path, f"🗄 DBS state backup {C.hm(self.d.now())} {C.TZ_LABEL} · {n} files" + (f" · {skipped} oversized skipped" if skipped else "") + " · no secrets/env")
            if ok: self.tg.admin(f"🗄 Backup sent ({n} files). Keep it safe; it contains your stats, not secrets.")
            else: self.tg.admin(f"⚠️ Backup built ({n} files) but sending failed; kept at {path}")
        except Exception as e: log.warning("backup: %s", e); self.tg.admin(f"⚠️ Backup failed: {e}")
    def promo_build(self, force=False):
        now = self.d.now(); today = C.local(now).date()
        rows = [r for r in results(1, now) if r[0] == "vip" and r[2].date() == today]
        w = sum(r[3] == "WIN" for r in rows); l = sum(r[3] == "LOSS" for r in rows)
        if w + l == 0: self.tg.admin("📣 VIP promo: VIP had 0 trades today — skipped."); return None
        j = load(); pid = f"{today:%m%d}{int(time.time()) % 10000:04d}"; text = promo_text(w, l)
        j.setdefault("promos", {})[pid] = {"text": text, "at": time.time(), "state": "pending"}; save(j)
        self.tg.admin_kb("📣 VIP promo preview (public channel). Nothing posts unless you approve; expires in "
                         f"{C.PROMO_EXPIRE_H}h.\n\n{text}", [[("✅ Approve", f"p:approve {pid}"), ("❌ Skip", f"p:skip {pid}")]])
        return pid
    def promo_cb(self, arg):
        act, _, pid = arg.partition(" "); j = load(); p = j.get("promos", {}).get(pid)
        if not p or p.get("state") != "pending":
            self.tg.admin(f"📣 Promo {pid}: {'not found' if not p else 'already ' + p['state']} — nothing posted."); return
        if act == "approve":
            if time.time() - p["at"] > C.PROMO_EXPIRE_H * 3600: p["state"] = "expired"; save(j); self.tg.admin("⌛ Promo expired — nothing posted."); return
            if not C.PUBLIC_CHANNEL_ID: self.tg.admin("⚠️ PUBLIC_CHANNEL_ID not set — promo not posted."); return
            p["state"] = "posted"; save(j); self.tg.send(C.PUBLIC_CHANNEL_ID, p["text"], "public"); self.tg.admin("✅ Promo posted to public.")
        else: p["state"] = "skipped"; save(j); self.tg.admin("⏭ Promo skipped — nothing posted.")
    def promo_cmd(self, args):
        a = (args[0].lower() if args else "")
        if a in ("on", "off"):
            j = load(); j["promo_on"] = a == "on"; save(j); self.tg.admin(f"📣 Daily VIP promo preview: {a.upper()} ({C.PROMO_AT[0]:02d}:{C.PROMO_AT[1]:02d} {C.TZ_LABEL}, admin approval required)")
        elif a == "now": self.promo_build(force=True)
        else:
            j = load(); self.tg.admin(f"📣 Promo is {'ON' if j.get('promo_on', C.PROMO_DEFAULT_ON) else 'OFF'} · /promo on|off|now")
