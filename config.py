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
# Public / VIP channel switch (optional). VIP falls back to TELEGRAM_CHANNEL_ID (today: DUNYABAZAAR VIP).
PUBLIC_CHANNEL_ID   = _env("PUBLIC_CHANNEL_ID")
VIP_CHANNEL_ID      = _env("VIP_CHANNEL_ID", "TELEGRAM_CHANNEL_ID", "CHANNEL_ID", "CHAT_ID", "TELEGRAM_CHAT_ID")
def channel_ids(): return {"public": PUBLIC_CHANNEL_ID, "vip": VIP_CHANNEL_ID}

# ---- runtime mode ----
DATA_SOURCE     = _env("DATA_SOURCE", default="otcharts")   # otcharts | replay | mockstream (tests)
MODE            = _env("MODE", default="auto").lower()       # auto (default: poll all PAIRS, stream STREAM_PAIRS near budget limit) | poll | stream
REPLAY_CSV      = _env("REPLAY_CSV", default="replay_m1.csv")
TELEGRAM_ENABLED = _env("TELEGRAM_ENABLED", default="1") == "1"
DRY_RUN_MINUTES = int(_env("DRY_RUN_MINUTES", default="0"))  # replay: stop after N simulated minutes
STATE_DIR       = _env("STATE_DIR", default=".")             # mount a Railway volume here
STATS_FILE      = os.path.join(STATE_DIR, "pair_stats.json")
TRADE_LOG       = os.path.join(STATE_DIR, "trades.csv")
CHANNEL_TARGET_FILE = os.path.join(STATE_DIR, "channel_target.json")
BENCH_FILE = os.path.join(STATE_DIR, "benched.json")   # pairs benched today after MAX_LOST_LADDERS_PER_DAY lost ladders

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

_DEFAULT_PAIRS = ["USDBRL", "USDMXN", "USDINR", "USDPKR", "USDDZD", "USDARS", "USDBDT", "USDCOP", "USDIDR", "USDPHP", "USDEGP", "USDNGN", "NZDUSD", "USDZAR", "EURNZD"]
# All 12 enabled; the live payout gate (MIN_PAYOUT 77%) filters low-payout pairs per signal.
DISABLED_PAIRS: list = []   # move pairs here (and out of _DEFAULT_PAIRS) to disable
PAIRS = [p.strip().upper() for p in _env("PAIRS", default=",".join(_DEFAULT_PAIRS)).split(",") if p.strip()]

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

# ---- display time zone (everything shown to people). Internal logic, candles, OTCharts, budget reset and the
# daily-stats day boundary stay UTC (00:00 UTC = 03:00 UTC+3).
DISPLAY_TZ_OFFSET_HOURS = float(_env("DISPLAY_TZ_OFFSET_HOURS", "DISPLAY_TZ", default="3").upper().replace("UTC", "") or 0)
def _tz_label(h):
    if h == 0: return "UTC"
    hh = int(abs(h)); mm = int(round((abs(h) - hh) * 60))
    return f"UTC{'+' if h > 0 else '-'}{hh}" + (f":{mm:02d}" if mm else "")
TZ_LABEL = _tz_label(DISPLAY_TZ_OFFSET_HOURS)
def local(dt):
    """UTC datetime/Timestamp -> display time (same object type, shifted)."""
    from datetime import timedelta
    return dt + timedelta(hours=DISPLAY_TZ_OFFSET_HOURS) if dt is not None else dt
def hm(dt): return f"{local(dt):%H:%M}"
def reset_local(): return f"{(24 + DISPLAY_TZ_OFFSET_HOURS) % 24:02.0f}:00 {TZ_LABEL}"

# ---- sessions (optional; "HH:MM-HH:MM" UTC, comma-separated; empty = always on) ----
SESSIONS_UTC = _env("SESSIONS_UTC", default="")

# ---- timing ----
SCAN_MODE       = _env("SCAN_MODE", default="closed")   # closed: evaluate closed candle at :02, entry at the next :00
                                                        # early52: old behaviour, evaluate FORMING candle at :52, entry at next :00
SCAN_SECOND     = 52 if SCAN_MODE == "early52" else 2

# ---- pre-entry confirmation (re-check forming candle just before entry) ----
PRECHECK_MODE   = _env("PRECHECK_MODE", default="direction")  # direction (never skip: confirm or switch) | confirm | log | off
PRECHECK_SECOND = int(_env("PRECHECK_SECOND", default="57"))
PC_SMALL_BODY_ATR   = 0.30   # body < 0.30*ATR counts as "small" (neutral, OK)
PC_STRONG_BODY_ATR  = 0.60   # opposing body >= 0.60*ATR = strongly against -> cancel
PC_MAX_ADVERSE_ATR  = 0.50   # price moved > 0.50*ATR against the signal -> cancel
SWITCH_MIN_BODY_ATR = float(_env("SWITCH_MIN_BODY_ATR", default="0.8"))  # direction mode: SWITCH only if the forming candle is clearly opposite (body >= this, in ATR); else keep
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
ATR_LEN, ATR_PCTL_LOOKBACK, ATR_PCTL_MIN, ATR_PCTL_MAX = 14, 240, 15, 96
ADX_LEN, ADX_TREND, ADX_RANGE = 14, 23, 19
ER_LEN, ER_TREND = 10, 0.30
CHOP_PCT, CHOP_BARS, CHOP_WINDOW = 0.0008, 4, 5
SR_MIN_RANGE_ATR = 2.5
BODY_MIN, WICK_MAX = 0.55, 0.30
W_TREND = dict(trend=2.0, momentum=1.5, location=1.0, candle=1.0)
W_RANGE = dict(trend=0.0, momentum=1.0, location=2.0, candle=1.0)
MIN_CONF = 0.62
STREAM_WARMUP_BARS = max(30, min(50, int(_env("STREAM_WARMUP_BARS", default="50"))))  # /v1/candles per pair on every stream open
MIN_BARS = int(_env("MIN_BARS", default="50" if MODE == "stream" or DATA_SOURCE == "mockstream" else "260"))
POLL_MIN_BARS = MIN_BARS
STREAM_MIN_BARS = int(_env("MIN_BARS", default=str(STREAM_WARMUP_BARS)))  # auto mode: stream phase signals right after instant warm-up
ATR_PCTL_MIN_PERIODS = 30   # ATR percentile uses whatever window exists (>=30 bars); noisier until ~240 bars
OVERLAP_MODE = _env("OVERLAP_MODE", default="log")  # off | log | require (filter only, never creates signals)
OVERLAP_LOOKBACK = 5
MAX_GAP_RUNS = 0         # reject if the recent window has feed gaps
ALLOW_CONFLICT = False   # strict: any opposing vote rejects the setup

# ---- relaxed SHADOW engine (admin chat only; never posted, never touches real trades/stats/compounding) ----
SHADOW_RELAXED = _env("SHADOW_RELAXED", default="1").lower() in ("1", "true", "yes", "on")
SHADOW_STATS_FILE = os.path.join(STATE_DIR, "shadow_stats.json")
SHADOW_LOG = os.path.join(STATE_DIR, "shadow_trades.csv")
SHADOW_MAX_WAIT_MIN = 20     # give up resolving a shadow trade if its candle never shows up in already-fetched data
# Overrides applied ONLY inside the shadow evaluation. Payout floor (MIN_PAYOUT) and gap rejection are NOT relaxed.
SHADOW_OVERRIDES = dict(
    MIN_CONF=0.55,           # strict 0.62
    ADX_TREND=21,            # strict 23
    ADX_RANGE=20,            # strict 19 (narrower "unclear" band)
    ER_TREND=0.25,           # strict 0.30
    ATR_PCTL_MIN=10,         # strict 15
    ATR_PCTL_MAX=98,         # strict 96
    SR_MIN_RANGE_ATR=2.0,    # strict 2.5
    BODY_MIN=0.50,           # strict 0.55
    CHOP_BARS=5,             # strict 4 (chop must be 5/5 bars to reject)
    ALLOW_CONFLICT=True,     # strict False: net weighted score decides
    OVERLAP_MODE="off",      # strict log/require: ignored
)

# ---- frequency LEVELS (1 very conservative … 5 extreme). Level 3 = the strict defaults above. ----
# Every level keeps MIN_PAYOUT (77%) and gap rejection. Shadow runs one level looser than the current level (capped at 5).
_LVL_KEYS = ("MIN_CONF", "ADX_TREND", "ADX_RANGE", "ER_TREND", "ATR_PCTL_MIN", "ATR_PCTL_MAX", "SR_MIN_RANGE_ATR",
             "BODY_MIN", "CHOP_BARS", "ALLOW_CONFLICT", "ENSEMBLE_MIN_AGREE", "ENSEMBLE_STRONG_OPP", "B_BB_Z", "C_WICK_MIN",
             "ENTRY_MAX_EXT_ATR")
LEVELS = {
    1: dict(MIN_CONF=0.75, ADX_TREND=27, ADX_RANGE=17, ER_TREND=0.40, ATR_PCTL_MIN=25, ATR_PCTL_MAX=93, SR_MIN_RANGE_ATR=3.5,
            BODY_MIN=0.65, CHOP_BARS=3, ALLOW_CONFLICT=False, ENSEMBLE_MIN_AGREE=3, ENSEMBLE_STRONG_OPP=0.40, B_BB_Z=2.0, C_WICK_MIN=0.60,
            ENTRY_MAX_EXT_ATR=1.5),
    2: dict(MIN_CONF=0.70, ADX_TREND=25, ADX_RANGE=18, ER_TREND=0.35, ATR_PCTL_MIN=20, ATR_PCTL_MAX=95, SR_MIN_RANGE_ATR=3.0,
            BODY_MIN=0.60, CHOP_BARS=4, ALLOW_CONFLICT=False, ENSEMBLE_MIN_AGREE=2, ENSEMBLE_STRONG_OPP=0.50, B_BB_Z=1.8, C_WICK_MIN=0.55,
            ENTRY_MAX_EXT_ATR=1.8),
    3: dict(MIN_CONF=0.62, ADX_TREND=23, ADX_RANGE=19, ER_TREND=0.30, ATR_PCTL_MIN=15, ATR_PCTL_MAX=96, SR_MIN_RANGE_ATR=2.5,
            BODY_MIN=0.55, CHOP_BARS=4, ALLOW_CONFLICT=False, ENSEMBLE_MIN_AGREE=2, ENSEMBLE_STRONG_OPP=0.60, B_BB_Z=1.6, C_WICK_MIN=0.50,
            ENTRY_MAX_EXT_ATR=2.0),
    4: dict(MIN_CONF=0.55, ADX_TREND=21, ADX_RANGE=20, ER_TREND=0.25, ATR_PCTL_MIN=10, ATR_PCTL_MAX=98, SR_MIN_RANGE_ATR=2.0,
            BODY_MIN=0.50, CHOP_BARS=5, ALLOW_CONFLICT=True, ENSEMBLE_MIN_AGREE=2, ENSEMBLE_STRONG_OPP=0.70, B_BB_Z=1.4, C_WICK_MIN=0.45,
            ENTRY_MAX_EXT_ATR=2.5),
    5: dict(MIN_CONF=0.45, ADX_TREND=18, ADX_RANGE=18, ER_TREND=0.20, ATR_PCTL_MIN=5, ATR_PCTL_MAX=99, SR_MIN_RANGE_ATR=1.5,
            BODY_MIN=0.45, CHOP_BARS=6, ALLOW_CONFLICT=True, ENSEMBLE_MIN_AGREE=2, ENSEMBLE_STRONG_OPP=0.80, B_BB_Z=1.2, C_WICK_MIN=0.40,
            ENTRY_MAX_EXT_ATR=3.0),
}
DEFAULT_LEVEL = _env("DEFAULT_LEVEL", default="auto").lower()       # 1..5 or auto (default auto, starting at LEVEL_AUTO_START)
LEVEL_AUTO_START = int(_env("LEVEL_AUTO_START", default="4"))
MIN_SIGNALS_PER_HOUR = float(_env("MIN_SIGNALS_PER_HOUR", default="4"))   # auto: loosen while the last 60 active minutes are below this
LEVEL_RATE_CHECK_MIN = 20      # auto: re-check the signal rate every 20 active minutes
LEVEL_FILE = os.path.join(STATE_DIR, "level.json")
LEVEL_AUTO_EVERY = 20          # settled real trades between auto evaluations
LEVEL_AUTO_LOOSEN_MARGIN = 0.05
LEVEL_AUTO_LOW_RATE = 1.0      # signals/hour over the last 2 h of active time counts as "low"
LEVEL_AUTO_MIN, LEVEL_AUTO_MAX = 1, 5   # level 5 only while the last 20 trades are at/above breakeven
for _k, _v in LEVELS[3].items(): globals()[_k] = _v                 # strict defaults = level 3
ENTRY_CHECK = _env("ENTRY_CHECK", default="1").lower() in ("1", "true", "yes", "on")   # closed candle overextended from EMA9 -> no group signal
STRATEGY_STATS_FILE = os.path.join(STATE_DIR, "strategy_stats.json")

# ---- bug / glitch watcher (EXPERIMENTAL, admin only) ----
BUG_WATCH = _env("BUG_WATCH", default="1").lower() in ("1", "true", "yes", "on")
BUG_FILE = os.path.join(STATE_DIR, "bug_watch.json")
BUG_MIN_SAMPLES = 20
BUG_MIN_RATE = 0.65
BUG_MAX_P = 0.01               # one-sided binomial p-value vs 50% (keeps random data from triggering)
BUG_SCAN_MIN = 5               # non-stream pairs: re-scan history every N minutes (only candles already fetched)
BUG_STREAM_MIN_SAMPLES = 15     # live stream pairs: scanned every closed candle with a lower sample minimum
BUG_PROMOTE_MIN_FW = 20         # daily review: forward paper trades needed to promote/demote
BUG_PROMOTE_MARGIN = 0.05       # promote to 'candidate' at >= breakeven + 5 pts
BUG_ALERT_COOLDOWN_H = 6       # same pattern on the same pair alerts at most once per 6 h
BUG_MAX_ALERTS_PER_HOUR = 3
BUG_BIG_ATR = 1.2
BUG_SAME_BODY_FRAC = 0.05      # identical consecutive bodies on >= 5% of bars (and >= BUG_MIN_SAMPLES) = glitch

# ---- payout / pair management (NO martingale) ----
MIN_PAYOUT = 0.77        # never signal below 77%
DEFAULT_PAYOUT = 0.85    # used in replay / when book gives no payout
PAYOUT_EDGE_MARGIN = 0.01
ROLLING_N = 50
MIN_TRADES_FOR_GATE = 30

# ---- stream mode ----
STREAM_PAIRS = [p.strip().upper() for p in _env("STREAM_PAIRS", default="USDINR,USDIDR,USDCOP,USDMXN,USDBRL").split(",") if p.strip()]
STREAM_MAX_INSTRUMENTS = int(_env("STREAM_MAX_INSTRUMENTS", default="5"))   # Lite plan: 1 stream x 5 instruments
STREAM_429_BACKOFF_SEC = 300       # legacy (unused)
STREAM_429_BACKOFFS = [60, 120, 300, 600]   # 429 retry schedule, capped at 600 s
STREAM_FALLBACK_POLL = _env("STREAM_FALLBACK_POLL", default="1").lower() in ("1", "true", "yes", "on")  # poll stream pairs while the stream is down
STREAM_FALLBACK_RESERVE = int(_env("STREAM_FALLBACK_RESERVE", default="50"))  # keep this many requests unspent
STREAM_WATCHDOG_SEC = 15            # how often the tick watchdog checks
STREAM_SWITCH_AT = float(_env("STREAM_SWITCH_AT", default="0.93"))  # auto: open the stream while its 1 request is still available
STREAM_REOPEN_DROPPED_MIN = 3
STREAM_STALE_SEC = 90               # no tick for this long: feed gap AND the watchdog closes + reconnects the stream
STREAM_FALLBACK_PAYOUT = float(_env("STREAM_FALLBACK_PAYOUT", default="0.85"))  # used only if no live/cached payout
PERSIST_CANDLES = 300
CANDLE_DIR = os.path.join(STATE_DIR, "candles")
PAYOUT_CACHE = os.path.join(STATE_DIR, "payouts.json")
RESOLUTION_CACHE = os.path.join(STATE_DIR, "resolutions.json")
ALL_PAIRS = list(PAIRS)            # full list, kept for /poll even when started with MODE=stream
MODE_CHOICE_FILE = os.path.join(STATE_DIR, "mode_choice.json")
if MODE == "stream": PAIRS = list(STREAM_PAIRS)

def missing_secrets():
    need = {"OTCHARTS_API_KEY": OTCHARTS_API_KEY if DATA_SOURCE == "otcharts" else "n/a"}
    if TELEGRAM_ENABLED:
        need.update(TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN,
                    TELEGRAM_CHANNEL_ID=TELEGRAM_CHANNEL_ID or VIP_CHANNEL_ID or PUBLIC_CHANNEL_ID,
                    TELEGRAM_ADMIN_ID=TELEGRAM_ADMIN_ID)
    return [k for k, v in need.items() if not v]
