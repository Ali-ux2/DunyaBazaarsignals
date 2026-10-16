"""All tunables. Secrets come ONLY from environment variables (Railway Variables)."""
import os

def _env(*names, default=None):
    for n in names:
        v = os.environ.get(n)
        if v not in (None, ""):
            return v
    return default

def _env_chat(*names, default=None):
    """Channel/chat id from env: strip whitespace/quotes; keep as string (Telegram accepts '-100…' strings; do not int())."""
    v = _env(*names, default=default)
    if v is None: return default
    v = str(v).strip().strip('"').strip("'")
    return v if v else default

# ---- secrets / ids (primary name first, then fallbacks) ----
OTCHARTS_API_KEY    = _env("OTCHARTS_API_KEY", "OTCHARTS_KEY", "OTC_API_KEY")
TELEGRAM_BOT_TOKEN  = _env("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "BOT_TOKEN")
TELEGRAM_CHANNEL_ID = _env_chat("TELEGRAM_CHANNEL_ID", "CHANNEL_ID", "CHAT_ID", "TELEGRAM_CHAT_ID")
TELEGRAM_ADMIN_ID   = _env_chat("TELEGRAM_ADMIN_ID", "ADMIN_CHAT_ID", "ADMIN_ID")
# Public / VIP channel switch (optional). VIP falls back to TELEGRAM_CHANNEL_ID (today: DUNYABAZAAR VIP).
PUBLIC_CHANNEL_ID   = _env_chat("PUBLIC_CHANNEL_ID")
VIP_CHANNEL_ID      = _env_chat("VIP_CHANNEL_ID", "TELEGRAM_CHANNEL_ID", "CHANNEL_ID", "CHAT_ID", "TELEGRAM_CHAT_ID")
def channel_ids():
    return {"public": PUBLIC_CHANNEL_ID, "vip": VIP_CHANNEL_ID, "bug": BUG_CHANNEL_ID, "shadow": SHADOW_CHANNEL_ID}

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
CROSS_BOOK_PAYOUT = (float(_env("CROSS_BOOK_PAYOUT", default="")) if _env("CROSS_BOOK_PAYOUT", default="") else None)  # fail closed: unknown unless explicitly set  # Quotex payout assumed for a cross-book pair (check it manually on Quotex)
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
LEVEL_AUTO_MIN, LEVEL_AUTO_MAX = 1, 4   # auto never goes to 5 (live shadow at L5: 8W/14L 36% on 2026-10-15); /level 5 manual only
for _k, _v in LEVELS[3].items(): globals()[_k] = _v                 # strict defaults = level 3
ENTRY_CHECK = _env("ENTRY_CHECK", default="1").lower() in ("1", "true", "yes", "on")   # closed candle overextended from EMA9 -> no group signal
STRATEGY_STATS_FILE = os.path.join(STATE_DIR, "strategy_stats.json")

# ---- bug / glitch watcher (EXPERIMENTAL, admin only) ----
BUG_WATCH = _env("BUG_WATCH", default="1").lower() in ("1", "true", "yes", "on")
BUG_FILE = os.path.join(STATE_DIR, "bug_watch.json")
BUG_MIN_SAMPLES = 40
BUG_MIN_RATE = 0.68
BUG_MAX_P = 0.01               # one-sided binomial p-value vs 50% (keeps random data from triggering)
BUG_SCAN_MIN = 5               # non-stream pairs: re-scan history every N minutes (only candles already fetched)
BUG_STREAM_MIN_SAMPLES = 15     # live stream pairs: scanned every closed candle with a lower sample minimum
BUG_PROMOTE_MIN_FW = 20         # daily review: forward paper trades needed to promote/demote
BUG_PROMOTE_MARGIN = 0.05       # promote to 'candidate' at >= breakeven + 5 pts
BUG_ALERT_COOLDOWN_H = 6       # same pattern on the same pair alerts at most once per 6 h
BUG_MAX_ALERTS_PER_HOUR = 3
BUG_BIG_ATR = 1.2
BUG_SAME_BODY_FRAC = 0.05
# bug SIGNALS (live): admin (default) | group | candidates | off  (runtime: /bugsignals)
BUG_CHANNEL_ID = _env_chat("BUG_CHANNEL_ID")                     # optional dedicated 🐞 bug group
BUG_SIGNALS = _env("BUG_SIGNALS", default="admin").lower()   # admin | group | candidates | off — extra routing only; the bug group (if set) always gets bug signals
BUG_SIGNALS_FILE = os.path.join(STATE_DIR, "bug_signals.json")
BUG_SIGNAL_COOLDOWN_MIN = 5    # per pattern/strategy
BUG_PAIR_COOLDOWN_MIN = 3      # per pair
BUG_DEMOTE_MIN_FW = 10         # below breakeven after >= 10 forward trades -> stops firing
BUG_STRAT_VARIANT = _env("BUG_STRAT_VARIANT", default="improved").lower()   # improved | original
BUG_STRATS = [s.strip().upper() for s in _env("BUG_STRATS", default="B1,B2,B3,B4,B5,B6,E,F").split(",") if s.strip()]
B1_SPIKE_X, B1_WICK_FRAC, B1_WICK_X, B1_CHECK_SECOND = 3.0, 0.60, 2.5, 30
B2_BODY_ATR, B2_BODY_FRAC, B2_DIST_ATR, B2_LOOKBACK, B2_TOUCHES = 0.8, 0.6, 0.3, 50, 2
B3_ZZ_DEV_ATR, B3_ZZ_DEPTH, B3_PUSH = 1.5, 12, 3      # identical consecutive bodies on >= 5% of bars (and >= BUG_MIN_SAMPLES) = glitch

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

# ---- telemetry (admin only, no requests) ----
TELEMETRY = _env("TELEMETRY", default="1") == "1"
TELEMETRY_CSV = _env("TELEMETRY_CSV", default="1") == "1"
TELEMETRY_MAX_MB = float(_env("TELEMETRY_MAX_MB", default="20"))
TELEMETRY_TICK_SAMPLE = int(_env("TELEMETRY_TICK_SAMPLE", default="10"))   # 1 in N normal ticks to CSV; anomalies always

# ---- strategies E/F (ensemble members + bug-style signals) and bug strategies B4-B6 ----
E_ZZ_DEPTH, E_ZZ_BACKSTEP, E_ZZ_DEV_POINTS, E_ZZ_DEV_ATR = 12, 3, 5, 1.0   # dev = max(5 points, 1.0 ATR) on 1m OTC (see README)
E_DEM_N, E_DEM_LO, E_DEM_HI = 14, 0.30, 0.70
F_RSI_N, F_RSI_LO, F_RSI_HI, F_CCI_N, F_CCI_X, F_LOOK = 12, 20, 80, 17, 100, 3
B4_FREEZE_S = float(_env("B4_FREEZE_S", default="2.5"))     # seconds without price change
B4_MOM_BARS = 6                                               # 5 s bars of trend momentum (30 s)
B5_SUB_SECONDS = (10, 15); B5_N, B5_BODY_FRAC = 5, 0.70
B6_BB = ((14, 2.0), (20, 2.0))
TICK_CHECK_SECONDS = (15, 30, 45)                             # stream tick strategies (B1/B4/B5s) are checked at these seconds
ENSEMBLE_EF = _env("ENSEMBLE_EF", default="1") == "1"         # E/F vote in the group ensemble
LB_MIN_N, LB_MUTE_N, LB_BOOST_PTS = 20, 30, 0.03              # leaderboard: min trades, mute after, boost margin
LB_W_MIN, LB_W_MAX = 0.5, 1.5
GROUP_FILTER = _env("GROUP_FILTER", default="best").lower()          # best | all
BEST_BOOTSTRAP = _env("BEST_BOOTSTRAP", default="ensemble").lower()  # ensemble | silent (until a strategy qualifies)
PUBLIC_SESSION_START = _env("PUBLIC_SESSION_START", default="10:30")          # local (UTC+3) time the PUBLIC session opens
PUBLIC_NET_TARGET = int(_env("PUBLIC_NET_TARGET", default="2"))               # close when public wins - losses reaches this (0 = no cap)
PUBLIC_MAX_SIGNALS = int(_env("PUBLIC_MAX_SIGNALS", default="10"))            # safety stop
PUBLIC_MAX_NET_LOSS = int(_env("PUBLIC_MAX_NET_LOSS", default="-3"))          # safety stop
PUBLIC_TARGET_MSG = _env("PUBLIC_TARGET_MSG", default="1") == "1"
PUBLIC_TARGET_TEXT = _env("PUBLIC_TARGET_TEXT", default="✅ Daily target reached ({w}W/{l}L, net +{net}) — see you tomorrow. More signals in VIP: @DBSdesk")
PUBLIC_STOP_TEXT = _env("PUBLIC_STOP_TEXT", default="⏹ Public session closed for today: {w}W/{l}L (net {net:+d}). Back tomorrow at {start} UTC+3. More signals in VIP: @DBSdesk")
PUBLIC_CAP_FILE = os.path.join(STATE_DIR, "public_cap.json")
# "wins" (default, matches the user's examples W,W / W,L,W / W,L,L,W,W): close when wins >= target AND net >= +1.
# "net": close when wins - losses >= target (strict reading; W,L,W would NOT close).
PUBLIC_CLOSE_RULE = _env("PUBLIC_CLOSE_RULE", default="wins").lower()
SCHED_MIN_GAP_S = float(_env("SCHED_MIN_GAP_S", default="0.05"))    # global gap between scheduled API calls
SCHED_PER_CHAT_GAP_S = float(_env("SCHED_PER_CHAT_GAP_S", default="1.0"))
PUBLIC_START_TEXT = _env("PUBLIC_START_TEXT", default="🟢 Public session is open — signals start now. Good luck! VIP: @DBSdesk")
BUG_TIME_PATTERNS = _env("BUG_TIME_PATTERNS", default="0") == "1"   # hour-of-day / minute colour bias (false-positive source) off by default
BUG_LIVE_MIN_FW = 10                                                  # statistical patterns signal only after >=10 forward trades at >= breakeven
ENSEMBLE_ONLY = ""                                                    # shadow at live>=4: "BC" = single B or C strategy test
STRAT_PRIOR = {"B": 1.2, "C": 1.2}                                    # initial ensemble weights; leaderboard overrides once >=20 trades
DAY_STATE_FILE = os.path.join(STATE_DIR, "day_state.json")   # today's W/L, switches, ladders: survive restarts within the UTC day
STATE_MARKER = os.path.join(STATE_DIR, ".state_marker")
# ---- historical confirmation gate (all signals) ----
HIST_GATE = _env("HIST_GATE", default="all").lower()               # all | bugs | off
HIST_MARGIN = float(_env("HIST_MARGIN", default="0.03"))
HIST_MIN_N = int(_env("HIST_MIN_N", default="15"))
HIST_W7 = 2                                                          # 7d occurrences weigh double
HIST_MIN_COVER_DAYS = float(_env("HIST_MIN_DAYS", "HIST_MIN_COVER_DAYS", default="7"))   # HIST_MIN_DAYS; /history days N
BUG_WARMUP = _env("BUG_WARMUP", default="on").lower() in ("on", "1", "true", "yes")       # bug group only, while < HIST_MIN_DAYS stored
BUG_WARMUP_MIN_N = 5; BUG_WARMUP_MARGIN = 0.05; BUG_WARMUP_MAX_HOUR = 4
HIST_KEEP_DAYS = 30
HIST_BACKFILL_MAX_REQ = int(_env("HIST_BACKFILL_MAX_REQ", default="800"))
HIST_BACKFILL_LIMIT = int(_env("HIST_BACKFILL_LIMIT", default="10000"))   # bars per backfill call (OTCharts max may be lower)
HIST_SAVE_SEC = 900
HIST_CHECK_BUDGET_S = float(_env("HIST_CHECK_BUDGET_S", default="2"))   # max seconds a gate check may scan before it says "warming up"
HIST_WARM_BUDGET_S = float(_env("HIST_WARM_BUDGET_S", default="3"))     # background warm-up seconds per minute
FILL_MIN_HOUR = _env("FILL_MIN_HOUR", default="1") == "1"            # Tier-2 fill to reach MIN_SIGNALS_PER_HOUR
FILL_START_MIN = int(_env("FILL_START_MIN", default="0"))            # paced fill: may start from :00
FILL_SLOT_MIN = int(_env("FILL_SLOT_MIN", default="15"))             # pacing: aim for one signal per 15-minute slot
FILL_SLOT_WAIT = int(_env("FILL_SLOT_WAIT", default="3"))            # minutes into a slot that Tier 1 gets first before a fill
FILL_PAIR_GAP_MIN = int(_env("FILL_PAIR_GAP_MIN", default="10"))     # at most 1 fill per pair per 10 min
FILL_WICK_MIN = float(_env("FILL_WICK_MIN", default="0.40"))
FILL_LEVEL = int(_env("FILL_LEVEL", default="4"))                    # fill candidates may use this level's confidence thresholds
TIERS_FILE = os.path.join(STATE_DIR, "tiers.json")
# ---- loss-streak cooldown / blackouts / news ----
LOSS_STREAK_N = int(_env("LOSS_STREAK_N", default="3"))
STREAK_COOLDOWN_MIN = int(_env("COOLDOWN_MIN", default="30"))
BLACKOUT_ON = _env("BLACKOUT", default="on").lower() != "off"
# zone-local windows (DST-aware): London open 08:00 −15/+30, NY open 09:30 ±15, rollover/Tokyo 00:00 UTC ±15
# = 09:45-10:30 / 16:15-16:45 / 02:45-03:15 UTC+3 during European/US summer time; they move by 1 h in winter (zoneinfo handles it).
BLACKOUT_WINDOWS = _env("BLACKOUT_WINDOWS", default="Europe/London@07:45-08:30,America/New_York@09:15-09:45,UTC@23:45-00:15")
NEWS_FILTER = _env("NEWS_FILTER", default="on").lower() != "off"
NEWS_BLACKOUT_MIN = int(_env("NEWS_BLACKOUT_MIN", default="15"))
NEWS_URL = _env("NEWS_URL", default="https://nfs.faireconomy.media/ff_calendar_thisweek.json")
NEWS_REFRESH_H = 6
DOUBLE_CONFIRM = _env("DOUBLE_CONFIRM", default="on").lower() != "off"          # VIP / public / bug group
DOUBLE_CONFIRM_SHADOW = _env("DOUBLE_CONFIRM_SHADOW", default="off").lower() == "on"   # shadow channel + paper (unproven) only
# ---- candle-pattern family P (patterns.py) ----
PATTERNS_ON = _env("PATTERNS", default="on").lower() != "off"
P_LIVE_MIN = int(_env("P_LIVE_MIN", default="20"))          # admin-only until ≥20 forward trades at ≥ breakeven
# ---- daily stop / weekly tune ----
DAILY_STOP_NET = int(_env("DAILY_STOP_NET", default="5"))      # net losses (losses − wins) on public/VIP → stop until DAILY_STOP_RESUME
DAILY_STOP_RESUME = _env("TRADING_DAY_START", "DAILY_STOP_RESUME", default="08:00")   # UTC+3: trading day for the daily stops resets (and a stopped day reopens) here
WEEKLY_TUNE = _env("WEEKLY_TUNE", default="on").lower() != "off"   # Sunday 03:00 UTC+3: ablation + proposals (auto-apply safe bounds only)
WEEKLY_TUNE_APPLY = _env("WEEKLY_TUNE_APPLY", default="on").lower() != "off"
DAILY_PROFIT_ALERT = int(_env("DAILY_PROFIT_ALERT", default="15"))   # net wins (W − L) → one-time admin alert
DAILY_PROFIT_CLOSE = int(_env("DAILY_PROFIT_CLOSE", default="20"))   # net wins → live signals closed until the trading-day start (08:00) next day
# ---- volume mode (Tier 3) ----
VOLUME_MODE = _env("VOLUME_MODE", default="on").lower() != "off"     # user request: 4–6 signals per open hour
VOLUME_TARGET = int(_env("VOLUME_TARGET", default="4"))
VOLUME_MAX_HOUR = int(_env("VOLUME_MAX_HOUR", default="6"))           # cap for fills (Tier 2 + 3) per hour
VOLUME_SLOT_MIN = int(_env("VOLUME_SLOT_MIN", default="10"))          # pace ≈ 1 per 10 min
VOLUME_GUARD_N = int(_env("VOLUME_GUARD_N", default="40"))            # alert if Tier 3 < breakeven after this many live trades
VERBOSE = _env("VERBOSE", default="off").lower() == "on"           # per-drop admin notes (single-confirmation / filtered by history)
SHADOW_CHANNEL_ID = _env_chat("SHADOW_CHANNEL_ID", default="")      # shadow + paper (admin-only/unproven) signals go here; unset = admin chat
FEEDBACK_KEEP_DAYS = int(_env("FEEDBACK_KEEP_DAYS", default="30"))   # STATE_DIR/feedback/*.jsonl rotation
PAYOUT_MAX_AGE_SEC = int(_env("PAYOUT_MAX_AGE_SEC", default="600"))   # payout older than this = unknown = BLOCKED (fail closed)
PAYOUT_REFRESH_PAUSED_SEC = int(_env("PAYOUT_REFRESH_PAUSED_SEC", default="540"))   # payout refresh cadence even when REST candles are paused for budget (~160 req/day)
