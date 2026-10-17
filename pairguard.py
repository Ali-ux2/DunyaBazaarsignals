"""Pair-level auto-removal for VIP/public, from the rolling 30-day live record (VIP + public + bug results; shadow shown
separately, never used for the decision). Exclude when n ≥ PG_MIN_N and Wilson UB < the pair's break-even from its actual
payouts; re-enable when Wilson LB ≥ BE − 0.02 or the last 30 trades are above BE. Shadow keeps tracking. No stake sizing."""
import os, json, logging, time
import config as C
import feedback as FB
from quant import wilson
log = logging.getLogger("pairguard")
MIN_N = int(os.environ.get("PG_MIN_N", "30")); ON = os.environ.get("PAIR_GUARD", "on") == "on"; MARGIN = float(os.environ.get("PG_REENABLE_MARGIN", "0.02"))
LIVE = {"vip_result": "live", "public_result": "live", "main_result": "live", "bug_group_result": "live", "shadow_result": "shadow"}

def _pay(r):
    try: p = float(r.get("payout") or C.DEFAULT_PAYOUT)
    except (TypeError, ValueError): p = C.DEFAULT_PAYOUT
    return p / 100 if p > 1.5 else p
def records(days=30, now=None):
    """{(src, pair): [(ts, win(bool), payout)]} for settled WIN/LOSS."""
    out = {}
    for r in FB.posted_only(FB.read(days, now)):
        src = LIVE.get(r.get("kind")); res = str(r.get("result") or "").upper()
        if not src or res not in ("WIN", "LOSS") or not r.get("pair"): continue
        out.setdefault((src, str(r["pair"]).upper()), []).append((str(r.get("ts")), res == "WIN", _pay(r)))
    for v in out.values(): v.sort()
    return out
def metrics(rows):
    n = len(rows); w = sum(1 for _, x, _ in rows if x); pay = sum(p for _, _, p in rows) / n if n else C.DEFAULT_PAYOUT
    be = 1 / (1 + pay); lb, ub = wilson(w, n); last = rows[-30:]; lw = sum(1 for _, x, _ in last if x)
    exp = sum((p if x else -1.0) for _, x, p in rows) / n if n else 0.0
    return {"n": n, "w": w, "wr": w / n if n else 0.0, "pay": pay, "be": be, "lb": lb, "ub": ub, "exp": exp,
            "last30": (lw / len(last)) if len(last) >= 30 else None}

class PairGuard:
    def pg_path(self): return os.path.join(C.STATE_DIR, "pairguard.json")
    def pg_load(self):
        try: return json.load(open(self.pg_path()))
        except Exception: return {}
    def pg_save(self, j):
        try: json.dump(j, open(self.pg_path() + ".tmp", "w")); os.replace(self.pg_path() + ".tmp", self.pg_path())
        except Exception as e: log.warning("pairguard save: %s", e)
    def pg_check(self, force=False):
        try:
            if not ON or (not force and time.time() - getattr(self, "_pg_at", 0) < 300): return
            self._pg_at = time.time(); j = self.pg_load(); rec = records(30, self.d.now()); changed = False
            for (src, pair), rows in rec.items():
                if src != "live": continue
                m = metrics(rows); s = j.setdefault(pair, {"excluded": False, "override": False})
                if not s["excluded"] and not s.get("override") and m["n"] >= MIN_N and m["ub"] < m["be"]:
                    s["excluded"] = True; changed = True
                    self._pg_alert(pair, f"⛔ {pair} removed from VIP/public: 30d live {m['w']}W/{m['n'] - m['w']}L ({m['wr']:.0%}), Wilson UB {m['ub']:.1%} < break-even {m['be']:.1%} (avg payout {m['pay']:.0%}). Shadow keeps tracking. /pairs enable {pair} to override.", m)
                elif s["excluded"] and (m["lb"] >= m["be"] - MARGIN or (m["last30"] is not None and m["last30"] > m["be"])):
                    s["excluded"] = False; changed = True
                    self._pg_alert(pair, f"✅ {pair} back in VIP/public: Wilson LB {m['lb']:.1%}" + (f", last 30 {m['last30']:.0%}" if m["last30"] is not None else "") + f" vs break-even {m['be']:.1%}.", m)
            if changed: self.pg_save(j)
        except Exception as e: log.warning("pairguard: %s", e)
    def _pg_alert(self, pair, text, m):
        try: self.tg.admin(text); self.audit("pair_guard", {"pair": pair}, wr=round(m["wr"], 4), be=round(m["be"], 4), n=m["n"], text=text)
        except Exception: pass
    def pg_excluded(self, pair):
        try: s = self.pg_load().get(str(pair).upper()) or {}; return ON and s.get("excluded") and not s.get("override")
        except Exception: return False
    def pairstats_cmd(self, args):
        rec = records(30, self.d.now()); j = self.pg_load(); pairs = sorted({p for _, p in rec} | set(C.PAIRS))
        out = [f"📊 Pair stats 30d (live = VIP+public+bug; payout-weighted break-even; E per $1) · guard {'ON' if ON else 'OFF'}, min {MIN_N} trades"]
        rows = []
        for p in pairs:
            lm = metrics(rec.get(("live", p), [])); sm = metrics(rec.get(("shadow", p), [])); s = j.get(p, {})
            st = "OVERRIDE" if s.get("override") else ("EXCLUDED" if s.get("excluded") else ("ok" if lm["n"] >= MIN_N else f"<{MIN_N}"))
            rows.append((lm["exp"] if lm["n"] else -9, f"  {p}: {lm['w']}W/{lm['n'] - lm['w']}L {lm['wr']:.0%} · BE {lm['be']:.1%} · E {lm['exp']:+.3f} · {st}"
                         + (f" · shadow {sm['w']}W/{sm['n'] - sm['w']}L E {sm['exp']:+.3f}" if sm["n"] else "")))
        out += [r for _, r in sorted(rows, key=lambda x: -x[0])]
        out.append("/pairs enable <PAIR> (override) · /pairs auto <PAIR> (back to automatic)"); self.tg.admin("\n".join(out))
    def pairs_cmd(self, args):
        a = list(args)
        if len(a) >= 2 and a[0] in ("enable", "auto"):
            p = a[1].upper(); j = self.pg_load(); s = j.setdefault(p, {"excluded": False, "override": False})
            if a[0] == "enable": s["override"] = True; s["excluded"] = False
            else: s["override"] = False
            self.pg_save(j); self.audit("pair_guard", {"pair": p}, action=a[0])
            self.tg.admin(f"{'✅ ' + p + ' enabled for VIP/public (manual override; auto-removal off for it)' if a[0] == 'enable' else '🔁 ' + p + ' back to automatic pair guard'}"); return
        self.pairstats_cmd([])

def expectancy_lines(now, days=7):
    rec = records(days, now); out = []
    for src, lab in (("live", "VIP+public+bug"), ("shadow", "shadow")):
        items = sorted(((p, metrics(r)) for (s, p), r in rec.items() if s == src), key=lambda x: -x[1]["exp"])
        if items: out.append(f"  {lab}: " + ", ".join(f"{p} E {m['exp']:+.2f} ({m['n']})" for p, m in items))
    return (["\n<b>Per-pair expectancy per $1</b> (actual payouts)"] + out) if out else []
