"""PUBLIC session: opens 10:00 UTC+3, closes at net +2 (W,W / W,L,W / W,L,L,W,W), safety stops (max signals, max net loss),
VIP-only outside the session, open trades still get results (losses counted), /publiccap commands, restart mid-session."""
import os, tempfile, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv",
                  PUBLIC_CHANNEL_ID="-100111", VIP_CHANNEL_ID="-100222", GROUP_FILTER="all")
import pandas as pd, config as C, bot
CLOCK = [pd.Timestamp("2026-01-05 06:30", tz="UTC")]           # 09:30 UTC+3
def mk():
    b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); b.d.now = lambda: CLOCK[0]; b.pending = None; b.bug_sigs = []
    b.tg.admin = lambda t: ADM.append(t); b.tg.group = lambda t, chats=None: GRP.append((t, [c[0] for c in (chats or [])])); return b
ADM, GRP = [], []
b = mk(); both, pub, vip = b.chats_for("both"), b.chats_for("public"), b.chats_for("vip")
assert not b.public_open() and b.cap_chats(pub) == vip and b.cap_chats(both) == vip, "before 10:00 → VIP only"
print(b.pubcap_line())
def day(seq, start="2026-01-05"):
    global CLOCK
    CLOCK[0] = pd.Timestamp(start + " 08:00", tz="UTC")        # 11:00 UTC+3
    closed_at = None
    for k, r in enumerate(seq):
        assert b.public_open(), (seq, k); ch = b.cap_chats(both); b.pubcap_posted(ch); b.pubcap_result(ch, r)
        if b.pubcap()["closed"] and closed_at is None: closed_at = k + 1
    return closed_at
for i, seq in enumerate((["WIN", "WIN"], ["WIN", "LOSS", "WIN"], ["WIN", "LOSS", "LOSS", "WIN", "WIN"])):
    n0 = len(GRP); at = day(seq, f"2026-01-{6 + i:02d}")
    assert at == len(seq) and b.pubcap()["closed"] == "target", (seq, at)
    assert "Daily target reached" in GRP[-1][0] and GRP[-1][1] == ["public"]
    assert b.cap_chats(both) == vip and b.cap_chats(pub) == vip, "after close → VIP only"
    print(" ".join(r[0] for r in seq), "→", b.pubcap_line())
# strict "net" rule: W,L,W does not close
C.PUBLIC_CLOSE_RULE = "net"; assert day(["WIN", "LOSS", "WIN"], "2026-01-09") is None and b.public_open(); C.PUBLIC_CLOSE_RULE = "wins"
# safety stop: net loss
at = day(["LOSS", "WIN", "LOSS", "LOSS", "LOSS"], "2026-01-10"); assert b.pubcap()["closed"] == "maxloss" and at == 5
assert any("PUBLIC safety stop" in m and "protection" in m for m in ADM) and "Public session closed for today: 1W/4L (net -3)" in GRP[-1][0]
# safety stop: max signals (alternating W/L never reaches +2)
seq = ["LOSS", "WIN"] * 5; at = day(seq, "2026-01-11"); assert b.pubcap()["closed"] == "max" and at == 10, at
print("safety:", b.pubcap_line())
# open trade finishes after close: its loss is counted and posted
b.pubcap_result(pub, "LOSS"); assert b.pubcap()["l"] == 6
# restart mid-session
CLOCK[0] = pd.Timestamp("2026-01-12 08:00", tz="UTC"); ch = b.cap_chats(both); b.pubcap_posted(ch); b.pubcap_result(ch, "WIN")
b2 = mk(); assert b2.public_open() and b2.pubcap()["w"] == 1 and b2.pubcap()["n"] == 1, "restored after restart"
b2.pubcap_posted(both); b2.pubcap_result(both, "WIN"); assert b2.pubcap()["closed"] == "target"
# commands
b2.publiccap_cmd(["target", "3"]); b2.publiccap_cmd(["max", "8"]); b2.publiccap_cmd(["start", "09:30"]); b2.publiccap_cmd(["maxloss", "-4"])
j = json.load(open(C.PUBLIC_CAP_FILE)); assert (j["target"], j["max"], j["start"], j["maxloss"]) == (3, 8, "09:30", -4)
b2.publiccap_cmd(["start", "25:00"]); assert ADM[-1].startswith("Use /publiccap")
CLOCK[0] = pd.Timestamp("2026-01-13 06:35", tz="UTC"); assert b2.public_open(), "new start 09:30 UTC+3"
b2.command("/status"); assert "Public session" in ADM[-1]; assert "Public session" in b2.summary_text()
print("PUBLIC SESSION TESTS PASSED")
