"""Filter ablation: replay the live decision path over stored 1m candles with each filter on/off, and score
win rate vs breakeven, signals per open hour and expectancy per signal E = WR·P − (1−WR).
Live timing: bar i closes → scan at :02 → entry bar i+2 (next :00); the :57 line sees bar i+1.
Used by sim/ (offline) and by the Sunday 03:00 self-tune (bot.py, background thread)."""
import math, collections, itertools
import numpy as np, pandas as pd
import config as C, analyzer as A
from bugstrats import double_confirm, ensure_cols
from history import wilson_lb, wilson_ub
from filters import session_blackout

def stage1(pair, d, pay=0.80, levels=(3, 4)):
    """Per closed bar: ensemble decision at each level (+ DC strict/fill flags), member votes, outcomes."""
    d = d[["open", "high", "low", "close"]].sort_index(); x = A.compute(d); ensure_cols(x); rows = []
    o, c, idx = x.open.values, x.close.values, x.index
    for i in range(C.MIN_BARS, len(x) - 2):
        if idx[i + 1] - idx[i] != pd.Timedelta("60s") or idx[i + 2] - idx[i + 1] != pd.Timedelta("60s"): continue
        a_ = float(x.atr.iloc[i]) or 1e-12
        rec = {"t": idx[i], "pair": pair, "out": int(np.sign(c[i + 1] - o[i + 1])), "out2": int(np.sign(c[i + 2] - o[i + 2])),
               "b1": float(c[i + 1] - o[i + 1]) / a_}
        for L in levels:
            with A._overrides(A.level_overrides(L)): s = A.ensemble(d.iloc[:i + 1], pair, payout=pay, x=x, i=i, use_gate=False)
            if s.get("signal"):
                m, st, _ = double_confirm(x, i, s["signal"]); mf, stf, _ = double_confirm(x, i, s["signal"], fill=True)
                rec[f"L{L}"] = (s["signal"], tuple(s["agree"]), s["confidence"], bool(m and st), bool(mf and stf))
            if L == levels[0]: rec["votes"] = {k: v[0] for k, v in (s.get("strategies") or {}).items() if v and k != "D"}
        rows.append(rec)
    return rows

BASE = dict(hist=True, dc=True, blackout=True, news=True, cooldown=True, level=3, switch=True, fill=True, dailystop=True, volume=True, fill_level=4)

def simulate(rows, cfg=None, pay=0.80, news_events=(), cover_days=2.0):
    """news_events: [(utc Timestamp, currency)]. Returns metrics dict."""
    cfg = {**BASE, **(cfg or {})}; BE = 1 / (1 + pay)
    by_t = collections.defaultdict(list)
    for r in rows: by_t[r["t"]].append(r)
    times = sorted(by_t); t_first = {}; hist = collections.defaultdict(list)
    sent = []; W = L_ = 0; tiers = collections.Counter(); hours = collections.defaultdict(lambda: {"n": 0, "act": 0}); pairs_h = collections.defaultdict(set)
    block_until = frozen = stop_until = None; streak = 0; day_net = collections.Counter(); fill_last = {}
    sw_n = 0
    for t in times:
        now = t + pd.Timedelta(minutes=1, seconds=2); h = now.floor("h"); rs = by_t[t]
        for r in rs: t_first.setdefault(r["pair"], t); pairs_h[h].add(r["pair"])
        hard = (cfg["blackout"] and session_blackout(now.to_pydatetime())) or (cfg["cooldown"] and frozen is not None and now < frozen) \
               or (cfg["dailystop"] and stop_until is not None and now < stop_until)
        if not hard: hours[h]["act"] += 1
        if not hard and not (block_until and now < block_until):
            cands, pool = [], []
            for r in rs:
                if cfg["news"] and any(abs((now - te).total_seconds()) <= C.NEWS_BLACKOUT_MIN * 60 and cur in r["pair"] for te, cur in news_events): continue
                s = r.get(f"L{cfg['level']}"); sf = r.get(f"L{max(cfg['level'], cfg['fill_level'])}")
                if not s and not sf: continue
                strict = bool(s); s = s or sf
                dc_ok = s[3] or not cfg["dc"]
                w = n = w7 = n7 = 0
                for k in s[1]:
                    for tt, win in hist[(r["pair"], k, s[0])]:
                        n += 1; w += win
                        if tt >= t - pd.Timedelta(days=7): n7 += 1; w7 += win
                if not cfg["hist"]: t1 = True
                else:
                    cover = (t - t_first[r["pair"]]).total_seconds() / 86400; nn, ww = n + 2 * n7, w + 2 * w7
                    t1 = cover >= cover_days and n >= C.HIST_MIN_N and wilson_lb(ww, nn) >= BE + C.HIST_MARGIN and not (n7 >= 5 and w7 / n7 < BE)
                if strict and dc_ok and t1: cands.append((s[2], r, s, 1))
                elif (s[4] or not cfg["dc"]) and not (n and wilson_ub(w, n) < BE):
                    sc = s[2] / 100 + ((w / n - BE) if n else 0) + (0.05 if strict else 0)
                    neg = n >= C.HIST_MIN_N and w / n < BE          # Tier 2 refuses 'history below BE'; Tier 3 only needs UB ≥ BE
                    pool.append((sc, r, s, 3 if (neg and cfg["hist"]) else 2))
            pick = max(cands, key=lambda c: c[0]) if cands else None
            m = now.minute; hs_ = sum(1 for x_ in sent if x_ >= h)
            el = [p for p in pool if not (fill_last.get(p[1]["pair"]) and now - fill_last[p[1]["pair"]] < pd.Timedelta(minutes=10))]
            if pick is None and el and cfg["fill"] and hs_ < C.VOLUME_MAX_HOUR:
                s0 = h + pd.Timedelta(minutes=(m // 15) * 15)
                need = m // 15 + (1 if now >= s0 + pd.Timedelta(minutes=C.FILL_SLOT_WAIT) else 0)
                need = min(need, math.ceil(4 * max(1, hours[h]["act"]) / 60))
                in_slot = any(x_ >= s0 for x_ in sent) and now < s0 + pd.Timedelta(minutes=C.FILL_SLOT_WAIT)
                e2 = [p for p in el if p[3] == 2]
                if hs_ < need and not in_slot and e2: pick = max(e2, key=lambda c: c[0]); fill_last[pick[1]["pair"]] = now
            if pick is None and el and cfg["volume"] and hs_ < C.VOLUME_MAX_HOUR:
                s0 = h + pd.Timedelta(minutes=(m // C.VOLUME_SLOT_MIN) * C.VOLUME_SLOT_MIN)
                if not any(x_ >= s0 for x_ in sent) and now >= s0 + pd.Timedelta(minutes=min(C.FILL_SLOT_WAIT, C.VOLUME_SLOT_MIN - 1)):
                    need = min(C.VOLUME_MAX_HOUR, m // C.VOLUME_SLOT_MIN + 1, math.ceil(C.VOLUME_MAX_HOUR * max(1, hours[h]["act"]) / 60))
                    if hs_ < need:
                        p3 = max(el, key=lambda c: c[0]); pick = (p3[0], p3[1], p3[2], 3); fill_last[p3[1]["pair"]] = now
            if pick:
                _, r, s, tier = pick; side = s[0]
                if cfg["switch"] and ((r["b1"] <= -C.SWITCH_MIN_BODY_ATR and side == "CALL") or (r["b1"] >= C.SWITCH_MIN_BODY_ATR and side == "PUT")):
                    side = "PUT" if side == "CALL" else "CALL"; sw_n += 1
                o = r["out2"]; sent.append(now); hours[h]["n"] += 1; block_until = now + pd.Timedelta(minutes=C.SIGNAL_COOLDOWN_MIN)
                if o:
                    win = (o > 0) == (side == "CALL"); W += win; L_ += not win; tiers[(tier, win)] += 1
                    dkey = (now + pd.Timedelta(hours=3)).date(); day_net[dkey] += -1 if win else 1   # losses - wins
                    streak = 0 if win else streak + 1
                    if streak >= C.LOSS_STREAK_N: streak = 0; frozen = now + pd.Timedelta(minutes=C.STREAK_COOLDOWN_MIN)
                    if day_net[dkey] >= C.DAILY_STOP_NET:
                        hh, mm = map(int, C.DAILY_STOP_RESUME.split(":")); stop_until = pd.Timestamp(dkey).tz_localize("UTC") + pd.Timedelta(days=1, hours=hh - 3, minutes=mm)
        for r in rs:
            if r["out"]:
                for k, side in (r.get("votes") or {}).items(): hist[(r["pair"], k, side)].append((t, (r["out"] > 0) == (side == "CALL")))
    op = {k: v for k, v in hours.items() if v["act"] >= 30 and len(pairs_h.get(k, ())) >= 10}
    n = W + L_; wr = W / n if n else 0.0; e = wr * pay - (1 - wr) if n else 0.0
    hit = sum(1 for v in op.values() if v["n"] >= math.ceil(4 * v["act"] / 60))
    in46 = sum(1 for v in op.values() if math.ceil(4 * v["act"] / 60) <= v["n"] <= 6)
    dist = collections.Counter(min(v["n"], 7) for v in op.values())
    tw = {k: (tiers[(k, True)], tiers[(k, True)] + tiers[(k, False)]) for k in (1, 2, 3)}
    return dict(n=n, wr=wr, be=BE, exp=e, total=e * n, per_hour=(sum(v["n"] for v in op.values()) / max(1, len(op))),
                pct4=hit / max(1, len(op)), pct46=in46 / max(1, len(op)), dist=dict(sorted(dist.items())), tiers=tw,
                open_hours=len(op), switched=sw_n)

GRID = [("baseline (all on, L3, volume)", {}), ("− volume mode", dict(volume=False)), ("− history gate", dict(hist=False)), ("− double-confirm", dict(dc=False)),
        ("− blackouts", dict(blackout=False)), ("− news", dict(news=False)), ("− cooldown", dict(cooldown=False)),
        ("− daily stop", dict(dailystop=False)), ("level 4", dict(level=4)), ("− :57 switch (keep only)", dict(switch=False)),
        ("− Tier-2 fill", dict(fill=False)), ("− hist − DC", dict(hist=False, dc=False)), ("− hist, L4", dict(hist=False, level=4)),
        ("− DC, L4", dict(dc=False, level=4)), ("− hist − DC, L4", dict(hist=False, dc=False, level=4)),
        ("− switch − DC", dict(switch=False, dc=False)), ("all off (raw L4)", dict(hist=False, dc=False, blackout=False, news=False, cooldown=False, dailystop=False, level=4, switch=False))]

def table(rows, pay=0.80, news_events=()):
    out = []
    for name, cfg in GRID:
        m = simulate(rows, cfg, pay, news_events); out.append((name, cfg, m))
    return out

def fmt(res):
    lines = [f"{'config':28} {'n':>5} {'WR':>6} {'BE':>6} {'E/sig':>7} {'total':>7} {'sig/h':>6} {'≥4/h':>5}"]
    for name, _, m in res:
        lines.append(f"{name:28} {m['n']:5d} {m['wr']:6.1%} {m['be']:6.1%} {m['exp']:+7.3f} {m['total']:+7.1f} {m['per_hour']:6.2f} {m['pct4']:5.0%}")
    return "\n".join(lines)

SAFE = {"dc", "level", "switch", "hist_margin"}   # self-tune may only touch these; never the hard blocks (pause/news/blackout/cooldown/daily stop/payout)
def propose(res, min_n=60):
    """Best config by total expectancy among those with ≥4 signals/open hour AND expectancy ≥ 0 if any, else best total expectancy.
    Returns (name, cfg, metrics, safe_changes). Hard blocks are never loosened."""
    ok = [r for r in res if r[2]["n"] >= min_n]
    if not ok: return None
    # the frequency target only counts among configs that don't lose money: a negative-expectancy config is never "best"
    reach = [r for r in ok if r[2]["per_hour"] >= 4 and r[2]["exp"] >= 0]
    best = max(reach or ok, key=lambda r: r[2]["total"])
    safe = {k: v for k, v in best[1].items() if k in ("dc", "level", "switch")}
    return best[0], best[1], best[2], safe
