"""Resolve logical pairs (e.g. USDBRL) to the symbol a book actually lists.

Direction rule for flipped quotes: if USDBRL resolves to BRLUSD_otc, the bot analyses and trades
BRLUSD_otc's OWN candles, so its CALL/PUT apply to BRLUSD directly (the instrument shown on the card).
Economically, CALL on BRLUSD == PUT on USDBRL; that equivalence is shown as a note only."""
from __future__ import annotations
import logging, re
from dataclasses import dataclass
import config as C
log = logging.getLogger("bot")

@dataclass
class Resolution:
    pair: str                 # logical pair, e.g. USDBRL (stats key)
    symbol: str | None        # exact id on the book
    book: str | None
    inverted: bool = False    # True if symbol quotes QUOTE/BASE (e.g. BRLUSD)
    variant: int | None = None
    tried: list | None = None
    cross_book: bool = False

def _norm(s):
    s = s.upper()
    s = re.sub(r"[-_ ]?OTC$", "", s)
    return re.sub(r"[^A-Z]", "", s)

def candidates(pair):
    base, quote = pair[:3], pair[3:]
    gen = [f"{base}{quote}_otc", f"{quote}{base}_otc", f"{base}{quote}-OTC", f"{quote}{base}-OTC",
           f"{base}/{quote}-OTC", f"{quote}/{base}-OTC", f"{base}/{quote}", f"{quote}/{base}", f"{base}{quote}", f"{quote}{base}"]
    out = []
    for s in list(C.PAIR_VARIANTS.get(pair, [])) + gen:
        if s not in out: out.append(s)
    return out

def resolve(pair, catalogue, book):
    """catalogue: iterable of symbol ids listed by `book`. Explicit candidates first (in order),
    then a scan of the catalogue for any id containing both currencies in either order."""
    cat = list(catalogue); have = set(cat); base, quote = pair[:3], pair[3:]
    tried = candidates(pair)
    for n, s in enumerate(tried, 1):
        if s in have:
            return Resolution(pair, s, book, _norm(s).startswith(quote), n, tried)
    for s in cat:                                   # catalogue scan
        ns = _norm(s)
        if ns in (base+quote, quote+base) or (base in ns and quote in ns and len(ns) <= 8):
            tried.append(f"scan:{s}")
            return Resolution(pair, s, book, ns.startswith(quote), len(tried), tried)
    return Resolution(pair, None, None, False, None, tried)

def describe(r: Resolution):
    if r.symbol:
        msg = f"{r.pair} -> resolved to {r.symbol} on {r.book} (variant {r.variant})"
        if r.inverted: msg += f"; FLIPPED quote: signals are for {r.symbol}; CALL on {r.symbol} = PUT on {r.pair}"
        if r.cross_book: msg += f"; CROSS-BOOK: prices are {r.book}'s, NOT Quotex's; signals will not match Quotex charts"
        return msg
    return (f"{r.pair} -> no variant found on {C.BROKER_BOOK}; tried {r.tried}; kept in pairs, "
            f"retry every {C.RESOLVE_RETRY_MIN} min")
