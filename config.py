"""All tunables. Secrets come ONLY from environment variables (Railway Variables)."""
import os

def _env(*names, default=None):
    for n in names:
        v = os.environ.get(n)
        if v not in (None, ""):
            return v
    return default

# ---- secrets / ids (primary name first, then fallbacks) ----
OTCHARTS_API_KEY    = _env("OTCHARTS_API_KEY", "OTCHARTS_KEY", "OTC_API_KEY")
TELEGRAM_BOT_TOKEN  = _env("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "BOT_TOKEN")
TELEGRAM_CHANNEL_ID = _env("TELEGRAM_CHANNEL_ID", "CHANNEL_ID", "CHAT_ID", "TELEGRAM_CHAT_ID")
TELEGRAM_ADMIN_ID   = _env("TELEGRAM_ADMIN_ID", "ADMIN_CHAT_ID", "ADMIN_ID")

# ---- runtime mode ----
DATA_SOURCE     = _env("DATA_SOURCE", default="otcharts")   # otcharts | replay
REPLAY_CSV      = _env("REPLAY_CSV", default="replay_m1.csv")
TELEGRAM_ENABLED = _env("TELEGRAM_ENABLED", default="1") == "1"
DRY_RUN_MINUTES = int(_env("DRY_RUN_MINUTES", default="0"))  # replay: stop after N simulated minutes
STATE_DIR       = _env("STATE_DIR", default=".")             # mount a Railway volume here
STATS_FILE      = os.path.join(STATE_DIR, "pair_stats.json")
TRADE_LOG       = os.path.join(STATE_DIR, "trades.csv")

# ---- OTCharts ----
OTCHARTS_BASE   = "https://otcharts.com"
BROKER_BOOK     = _env("BROKER_BOOK", default="quotex")  # quotex (default) | otc(Pocket Option) | iq | binodex
USER_AGENT      = "otc-signal-bot/1.0 (+requests)"
INITIAL_LIMIT   = 300
POLL_LIMIT      = 60
PAYOUT_REFRESH_SEC = 300
BOOK_UTC_OFFSET_H = {"otc": 2}        # Pocket Option stamps bars UTC+2; others UTC
BUDGET_WARN     = 0.80
BUDGET_PAUSE    = 0.95
USAGE_CHECK_SEC = 300

PAIRS = ["USDINR", "USDMXN", "USDARS", "USDPKR", "USDBDT", "USDCOP", "USDPHP",
         "USDIDR", "USDEGP", "USDNGN", "USDDZD", "USDBRL"]

# Pairs used for compounding sessions (never removed from PAIRS even if a book lacks them)
COMPOUND_PAIRS = ["USDINR", "USDMXN", "USDBRL"]

# Symbol resolution. Explicit variants are tried first, in order, then generic forms, then a catalogue scan.
# Brazil variants publicly listed on OTCharts (symbols.md, 14 Oct 2026): USDBRL_otc (Pocket Option), USDBRL-OTC (IQ Option);
# none listed on Quotex or BinoDex. Flipped forms are included in case the Quotex catalogue carries one.
# NOTE: on a flipped quote (BRLUSD) the bot analyses BRLUSD's own candles; CALL on BRLUSD == PUT on USDBRL.
PAIR_VARIANTS = {
    "USDBRL": ["USDBRL_otc", "BRLUSD_otc", "BRL/USD", "USD/BRL", "USDBRL-OTC", "BRLUSD-OTC",
               "USD/BRL-OTC", "BRL/USD-OTC", "USDBRL", "BRLUSD"],
}
RESOLVE_RETRY_MIN = 30
ALLOW_CROSS_BOOK_FALLBACK = _env("ALLOW_CROSS_BOOK_FALLBACK", default="false").lower() in ("1", "true", "yes")
CROSS_BOOK_PAYOUT = float(_env("CROSS_BOOK_PAYOUT", default="0.80"))  # Quotex payout assumed for a cross-book pair (check it manually on Quotex)
FALLBACK_BOOKS = [b for b in _env("FALLBACK_BOOKS", default="otc,iq").split(",") if b]

def symbol_for(pair, book=BROKER_BOOK):
    if book in ("otc", "quotex"): return f"{pair}_otc"
    if book == "iq": return f"{pair}-OTC"
    if book == "binodex": return f"{pair[:3]}/{pair[3:]}-OTC"
    return pair

# ---- sessions (optional; "HH:MM-HH:MM" UTC, comma-separated; empty = always on) ----
SESSIONS_UTC = _env("SESSIONS_UTC", default="")

# ---- timing ----
SCAN_MODE       = _env("SCAN_MODE", default="closed")   # closed: evaluate closed candle at :02, entry at the next :00
                                                        # early52: old behaviour, evaluate FORMING candle at :52, entry at next :00
SCAN_SECOND     = 52 if SCAN_MODE == "early52" else 2

# ---- pre-entry confirmation (re-check forming candle just before entry) ----
PRECHECK_MODE   = _env("PRECHECK_MODE", default="confirm")  # confirm | log | off
PRECHECK_SECOND = int(_env("PRECHECK_SECOND", default="57"))
PC_SMALL_BODY_ATR   = 0.30   # body < 0.30*ATR counts as "small" (neutral, OK)
PC_STRONG_BODY_ATR  = 0.60   # opposing body >= 0.60*ATR = strongly against -> cancel
PC_MAX_ADVERSE_ATR  = 0.50   # price moved > 0.50*ATR against the signal -> cancel
PC_EXHAUST_ATR      = 1.50   # price ran > 1.5*ATR in signal direction -> edge consumed, cancel
PC_USE_EMA9         = True   # cancel if price crossed back through EMA9
PC_USE_SIG_MID      = True   # cancel if price crossed back through signal candle midpoint
EXPIRY_MIN      = 1
SIGNAL_COOLDOWN_MIN = 3
RESULT_COOLDOWN_MIN = 2
SEND_NO_SETUP   = True   # admin chat only
GROUP_SHOW_RESULTS = _env("GROUP_SHOW_RESULTS", default="true").lower() in ("1", "true", "yes")
RESULT_CHART = _env("RESULT_CHART", default="true").lower() in ("1", "true", "yes")  # photo result; text fallback
SIGNAL_CHART = _env("SIGNAL_CHART", default="true").lower() in ("1", "true", "yes")  # chart photo on the signal card
CHART_DIR = os.path.join(STATE_DIR, "charts")  # used when Telegram is disabled
COMPOUND_TARGET = 5          # ladder steps counted as a completed ladder
MAX_LOST_LADDERS_PER_DAY = 2
COMPOUND_MIN_TRADES = int(_env("COMPOUND_MIN_TRADES", default="30"))  # settled trades (cancels/ties excluded) needed to rotate INTO compounding # rotate a compounding pair out after this many lost ladders today

# ---- indicators ----
EMA_FAST, EMA_SLOW, EMA_SLOPE_LOOKBACK = 9, 21, 3
RSI_LEN = 7
STOCH_K, STOCH_SMOOTH, STOCH_D = 5, 3, 3
SR_LEN, BB_LEN, BB_STD = 20, 20, 2.0
ATR_LEN, ATR_PCTL_LOOKBACK, ATR_PCTL_MIN, ATR_PCTL_MAX = 14, 240, 20, 95
ADX_LEN, ADX_TREND, ADX_RANGE = 14, 25, 18
ER_LEN, ER_TREND = 10, 0.35
CHOP_PCT, CHOP_BARS, CHOP_WINDOW = 0.0008, 4, 5
SR_MIN_RANGE_ATR = 3.0
BODY_MIN, WICK_MAX = 0.60, 0.30
W_TREND = dict(trend=2.0, momentum=1.5, location=1.0, candle=1.0)
W_RANGE = dict(trend=0.0, momentum=1.0, location=2.0, candle=1.0)
MIN_CONF = 0.70
MIN_BARS = 260
OVERLAP_MODE = _env("OVERLAP_MODE", default="log")  # off | log | require (filter only, never creates signals)
OVERLAP_LOOKBACK = 5
MAX_GAP_RUNS = 0         # reject if the recent window has feed gaps

# ---- payout / pair management (NO martingale) ----
MIN_PAYOUT = 0.77        # never signal below 77%
DEFAULT_PAYOUT = 0.85    # used in replay / when book gives no payout
PAYOUT_EDGE_MARGIN = 0.01
ROLLING_N = 50
MIN_TRADES_FOR_GATE = 30

def missing_secrets():
    need = {"OTCHARTS_API_KEY": OTCHARTS_API_KEY if DATA_SOURCE == "otcharts" else "n/a"}
    if TELEGRAM_ENABLED:
        need.update(TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID=TELEGRAM_CHANNEL_ID,
                    TELEGRAM_ADMIN_ID=TELEGRAM_ADMIN_ID)
    return [k for k, v in need.items() if not v]
