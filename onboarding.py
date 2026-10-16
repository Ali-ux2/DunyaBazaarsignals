"""Onboarding: pinned 'How DunyaBazaar works' post (admin Approve → public channel + pin; re-approve edits the pinned
message) and a private /start welcome for non-admin users (once per user per 24 h). Texts live in STATE_DIR and are
editable from the admin chat. No guarantee claims."""
import os, json, time, logging
import config as C
log = logging.getLogger("onboarding")
WELCOME_EVERY_S = 24 * 3600
DEFAULT_HOWITWORKS = (
    "📌 <b>How DunyaBazaar works</b>\n\n"
    "• Signals for <b>Quotex OTC, 1-minute (M1)</b> trades.\n"
    "• Each signal comes in 3 steps:\n"
    "  1️⃣ <b>Card</b>: pair, direction (CALL/PUT) and entry time.\n"
    "  2️⃣ <b>:57 line</b>: at second 57 we post ✅ CONFIRMED or 🔄 SWITCH. Enter only then.\n"
    "  3️⃣ <b>Result</b>: WIN / LOSS posted after the candle closes.\n"
    "• <b>No martingale (no MTG)</b>: one fixed-size trade per signal, never double up after a loss.\n"
    "• Signals are sent only when the payout is <b>77% or higher</b>.\n\n"
    "⚠️ <b>Risk warning:</b> trading is risky and you can lose money. There are no guarantees, and past results "
    "don't guarantee future results. Only trade money you can afford to lose.\n\n"
    "📩 Questions / VIP: @DBSdesk")
DEFAULT_WELCOME = (
    "👋 Welcome to <b>Dunya Bazaar Signals</b>!\n\n"
    "We post Quotex OTC 1-minute (M1) signals: a card, a :57 confirm line, then the result. No martingale, "
    "and only when the payout is 77% or higher.\n\n"
    "⚠️ Trading is risky. There are no guarantees, and past results don't guarantee future results.\n\n"
    "📩 Questions or VIP access: @DBSdesk")

def _p(name): return os.path.join(C.STATE_DIR, name)
def _read(name, default):
    try:
        with open(_p(name), encoding="utf-8") as f: t = f.read().strip()
        return t or default
    except OSError: return default
def _write(name, text):
    os.makedirs(C.STATE_DIR, exist_ok=True); tmp = _p(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: f.write(text.strip() + "\n")
    os.replace(tmp, _p(name))
def _jload(name):
    try:
        with open(_p(name)) as f: return json.load(f)
    except (OSError, ValueError): return {}
def _jsave(name, j):
    tmp = _p(name) + ".tmp"
    with open(tmp, "w") as f: json.dump(j, f)
    os.replace(tmp, _p(name))
def howitworks_text(): return _read("howitworks.txt", DEFAULT_HOWITWORKS)
def welcome_text(): return _read("welcome.txt", DEFAULT_WELCOME)

def _raw_arg(raw, cmd):
    """Text after '/cmd set ' with original case/newlines preserved."""
    t = raw.strip(); i = t.lower().find(" set")
    return t[i + 4:].strip() if i >= 0 else ""

class Onboarding:
    """Mixed into Bot (needs self.tg)."""
    def onboarding_raw(self, m):
        """raw_hook part: '/howitworks set <text>' and '/welcome set <text>' keep case and line breaks. True = handled."""
        t = (m.get("text") or "").strip(); low = t.lower()
        for cmd, fname, label in (("/howitworks", "howitworks.txt", "How it works"), ("/welcome", "welcome.txt", "Welcome")):
            if low.startswith(cmd + " set"):
                body = _raw_arg(t, cmd)
                if not body: self.tg.admin(f"Usage: {cmd} set <text> (line breaks kept)"); return True
                _write(fname, body)
                self.tg.admin(f"✏️ {label} text saved ({len(body)} chars). " + ("Send /howitworks to preview and approve." if cmd == "/howitworks" else "Send /welcome preview to see it."))
                return True
        return False
    def howitworks_cmd(self, args):
        a = (args[0].lower() if args else "")
        if a == "reset":
            try: os.remove(_p("howitworks.txt"))
            except OSError: pass
            self.tg.admin("↩️ How-it-works text reset to default."); return
        st = _jload("howitworks_state.json"); mid = st.get("message_id")
        self.tg.admin_kb("📌 <b>How it works</b> preview (PUBLIC channel, pinned). Nothing posts unless you approve."
                         + (f" Approve edits the pinned post (message {mid})." if mid else "") + "\n\n" + howitworks_text(),
                         [[("✅ Approve", "h:approve"), ("❌ Skip", "h:skip")]])
    def howitworks_cb(self, act):
        if act != "approve": self.tg.admin("⏭ How-it-works skipped — nothing posted."); return
        if not C.PUBLIC_CHANNEL_ID: self.tg.admin("⚠️ PUBLIC_CHANNEL_ID not set — nothing posted."); return
        text = howitworks_text(); chat = C.PUBLIC_CHANNEL_ID; st = _jload("howitworks_state.json")
        mid = st.get("message_id") if str(st.get("chat")) == str(chat) else None
        if mid:
            r = self.tg._kb_call("editMessageText", {"chat_id": chat, "message_id": mid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
            if r is not None or not self.tg.on:
                self.tg._kb_call("pinChatMessage", {"chat_id": chat, "message_id": mid, "disable_notification": True})
                self.tg.admin(f"✅ Pinned how-it-works post updated (message {mid})."); return
            self.tg.admin("ℹ️ Couldn't edit the old pinned post (deleted or too old) — posting a new one.")
        r = self.tg._kb_call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        new = (r or {}).get("message_id")
        if r is None and self.tg.on: self.tg.admin("⚠️ Posting to public failed — check the bot can post there."); return
        _jsave("howitworks_state.json", {"chat": str(chat), "message_id": new, "at": time.time()})
        pin = self.tg._kb_call("pinChatMessage", {"chat_id": chat, "message_id": new, "disable_notification": True})
        if pin is None and self.tg.on:
            self.tg.admin("⚠️ Posted to public, but PIN FAILED. Make the bot an admin of the public channel with the "
                          "'Pin messages' (channels: 'Edit messages of others') right, then approve again — it will pin the same post.")
        else: self.tg.admin(f"✅ How-it-works posted to public and pinned (message {new}).")
    def welcome_cmd(self, args):
        a = (args[0].lower() if args else "preview")
        if a == "reset":
            try: os.remove(_p("welcome.txt"))
            except OSError: pass
            self.tg.admin("↩️ Welcome text reset to default."); return
        if a == "preview": self.tg.admin("👋 Welcome preview (sent to users who /start the bot, once per 24 h):\n\n" + welcome_text()); return
        self.tg.admin("Usage: /welcome preview · /welcome set <text> · /welcome reset")
    def welcome_user(self, m, now_s=None):
        """Non-admin private /start → welcome text (once per user per 24 h). Returns True if sent."""
        try:
            chat = m.get("chat") or {}; uid = str((m.get("from") or {}).get("id") or chat.get("id") or "")
            if chat.get("type") != "private" or not uid or uid == str(C.TELEGRAM_ADMIN_ID): return False
            if not (m.get("text") or "").strip().lower().startswith("/start"): return False
            now_s = now_s or time.time(); j = _jload("welcome_seen.json")
            if uid in j and now_s - j[uid] < WELCOME_EVERY_S: return False
            j[uid] = now_s; j = {k: v for k, v in j.items() if now_s - v < 7 * 86400}; _jsave("welcome_seen.json", j)
            self.tg.send(chat.get("id"), welcome_text(), "welcome"); return True
        except Exception as e: log.warning("welcome: %s", e); return False
