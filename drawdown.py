"""Flat-stake equity / drawdown per channel from the stored feedback results, using each trade's own payout
(WIN = +payout units, LOSS = -1, TIE = 0). Notional bank DD_BANK units (default 20). Private admin output only."""
import os, json, logging, time
from datetime import timedelta
import config as C
import feedback as FB
log = logging.getLogger("drawdown")
BANK = float(os.environ.get("DD_BANK", "20")); ALERT = float(os.environ.get("DD_ALERT", "-0.15"))
AUTOPAUSE = os.environ.get("DD_AUTOPAUSE", "off") == "on"; WINDOW = int(os.environ.get("DD_WINDOW", "20"))
LADDER = [float(x) for x in os.environ.get("DD_LADDER", "-0.10,-0.15,-0.20,-0.30,-0.40").split(",")]
LADDER_ON = os.environ.get("DD_LADDER_ON", "on") == "on"; LADDER_PAUSE_H = float(os.environ.get("DD_LADDER_PAUSE_H", "4"))
STEP_TEXT = ["consider reducing stake to 1.5%", "reduce stake to 1%", f"auto-paused {LADDER_PAUSE_H:g}h", "paused for the rest of the day (until 00:00 UTC+3)",
             "paused for the week (until /ddresume or Monday 00:00 UTC+3)"]
KINDS = {"shadow_result": "shadow", "bug_group_result": "bug", "vip_result": "vip", "public_result": "public", "main_result": "vip"}

def trades(days=30, now=None):
    out = {}
    for r in FB.read(days, now):
        ch = KINDS.get(r.get("kind")); res = str(r.get("result") or "").upper()
        if not ch or res not in ("WIN", "LOSS", "TIE"): continue
        try: pay = float(r.get("payout") or C.DEFAULT_PAYOUT)
        except (TypeError, ValueError): pay = C.DEFAULT_PAYOUT
        if pay > 1.5: pay /= 100.0
        out.setdefault(ch, []).append((str(r.get("ts")), res, pay))
    for v in out.values(): v.sort()
    return out
def curve(rows):
    """→ dict(n, pnl, max_dd, cur_dd, worst_window) in units."""
    eq = peak = 0.0; mdd = 0.0; pnl = []
    for _, res, pay in rows:
        x = pay if res == "WIN" else (-1.0 if res == "LOSS" else 0.0); pnl.append(x); eq += x
        peak = max(peak, eq); mdd = min(mdd, eq - peak)
    worst = None
    if len(pnl) >= WINDOW:
        s = sum(pnl[:WINDOW]); worst = s
        for i in range(WINDOW, len(pnl)): s += pnl[i] - pnl[i - WINDOW]; worst = min(worst, s)
    elif pnl: worst = sum(pnl)
    return {"n": len(pnl), "pnl": eq, "max_dd": mdd, "cur_dd": eq - peak, "worst_window": worst}
def stats(days=30, now=None): return {ch: curve(rows) for ch, rows in trades(days, now).items()}
def pct(u): return u / BANK
def lines(days=30, now=None):
    st = stats(days, now)
    out = [f"📉 Drawdown (flat 1u, actual payouts, bank {BANK:g}u, last {days}d)"]
    for ch in ("vip", "public", "bug", "shadow"):
        m = st.get(ch)
        if not m: out.append(f"  {ch}: no results"); continue
        ww = m["worst_window"]
        out.append(f"  {ch}: n {m['n']} · P&L {m['pnl']:+.2f}u · max DD {m['max_dd']:.2f}u ({pct(m['max_dd']):.0%}) · now {m['cur_dd']:.2f}u ({pct(m['cur_dd']):.0%})"
                   + (f" · worst {WINDOW}-trade {ww:+.2f}u ({pct(ww):.0%})" if ww is not None else ""))
    return out

class Drawdown:
    def dd_check(self, force=False):
        """VIP/public: alert (admin only) when worst-20 window or current DD < DD_ALERT × bank; optional auto-pause."""
        try:
            if not force and time.time() - getattr(self, "_dd_at", 0) < 300: return
            self._dd_at = time.time(); self.dd_paused = getattr(self, "dd_paused", {}); self._dd_alerted = getattr(self, "_dd_alerted", {})
            st = stats(30, self.d.now()); self.ddl_check(st)
            for ch in ("vip", "public"):
                m = st.get(ch)
                if not m: continue
                bad = [f"{lab} {pct(v):.0%}" for lab, v in (("worst 20-trade", m["worst_window"]), ("current DD", m["cur_dd"])) if v is not None and pct(v) <= ALERT]
                if not bad: self._dd_alerted.pop(ch, None); continue
                if time.time() - self._dd_alerted.get(ch, 0) < 6 * 3600: continue
                self._dd_alerted[ch] = time.time()
                act = ""
                if AUTOPAUSE and not self.dd_paused.get(ch): self.dd_paused[ch] = True; act = f" · {ch.upper()} AUTO-PAUSED (resume: /drawdown resume {ch})"
                self.tg.admin(f"⚠️ Drawdown alert {ch.upper()}: " + ", ".join(bad) + f" of {BANK:g}u bank (limit {ALERT:.0%}){act}")
                try: self.audit("drawdown_alert", channel=ch, issues=bad, autopause=bool(act))
                except Exception: pass
        except Exception as e: log.warning("dd check: %s", e)
    def drawdown_cmd(self, args):
        self.dd_paused = getattr(self, "dd_paused", {})
        if len(args) >= 2 and args[0] == "resume": self.dd_paused.pop(args[1], None)
        self.tg.admin("\n".join(lines(30, self.d.now())) + f"\nAlert ≤ {ALERT:.0%} of bank · auto-pause {'ON' if AUTOPAUSE else 'OFF'}"
                      + (f" · paused: {', '.join(k for k, v in self.dd_paused.items() if v)}" if any(self.dd_paused.values()) else ""))

def _ladder_path(): return os.path.join(C.STATE_DIR, "ddladder.json")
def ladder_load():
    try: return json.load(open(_ladder_path()))
    except Exception: return {}
def ladder_save(j):
    try: json.dump(j, open(_ladder_path() + ".tmp", "w")); os.replace(_ladder_path() + ".tmp", _ladder_path())
    except Exception as e: log.warning("ddladder save: %s", e)
def step_for(dd_pct):
    s = 0
    for i, th in enumerate(LADDER, 1):
        if dd_pct <= th: s = i
    return s
def pause_until(step, now):
    """UTC datetime the step's pause ends, or None (alert-only steps)."""
    from datetime import datetime
    loc = C.local(now); mid = (loc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if step == 3: return now + timedelta(hours=LADDER_PAUSE_H)
    if step == 4: return now + (mid - loc)
    if step >= 5:
        mon = (loc + timedelta(days=(7 - loc.weekday()) or 7)).replace(hour=0, minute=0, second=0, microsecond=0)
        return now + (mon - loc)
    return None

class DDLadder:
    """VIP/public drawdown ladder from the current drawdown (vs peak) of the 30-day flat-stake curve. Admin-only alerts, once per step."""
    def ddl_check(self, st=None):
        try:
            if not LADDER_ON: return
            now = self.d.now(); j = ladder_load(); st = st if st is not None else stats(30, now); ch_ = False
            for ch in ("vip", "public"):
                m = st.get(ch); s = j.setdefault(ch, {"step": 0, "until": None})
                if not m: continue
                cur = pct(m["cur_dd"]); step = step_for(cur)
                if step > s["step"]:
                    s["step"] = step; ch_ = True; u = pause_until(step, now)
                    if u and (not s.get("until") or u.isoformat() > s["until"]): s["until"] = u.isoformat()
                    self.tg.admin(f"🪜 Drawdown ladder {ch.upper()}: current drawdown {cur:.0%} of {BANK:g}u bank (step {step}, ≤ {LADDER[step - 1]:.0%}) → {STEP_TEXT[min(step, len(STEP_TEXT)) - 1]}."
                                  + (f" Until {C.hm(u)} {C.TZ_LABEL} {C.local(u):%a %d %b}. /ddresume {ch} to resume now." if u else ""))
                    try: self.audit("dd_ladder", channel=ch, step=step, dd_pct=round(cur, 4), until=s.get("until"))
                    except Exception: pass
                elif step < s["step"] and step == 0:
                    s["step"] = 0; ch_ = True   # recovered above the first rung: re-arm alerts (an active pause still runs to its end)
            if ch_: ladder_save(j)
        except Exception as e: log.warning("dd ladder: %s", e)
    def ddl_paused(self, ch, now=None):
        try:
            s = ladder_load().get(ch) or {}; u = s.get("until")
            if not u: return False
            from datetime import datetime
            return (now or self.d.now()) < datetime.fromisoformat(u)
        except Exception: return False
    def ddladder_cmd(self, args):
        j = ladder_load(); st = stats(30, self.d.now()); out = [f"🪜 Drawdown ladder {'ON' if LADDER_ON else 'OFF'} (VIP/public; % of {BANK:g}u bank from peak): "
                                                               + " · ".join(f"{th:.0%} {STEP_TEXT[i]}" for i, th in enumerate(LADDER[:len(STEP_TEXT)]))]
        for ch in ("vip", "public"):
            s = j.get(ch) or {}; m = st.get(ch); cur = pct(m["cur_dd"]) if m else 0.0
            out.append(f"  {ch}: drawdown {cur:.0%} · step {s.get('step', 0)} · " + (f"PAUSED until {s['until'][:16].replace('T', ' ')}Z" if self.ddl_paused(ch) else "active"))
        out.append("/ddresume vip|public"); self.tg.admin("\n".join(out))
    def ddresume_cmd(self, args):
        ch = (args[0] if args else "").lower()
        if ch not in ("vip", "public"): self.tg.admin("Usage: /ddresume vip|public"); return
        j = ladder_load(); s = j.setdefault(ch, {"step": 0, "until": None}); s["until"] = None; ladder_save(j)
        getattr(self, "dd_paused", {}).pop(ch, None)
        try: self.audit("dd_resume", channel=ch, step=s.get("step"))
        except Exception: pass
        self.tg.admin(f"▶️ {ch.upper()} resumed (ladder step stays {s.get('step', 0)}; the next deeper step will alert again).")

def score_split(now, days=1):
    """{channel: {'≥6': [w,l], '<6': [w,l], '6–7': [w,l]}} from features.jsonl (score = confidence × 7 / 100)."""
    p = os.path.join(C.STATE_DIR, "features.jsonl"); sig, res = {}, {}
    if not os.path.exists(p): return {}
    cut = (now - timedelta(days=days)).isoformat()
    for ln in open(p):
        try: r = json.loads(ln)
        except ValueError: continue
        if str(r.get("ts", "")) >= cut and r.get("id"): (sig if r.get("type") == "signal" else res)[r["id"]] = r
    out = {}
    for i, r in res.items():
        s = sig.get(i) or {}
        if s.get("confidence") is None or r.get("result") not in ("WIN", "LOSS"): continue
        sc = float(s["confidence"]) * 7 / 100; d = out.setdefault(s.get("channel", "?"), {"6–7": [0, 0], "<6": [0, 0]})
        d["6–7" if sc >= 6 else "<6"][0 if r["result"] == "WIN" else 1] += 1
    return out
