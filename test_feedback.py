"""Feedback store: drops/shadow/bug notes written to STATE_DIR/feedback/YYYY-MM-DD.jsonl (not sent), 30-day rotation,
/feedback counts, /feedback export → CSV + JSON documents, menu button, /reset feedback scope with backup."""
import os, tempfile, json, glob
SD = tempfile.mkdtemp()
os.environ.update(STATE_DIR=SD, TELEGRAM_ENABLED="0", DATA_SOURCE="replay", REPLAY_CSV="replay_m1.csv", HIST_GATE="off", BLACKOUT="off",
                  NEWS_FILTER="off", DOUBLE_CONFIRM="off", PATTERNS="off", VOLUME_MODE="off")
import pandas as pd, config as C, bot, feedback as FB
from datetime import timedelta
b = bot.Bot(bot.ReplayAdapter("replay_m1.csv")); ADM, DOCS = [], []
b.tg.admin = lambda t: ADM.append(t); b.tg.admin_document = lambda p, cap="": (DOCS.append(p), True)[1]
T = pd.Timestamp("2026-10-15 10:00:02", tz="UTC"); b.d.now = lambda: T
C.VERBOSE = False
b.drop_note("dc", "bug B2", "➖ single-confirmation, dropped: bug B2 X CALL")
b.drop_note("hist", "pattern P3", "📚 Filtered by history: P3 · not enough history")
assert not ADM, "suppressed, not sent"
recs = FB.read(1, T); assert [r["kind"] for r in recs] == ["drop_single_confirmation", "filtered_by_history"] and recs[0]["source"] == "bug B2"
assert os.path.exists(os.path.join(SD, "feedback", "2026-10-15.jsonl"))
# rotation keeps 30 days
old = os.path.join(SD, "feedback", "2026-08-01.jsonl"); open(old, "w").write('{"kind":"x"}\n')
FB.rotate(T, 30); assert not os.path.exists(old) and os.path.exists(os.path.join(SD, "feedback", "2026-10-15.jsonl"))
# /feedback summary + export
b.command("/feedback"); assert "drop_single_confirmation 1" in ADM[-1] and "filtered_by_history 1" in ADM[-1]
b.command("/feedback export 7d"); assert len(DOCS) == 2 and DOCS[0].endswith(".csv") and DOCS[1].endswith(".json")
assert "filtered_by_history" in open(DOCS[0]).read() and len(json.load(open(DOCS[1]))) == 2
# menu button + help
assert any(any(btn[1] == "c:/feedback" for btn in row) for row in b.menu_view("main")[1])
# /reset feedback → backup copy + files removed
b.command("/reset feedback"); b.command("/reset confirm")
assert not glob.glob(os.path.join(SD, "feedback", "*.jsonl")) and glob.glob(os.path.join(SD, "backups", "*2026-10-15.jsonl"))
FB.log("x", "y", now=T); assert any(p.endswith(".jsonl") for p in b._reset_files("all")), "reset-all includes feedback"
print("ALL FEEDBACK TESTS PASSED")
