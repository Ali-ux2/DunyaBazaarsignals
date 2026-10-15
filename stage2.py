"""Minute-by-minute scheduler replay of the live decision path: one trade at a time, 3-min signal cooldown, session blackouts,
3-loss/30-min cooldown, history gate (pooled agreeing members, Wilson LB ≥ BE+3, 7d ×2), Tier-2 paced fill. Payout 80% → BE 55.6%."""
import sys, os, pickle, math, json, itertools, collections, pandas as pd
sys.path.insert(0, "/workspace/otc_bot_v2"); os.environ.update(TELEGRAM_ENABLED="0")
import config as C
from history import wilson_lb, wilson_ub
from filters import session_blackout
PAY = 0.80; BE = 1 / (1 + PAY)
def sim(rows, strict=3, fill_level=4, fill_on=True, slot=15, wait=6, pair_gap=10, cover_days=2.0, min_n=15, margin=0.03, fill_negok=False, pace=True):
    by_t = collections.defaultdict(list)
    for r in rows: by_t[r["t"]].append(r)
    times = sorted(by_t); t_first = {}
    hist = collections.defaultdict(list)          # (pair, member) -> [(t, win)]
    sent = []; res = collections.Counter(); hours = collections.defaultdict(lambda: {"n": 0, "act": 0, "t1": 0, "t2": 0})
    block_until = None; streak = 0; frozen_until = None; fill_last = {}
    for t in times:
        now = t + pd.Timedelta(minutes=1, seconds=40)                 # decision at :40 of the following minute (bar t closed)
        rs = by_t[t]
        for r in rs: t_first.setdefault(r["pair"], t)
        h = now.floor("h"); blk = session_blackout(now.to_pydatetime()) or (frozen_until is not None and now < frozen_until)
        if not blk: hours[h]["act"] += 1
        # history update is causal: outcomes of bars <= t-1 are known (bar t's outcome is the NEXT bar)
        cands, pool = [], []
        if not blk and not (block_until and now < block_until):
            for r in rs:
                s = r.get(f"L{strict}")
                def hs(agree, side):
                    w = n = w7 = n7 = 0
                    for k in agree:
                        if k == "D": continue
                        for tt, win in hist[(r["pair"], k, side)]:
                            n += 1; w += win
                            if tt >= t - pd.Timedelta(days=7): n7 += 1; w7 += win
                    return w, n, w7, n7
                cover = (t - t_first[r["pair"]]).total_seconds() / 86400
                if s and s[3]:
                    w, n, w7, n7 = hs(s[1], s[0]); nn, ww = n + 2 * n7, w + 2 * w7
                    ok = cover >= cover_days and n >= min_n and wilson_lb(ww, nn) >= BE + margin and not (n7 >= 5 and w7 / n7 < BE)
                    (cands if ok else pool).append((r, s, w, n, False))
                f = r.get(f"L{fill_level}")
                if fill_on and f and f[4] and not (s and s[3]):
                    w, n, *_ = hs(f[1], f[0]); pool.append((r, f, w, n, True))
            pick = tier = None
            if cands: pick = max(cands, key=lambda c: c[1][2]); tier = 1
            elif fill_on and pool:
                m = now.minute; s0 = now.floor("h") + pd.Timedelta(minutes=(m // slot) * slot)
                hs_ = sum(1 for x_ in sent if x_ >= now.floor("h")); in_slot = any(x_ >= s0 for x_ in sent)
                need = (m // slot) + (1 if now >= s0 + pd.Timedelta(minutes=wait) else 0) if pace else (math.ceil(4 * m / 60) if m >= 40 else 0)
                if hs_ < need and not (in_slot and now < s0 + pd.Timedelta(minutes=wait)):
                    el = []
                    for c in pool:
                        r, s, w, n, wide = c
                        if fill_last.get(r["pair"]) and now - fill_last[r["pair"]] < pd.Timedelta(minutes=pair_gap): continue
                        if n and wilson_ub(w, n) < BE: continue
                        if not fill_negok and n >= min_n and w / n < BE: continue
                        el.append((s[2] / 100 + ((w / n - BE) if n else 0) + (0 if wide else 0.05), c))
                    if el: pick = max(el, key=lambda e: e[0])[1]; tier = 2; fill_last[pick[0]["pair"]] = now
            if pick:
                r, s = pick[0], pick[1]; o = r["out"]
                win = None if o == 0 else ((o > 0) == (s[0] == "CALL"))
                res[(tier, win)] += 1; sent.append(now); hours[h]["n"] += 1; hours[h][f"t{tier}"] += 1
                block_until = now + pd.Timedelta(minutes=C.SIGNAL_COOLDOWN_MIN)
                if win is False: streak += 1
                elif win: streak = 0
                if streak >= 3: streak = 0; frozen_until = now + pd.Timedelta(minutes=30)
        for r in rs:   # this bar's members → outcome known next minute
            if r["out"] == 0: continue
            for k, side in (r.get("votes") or {}).items(): hist[(r["pair"], k, side)].append((t, (r["out"] > 0) == (side == "CALL")))
    op = {h: v for h, v in hours.items() if v["act"] >= 30 and sum(1 for p in set(x["pair"] for x in by_t.get(h, [])) ) >= 0}
    # open hour = ≥30 active minutes with ≥10 pairs trading
    pairs_h = collections.defaultdict(set)
    for t in times:
        for r in by_t[t]: pairs_h[t.floor("h")].add(r["pair"])
    op = {h: v for h, v in hours.items() if v["act"] >= 30 and len(pairs_h.get(h - pd.Timedelta(hours=0), ())) >= 10}
    hit = sum(1 for v in op.values() if v["n"] >= math.ceil(4 * v["act"] / 60))
    dist = collections.Counter(min(v["n"], 8) for v in op.values())
    wr = lambda k: (res[(k, True)], res[(k, True)] + res[(k, False)])
    return dict(open_hours=len(op), hit=hit, pct=hit / max(1, len(op)), dist=dict(sorted(dist.items())), t1=wr(1), t2=wr(2),
                ties=res[(1, None)] + res[(2, None)])
if __name__ == "__main__":
    rows = pickle.load(open(sys.argv[1], "rb")); print("rows", len(rows))
    fmt = lambda r: (f"open hours {r['open_hours']} · ≥4 in {r['hit']} ({r['pct']:.0%}) · dist {r['dist']} · "
                     f"T1 {r['t1'][0]}/{r['t1'][1]}" + (f" ({r['t1'][0]/r['t1'][1]:.1%})" if r['t1'][1] else "") +
                     f" · T2 {r['t2'][0]}/{r['t2'][1]}" + (f" ({r['t2'][0]/r['t2'][1]:.1%})" if r['t2'][1] else ""))
    grid = [dict(fill_on=False), dict(pace=False), dict(), dict(fill_negok=True), dict(wait=3), dict(strict=4, fill_level=4),
            dict(strict=4, fill_level=4, fill_negok=True), dict(margin=0.0)]
    for g in grid: print(g, "→", fmt(sim(rows, **g)), flush=True)
