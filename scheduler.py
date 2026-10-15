"""Scheduled posts + event hooks (admin-managed; persisted in STATE_DIR/schedule.json; times are local UTC+3).
Types: text (HTML), sticker (file_id), photo (file_id[|caption]), forward / copy (from_chat_id:message_id), pin / repin (message_id[:silent])."""
from __future__ import annotations
import json, logging, os, time, threading
from datetime import timedelta
import requests
import config as C
import tghtml
log = logging.getLogger("sched")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
EVENTS = ("on_public_session_start", "on_public_session_close", "on_daily_target", "on_bug_signal_first_of_day")
TYPES = ("text", "sticker", "photo", "forward", "copy", "pin", "repin")
HELP = ("Use:\n/sched — list\n/sched add HH:MM <public|vip|bug|both|all> <daily|once YYYY-MM-DD|mon,tue,..> <text|sticker|photo|forward|copy|pin|repin> <payload|last>\n"
        "/sched on <event> <target> <type> <payload|last>   events: " + ", ".join(EVENTS) + "\n"
        "/sched del <id> · /sched test <id> · /sched pause <id> · /sched resume <id>\n"
        "Payloads: text = HTML · sticker/photo = file_id (photo: file_id|caption) · forward/copy = chat_id:message_id · pin = message_id[:silent]\n"
        "Times are UTC+3. 'last' = the last sticker/photo/forwarded post you sent me.")

class Scheduler:
    def __init__(self, admin, now, enabled=None, base=None):
        self.admin = admin; self.now = now; self.on = C.TELEGRAM_ENABLED if enabled is None else enabled
        self.base = base or f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}"
        self.path = os.path.join(C.STATE_DIR, "schedule.json"); self.calls = []; self._lock = threading.Lock()
        self._last_call = 0.0; self._chat_last = {}
        self.d = {"items": [], "events": [], "last": {}, "next_id": 1}
        try:
            with open(self.path) as f: self.d.update(json.load(f))
        except Exception: pass
    def save(self):
        try:
            with open(self.path + ".tmp", "w") as f: json.dump(self.d, f)
            os.replace(self.path + ".tmp", self.path)
        except Exception as e: log.warning("schedule save: %s", e)
    # ---------- Telegram API with rate limiting ----------
    def api(self, method, payload):
        """≥ SCHED_MIN_GAP_S between calls, ≥ 1 s per chat (Telegram: ~30 msg/s overall, ~20/min per group)."""
        with self._lock:
            cid = str(payload.get("chat_id")); now = time.monotonic()
            wait = max(self._last_call + C.SCHED_MIN_GAP_S - now, self._chat_last.get(cid, 0) + C.SCHED_PER_CHAT_GAP_S - now, 0)
            if wait: time.sleep(wait)
            self._last_call = self._chat_last[cid] = time.monotonic()
            self.calls.append((method, dict(payload)))
            if not self.on: log.info("[TG %s] %s", method, payload); return True, None
            try:
                r = requests.post(f"{self.base}/{method}", json=payload, timeout=15)
                if r.status_code == 429:
                    ra = (r.json().get("parameters") or {}).get("retry_after", 3); time.sleep(min(ra, 30))
                    r = requests.post(f"{self.base}/{method}", json=payload, timeout=15)
                j = r.json() if r.content else {}
                return (r.ok, None if r.ok else j.get("description", r.text[:200]))
            except Exception as e: return False, str(e)
    # ---------- targets ----------
    @staticmethod
    def chats(target):
        ids = {"public": C.PUBLIC_CHANNEL_ID, "vip": C.VIP_CHANNEL_ID, "bug": C.BUG_CHANNEL_ID}
        names = {"both": ["public", "vip"], "all": ["public", "vip", "bug"]}.get(target, [target])
        return [(n, ids.get(n)) for n in names]
    # ---------- sending ----------
    def send(self, it, why="schedule"):
        ok_all = True
        for name, cid in self.chats(it["target"]):
            if not cid: self.admin(f"⚠️ Scheduled #{it.get('id')} ({why}): {name.upper()}_CHANNEL_ID not set"); ok_all = False; continue
            t, p = it["type"], it["payload"]
            try:
                if t == "text": calls = [("sendMessage", {"text": tghtml.sanitize(p), "parse_mode": "HTML", "disable_web_page_preview": True})]
                elif t == "sticker": calls = [("sendSticker", {"sticker": p})]
                elif t == "photo":
                    fid, _, cap = p.partition("|"); calls = [("sendPhoto", dict(photo=fid, **({"caption": tghtml.sanitize(cap), "parse_mode": "HTML"} if cap else {})))]
                elif t in ("forward", "copy"):
                    fc, mid = p.rsplit(":", 1)
                    calls = [("forwardMessage" if t == "forward" else "copyMessage", {"from_chat_id": int(fc) if fc.lstrip("-").isdigit() else fc, "message_id": int(mid)})]
                elif t in ("pin", "repin"):
                    mid, _, flag = p.partition(":"); body = {"message_id": int(mid), "disable_notification": flag == "silent"}
                    calls = ([("unpinChatMessage", {"message_id": int(mid)})] if t == "repin" else []) + [("pinChatMessage", body)]
                else: raise ValueError(f"unknown type {t}")
            except Exception as e:
                self.admin(f"⚠️ Scheduled #{it.get('id')} bad payload ({e})"); return False
            for m, body in calls:
                ok, err = self.api(m, dict(chat_id=cid, **body))
                if not ok and m != "unpinChatMessage":
                    hint = " — make the bot an admin with 'Pin messages' permission" if "pin" in m.lower() and err and ("rights" in err or "admin" in err.lower()) else ""
                    self.admin(f"⚠️ Scheduled #{it.get('id')} {m} to {name.upper()} failed: {err}{hint}"); ok_all = False
        return ok_all
    # ---------- time triggers ----------
    @staticmethod
    def _due(it, loc):
        if it.get("paused") or it["hm"] != f"{loc:%H:%M}": return False
        w = it["when"]
        if w == "daily": return True
        if w.startswith("once:"): return w[5:] == f"{loc:%Y-%m-%d}"
        return DAYS[loc.weekday()] in w.split(",")
    def tick(self):
        loc = C.local(self.now()); key = f"{loc:%Y-%m-%d %H:%M}"; changed = False
        for it in self.d["items"]:
            if self._due(it, loc) and it.get("last_run") != key:
                it["last_run"] = key; changed = True; self.send(it)
                if it["when"].startswith("once:"): it["paused"] = True
        if changed: self.save()
    def fire(self, event):
        """Run event hooks; returns how many were configured (0 → caller posts its default message)."""
        its = [e for e in self.d["events"] if e["event"] == event and not e.get("paused")]
        for e in its: self.send(e, event)
        return len(its)
    # ---------- capture ----------
    def capture(self, m):
        """Admin sent a non-command message: remember stickers/photos/forwards for 'last'. Returns the reply text or None."""
        L = self.d["last"]; out = None
        fo = m.get("forward_origin") or {}
        if fo.get("type") == "channel" or m.get("forward_from_chat"):
            ch = (fo.get("chat") or m.get("forward_from_chat") or {}); mid = fo.get("message_id") or m.get("forward_from_message_id")
            L["post"] = f"{ch.get('id')}:{mid}"
            out = (f"📌 Captured channel post {L['post']} ({ch.get('title', '')}).\nUse: /sched add 10:00 public daily copy last · "
                   f"/sched add 10:00 public daily forward last · /sched on on_public_session_start public pin {mid}")
        if m.get("sticker"):
            L["sticker"] = m["sticker"]["file_id"]; out = f"🧷 Sticker file_id:\n<code>{L['sticker']}</code>\nUse: /sched add 10:00 public daily sticker last"
        elif m.get("photo"):
            L["photo"] = m["photo"][-1]["file_id"]; out = f"🖼 Photo file_id:\n<code>{L['photo']}</code>\nUse: /sched add 10:00 vip daily photo last"
        elif m.get("animation") or m.get("video") or m.get("document"):
            k = "animation" if m.get("animation") else ("video" if m.get("video") else "document")
            L[k] = m[k]["file_id"]; out = f"📎 {k} file_id:\n<code>{L[k]}</code> (send it as a forward/copy of a channel post to schedule it)"
        elif not out and m.get("message_id"):
            L["msg"] = f"{m['chat']['id']}:{m['message_id']}"; out = f"💬 Message ref {L['msg']}. Use: /sched add 10:00 public daily copy {L['msg']}"
        if out: self.save()
        return out
    def _resolve(self, typ, payload):
        if payload != "last": return payload
        L = self.d["last"]
        v = {"sticker": L.get("sticker"), "photo": L.get("photo"), "forward": L.get("post"), "copy": L.get("post") or L.get("msg"),
             "pin": (L.get("post") or "").split(":")[-1] or None, "repin": (L.get("post") or "").split(":")[-1] or None}.get(typ)
        if not v: raise ValueError(f"nothing captured for '{typ} last' — send/forward it to me first")
        return v
    # ---------- commands (raw text, case preserved) ----------
    def command(self, text):
        a = text.split(maxsplit=1)[1].split() if len(text.split()) > 1 else []
        try:
            if not a: return self.list_text()
            op = a[0].lower()
            if op == "add":
                hm, target, when = a[1], a[2].lower(), a[3].lower(); k = 4
                h, mi = map(int, hm.split(":")); assert 0 <= h < 24 and 0 <= mi < 60
                if when == "once": when = "once:" + a[4]; k = 5
                elif when != "daily": assert all(d in DAYS for d in when.split(",")), "days must be mon,tue,..."
                typ = a[k].lower(); assert typ in TYPES, f"type must be one of {TYPES}"
                payload = self._resolve(typ, text.split(maxsplit=k + 2)[k + 2] if len(text.split()) > k + 2 else "")
                assert payload, "payload missing"
                assert target in ("public", "vip", "bug", "both", "all"), "target must be public|vip|bug|both|all"
                it = dict(id=self.d["next_id"], hm=f"{h:02d}:{mi:02d}", target=target, when=when, type=typ, payload=payload, paused=False)
                self.d["items"].append(it); self.d["next_id"] += 1; self.save(); return f"✅ Added #{it['id']}: {self._fmt(it)}"
            if op == "on":
                ev, target, typ = a[1].lower(), a[2].lower(), a[3].lower()
                assert ev in EVENTS, f"event must be one of {EVENTS}"; assert typ in TYPES
                payload = self._resolve(typ, text.split(maxsplit=5)[5] if len(text.split()) > 5 else "")
                assert payload, "payload missing"
                it = dict(id=self.d["next_id"], event=ev, target=target, type=typ, payload=payload, paused=False)
                self.d["events"].append(it); self.d["next_id"] += 1; self.save(); return f"✅ Added event #{it['id']}: {self._fmt(it)}"
            if op in ("del", "test", "pause", "resume"):
                i = int(a[1]); it = next((x for x in self.d["items"] + self.d["events"] if x["id"] == i), None)
                if not it: return f"⚠️ No scheduled item #{i}"
                if op == "del":
                    for L in (self.d["items"], self.d["events"]):
                        if it in L: L.remove(it)
                    self.save(); return f"🗑 Deleted #{i}"
                if op == "test": ok = self.send(it, "test"); return f"{'✅' if ok else '⚠️'} Test #{i} sent" + ("" if ok else " with errors (see above)")
                it["paused"] = op == "pause"; self.save(); return f"{'⏸' if it['paused'] else '▶️'} #{i} {op}d"
            return HELP
        except (IndexError, ValueError, AssertionError) as e:
            return f"⚠️ {e or 'invalid /sched command'}\n{HELP}"
    @staticmethod
    def _fmt(it):
        p = it["payload"]; p = p if len(p) <= 40 else p[:37] + "..."
        head = it["event"] if "event" in it else f"{it['hm']} UTC+3 {it['when'].replace('once:', 'once ')}"
        return f"{head} → {it['target']} · {it['type']} · {p}" + (" · ⏸ paused" if it.get("paused") else "")
    def list_text(self):
        rows = ["🗓 Scheduled posts (UTC+3):"] + [f"  #{x['id']} {self._fmt(x)}" for x in self.d["items"]]
        if not self.d["items"]: rows.append("  none")
        rows.append("Event hooks:"); rows += [f"  #{x['id']} {self._fmt(x)}" for x in self.d["events"]]
        if not self.d["events"]: rows.append("  none (defaults: built-in session start/close messages)")
        rows.append("/sched help for syntax"); return "\n".join(rows)
