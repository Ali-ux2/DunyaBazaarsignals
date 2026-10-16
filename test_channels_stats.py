"""Public/VIP switch (both ids, one id, none), trade pinned to its channel, /stats read-only, :57 switch threshold."""
import os, sys, tempfile, importlib, json
from datetime import timedelta
def fresh(env):
    for k in ("PUBLIC_CHANNEL_ID", "VIP_CHANNEL_ID", "TELEGRAM_CHANNEL_ID"): os.environ.pop(k, None)
    os.environ.update(SESSIONS="off", MTF="off", MIN_SCORE="0", SPACING_MIN="0", STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", **env)
    for m in ("config", "analyzer", "chart", "resolver", "bot"): sys.modules.pop(m, None)
    import bot; return bot
def mkbot(bot):
    b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); out = []
    b.tg.send = lambda chat, text, label="group": out.append((label, chat, text.split("\n")[0][:70]))
    b.tg.on = True                     # route through send() so we can see chat ids
    import requests; requests.post = lambda *a, **k: type("R", (), {"ok": False, "status_code": 0, "text": "mock"})()
    return b, out

# 1) both ids
bot = fresh({"PUBLIC_CHANNEL_ID": "-100PUB", "VIP_CHANNEL_ID": "-100VIP"}); b, out = mkbot(bot)
assert b.target == "vip", b.target
b.command("/both"); b.tg.group("x"); print("both ->", [o[:2] for o in out[-3:]])
assert [o[1] for o in out[-2:]] == ["-100PUB", "-100VIP"]
b.command("/public"); assert out[-1][2] == "📢 Posting to: PUBLIC", out[-1]
# restart keeps choice
b2, out2 = mkbot(bot); assert b2.target == "public"; print("persisted after restart:", b2.target, json.load(open(bot.C.CHANNEL_TARGET_FILE)))
# trade pinned: signal card to PUBLIC, switch to VIP mid-trade, :57 + result still PUBLIC
b.command("/public"); b.command("/publiccap start 00:00"); d = b.d; out.clear()
for _ in range(400):
    b.tick()
    if b.pending: break
    d.advance()
assert b.pending, "no signal in replay"
card = [o for o in out if o[0] in ("public", "vip")]; print("card ->", card[-1][:2])
b.command("/vip")
saved = d.clock; d.clock = b.pending["precheck_at"]; b.run_precheck(); d.clock = saved
while b.pending: d.advance(); b.tick()
grp = [o for o in out if o[0] in ("public", "vip")]
print("all group posts of that trade:", [(o[0], o[2][:30]) for o in grp])
assert all(o[0] == "public" for o in grp), grp
out.clear(); b.test_card(); assert any(o[0] == "vip" for o in out), out; print("/test after /vip ->", [o[0] for o in out])
# 2) only VIP via legacy TELEGRAM_CHANNEL_ID
bot = fresh({"TELEGRAM_CHANNEL_ID": "-100OLD"}); b, out = mkbot(bot)
assert b.target == "vip" and b.tg.chats == [("vip", "-100OLD")]
b.command("/public"); print("one id, /public ->", out[-1][2]); assert out[-1][2].startswith("⚠️ Can't switch to PUBLIC") and b.target == "vip"
b.command("/both"); assert out[-1][2].startswith("⚠️") and b.target == "vip"
b.command("/channel"); print("/channel ->", out[-1][2])
# only PUBLIC
bot = fresh({"PUBLIC_CHANNEL_ID": "-100PUB"}); b, out = mkbot(bot)
assert b.target == "public"; b.command("/vip"); assert out[-1][2].startswith("⚠️") and b.target == "public"; print("only public: default", b.target)
# 3) none
bot = fresh({}); b, out = mkbot(bot); b.tg.group("hi"); print("none ->", b.target, out[-1][:2]); assert b.target is None
b.command("/vip"); assert out[-1][2].startswith("⚠️")
# 4) /stats read-only + /help
bot = fresh({"VIP_CHANNEL_ID": "-100VIP"}); b, out = mkbot(bot)
b.day["USDINR"] = [2, 1, 0]; b.switches_day = 1; b.kept_day = 2; before = json.dumps([b.day, b.kind_day, b.switches_day, b.compound_active])
b.command("/stats"); txt = [o for o in out if o[0] == "admin"][-1]
full = b.summary_text(); print("/stats first line ->", txt[2]); print(full)
assert "USDINR: 2W / 1L" in full and ":57 switches 1" in full and "Eligibility" in full and "Shadow" in full
assert json.dumps([b.day, b.kind_day, b.switches_day, b.compound_active]) == before, "stats reset!"
b.command("/help"); h = b.summary_text
import inspect; src = inspect.getsource(bot.Bot.handlers)
assert "/stats" in src and "/public" in src and "/channel" in src
# 5) :57 switch threshold
import analyzer as A, config as C
sig = {"signal": "PUT", "atr": 1.0}
assert A.switch_kind(sig, 0, 0.53, False, "strong opposing candle (0.5 ATR)") is None         # live bug case -> keep PUT
assert A.switch_kind(sig, 0, 0.85, False, "strong opposing candle (0.9 ATR)") == "momentum"
assert A.switch_kind(sig, 0, -1.6, False, "exhausted: ran 1.6 ATR already") is None             # reversion w/o opposite candle -> keep
print("switch threshold", C.SWITCH_MIN_BODY_ATR, ": 0.53 ATR kept, 0.85 ATR switched, reversion kept")
print("\nALL CHANNEL / STATS / SWITCH TESTS PASSED")
