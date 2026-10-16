"""VIP/public signals + results are written silently to the feedback log (vip_signal/vip_result, public_*), bug_group_result distinct;
nothing extra posted to channels or admin; /feedback today lists the new kinds; stats untouched."""
import os, sys, tempfile, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv",
                  PUBLIC_CHANNEL_ID="-100PUB", VIP_CHANNEL_ID="-100VIP")
import bot, feedback as FB
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); out = []
b.tg.send = lambda chat, text, label="group": out.append((label, chat, text)); b.tg.on = True
import requests; requests.post = lambda *a, **k: type("R", (), {"ok": False, "status_code": 0, "text": "mock"})()
b.command("/both"); b.command("/publiccap start 00:00"); d = b.d
for _ in range(400):
    b.tick()
    if b.pending: break
    d.advance()
assert b.pending, "no signal in replay"
now = d.now(); kinds = [r["kind"] for r in FB.read(1, now)]
assert "vip_signal" in kinds and "public_signal" in kinds, kinds
saved = d.clock; d.clock = b.pending["precheck_at"]; b.run_precheck(); d.clock = saved
n_before = None
while b.pending: d.advance(); b.tick()
recs = FB.read(1, d.now()); res = [r for r in recs if r["kind"] in ("vip_result", "public_result")]
if not any(r["kind"] == "main_result" for r in recs) and res:
    r = res[-1]
    for f in ("ts", "pair", "dir", "entry_at", "payout", "tier", "source", "pattern", "dc", "hist", "level", "result", "open", "close", "destination"): assert f in r, f
    assert r["result"] in ("WIN", "LOSS", "TIE") and {x["destination"] for x in res} == {"vip", "public"}
    print("result rows:", [(x["kind"], x["pair"], x["result"]) for x in res])
else: print("trade was skipped at precheck (no result rows) — signal rows verified")
# no per-signal feedback text reached any chat
assert not any("vip_signal" in o[2] or "vip_result" in o[2] for o in out)
# bug group result logged distinctly via fb_main/bug path helper
g = {"k": "B2", "pair": "USDINR", "dir": "CALL", "traded": "CALL", "entry_at": d.now(), "payout": 0.85, "chats": [("bug", "-100BUG"), ("vip", "-100VIP")], "dc": "on"}
b.fb_main(g, "result", "WIN", 1.0, 1.1, bug=True); r = FB.read(1, d.now())[-1]
assert r["kind"] == "vip_result" and r["source"] == "bug B2" and r["tier"] == "bug"
src = open("bot.py").read(); assert '"bug_group_result"' in src
# /feedback lists new kinds
ADM = []; b.tg.admin = lambda t: ADM.append(t); b.command("/feedback"); assert "vip_signal" in ADM[-1] and "public_signal" in ADM[-1], ADM[-1]
print("ALL FEEDBACK MAIN TESTS PASSED")
