"""Weekly results report, daily backup, VIP promo approval flow (Telegram disabled)."""
import os, tempfile, time, zipfile, json
os.environ.update(STATE_DIR=tempfile.mkdtemp(), TELEGRAM_ENABLED="0", DATA_SOURCE="otcharts", PUBLIC_CHANNEL_ID="-100111")
from datetime import datetime, timedelta, timezone
import config as C, feedback as FB, adminjobs as AJ
UTC = timezone.utc
now = datetime(2026, 10, 18, 19, 30, tzinfo=UTC)   # Sunday 22:30 UTC+3
for i, (k, p, res) in enumerate([("vip_result", "USDBRL", "WIN"), ("vip_result", "USDBRL", "WIN"), ("vip_result", "USDMXN", "LOSS"),
                                 ("shadow_result", "USDINR", "LOSS"), ("bug_group_result", "USDPKR", "WIN"), ("public_result", "USDARS", "TIE")]):
    FB.log(k, "x", pair=p, result=res, now=now - timedelta(minutes=10 + i))
FB.log("vip_result", "old", pair="USDBRL", result="LOSS", now=now - timedelta(days=9))
t = AJ.weekly_text(now); print(t)
assert "vip: 2W/1L" in t and "shadow: 0W/1L" in t and "bug: 1W/0L" in t and "USDBRL: 2W/0L" in t
class TG:
    def __init__(s): s.adm, s.docs, s.kb, s.sent = [], [], [], []
    def admin(s, t): s.adm.append(t)
    def admin_document(s, p, cap=""): s.docs.append(p); return True
    def admin_kb(s, t, rows, e=None): s.kb.append((t, rows))
    def send(s, c, t, l): s.sent.append((c, t))
class D:
    def __init__(s): s.t = now
    def now(s): return s.t
class B(AJ.AdminJobs): pass
b = B(); b.tg = TG(); b.d = D()
json.dump({"w": 1}, open(os.path.join(C.STATE_DIR, "stats.json"), "w")); open(os.path.join(C.STATE_DIR, "secret_token.json"), "w").write("x")
b.adminjobs_tick()   # 22:30 Sunday: weekly + promo due, backup not yet
assert any("Weekly results" in m for m in b.tg.adm) and len(b.tg.kb) == 1 and not b.tg.docs and not b.tg.sent, "promo must NOT post before approval"
assert "2W–1L" in b.tg.kb[0][0] and "guarantee" in b.tg.kb[0][0]
n = len(b.tg.adm); b.adminjobs_tick(); assert len(b.tg.adm) == n and len(b.tg.kb) == 1, "once per day"
pid = b.tg.kb[0][1][0][0][1].split()[1]
b.promo_cb(f"approve {pid}"); assert b.tg.sent == [("-100111", AJ.promo_text(2, 1))]
b.promo_cb(f"approve {pid}"); assert len(b.tg.sent) == 1, "no double post"
# skip + expiry
pid2 = b.promo_build(); b.promo_cb(f"skip {pid2}"); assert len(b.tg.sent) == 1
pid3 = b.promo_build(); j = AJ.load(); j["promos"][pid3]["at"] -= 13 * 3600; AJ.save(j); b.adminjobs_tick()
assert AJ.load()["promos"][pid3]["state"] == "expired"; b.promo_cb(f"approve {pid3}"); assert len(b.tg.sent) == 1
# backup at 23:55
b.d.t = now.replace(hour=20, minute=56); b.adminjobs_tick(); assert len(b.tg.docs) == 1
names = zipfile.ZipFile(b.tg.docs[0]).namelist(); print(names)
assert "stats.json" in names and not any("secret" in x for x in names) and any(x.startswith("feedback/") for x in names)
# promo off / zero VIP trades
b.promo_cmd(["off"]); assert AJ.load()["promo_on"] is False; b.promo_cmd(["on"])
b.d.t = now + timedelta(days=1); k = len(b.tg.kb); b.promo_build(); assert len(b.tg.kb) == k and "0 trades" in b.tg.adm[-1]
print("ADMIN JOBS TESTS PASSED")
