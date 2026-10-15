"""Mocked /v1/symbols scenarios for the pair resolver (no network)."""
import os, logging, sys
os.environ.setdefault("OTCHARTS_API_KEY", "test")
import config as C, bot
others = [f"{p}_otc" for p in C.PAIRS if p != "USDBRL"]
def make(cat_quotex, cat_fallback=None, allow=False):
    C.ALLOW_CROSS_BOOK_FALLBACK = allow
    class A(bot.OTChartsAdapter):
        def _get(self, path, **kw):
            if path == "/v1/symbols":
                cat = cat_quotex if kw["venue"] == "quotex" else (cat_fallback or [])
                if kw["venue"] != "quotex" and cat_fallback is None: raise RuntimeError("/v1/symbols 403: plan does not open this book")
                return {"symbols": [{"symbol": s, "payout": 88, "open": True} for s in cat]}
            raise RuntimeError("no network in test")
    return A()
for name, kw in [("1: USDBRL_otc present", dict(cat_quotex=others + ["USDBRL_otc"])),
                 ("2: only BRLUSD_otc present", dict(cat_quotex=others + ["BRLUSD_otc"])),
                 ("3: none present", dict(cat_quotex=others)),
                 ("4: none on quotex, ALLOW_CROSS_BOOK_FALLBACK=true, fallback book lists USDBRL_otc", dict(cat_quotex=others, cat_fallback=["USDBRL_otc"], allow=True)),
                 ("6: none on quotex, fallback allowed but plan opens only one book (403)", dict(cat_quotex=others, allow=True)),
                 ("5: odd spelling found by catalogue scan", dict(cat_quotex=others + ["usd-brl_OTC"]))]:
    print(f"\n=== Scenario {name} ==="); sys.stdout.flush()
    a = make(**kw)
    m = a.pair_meta(); print("meta USDBRL:", m["USDBRL"], "| label:", a.label("USDBRL"), "| note CALL:", repr(a.note("USDBRL", "CALL")))
    print("USDBRL still in PAIRS:", "USDBRL" in C.PAIRS, "| COMPOUND_PAIRS:", C.COMPOUND_PAIRS); sys.stdout.flush()

print("\n=== Retry after 30 min (scenario 3 -> symbol appears) ===")
C.ALLOW_CROSS_BOOK_FALLBACK = False
a = make(cat_quotex=others)
a.resolve_at -= C.RESOLVE_RETRY_MIN*60 + 1
a._get.__func__  # noqa
cat_new = others + ["USDBRL_otc"]
a.catalogue = lambda book: (cat_new, {"symbols": [{"symbol": s, "payout": 90, "open": True} for s in cat_new]})
a.meta_at = 0; print("meta after retry:", a.pair_meta()["USDBRL"], a.label("USDBRL"))
