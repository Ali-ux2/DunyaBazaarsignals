import os
from dotenv import load_dotenv

load_dotenv()

# --- Credentials ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
OTCHARTS_API_KEY = os.environ.get("OTCHARTS_API_KEY")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")

# --- Pairs to TRY (bot verifies each at startup, skips broken ones) ---
PAIRS = [
    "USDINR_otc",
    "USDMXN_otc",
    "USDARS_otc",
    "USDPKR_otc",
    "USDBDT_otc",
    "USDCOP_otc",
    "USDPHP_otc",
    "USDIDR_otc",
    "USDEGP_otc",
    "USDNGN_otc",
    "USDDZD_otc",
    "USDBRL_otc",   # may not exist — will be auto-skipped if so
]

# --- Confluence ---
MIN_SCORE = 4

EMA_TREND_FAST = 9
EMA_TREND_SLOW = 21
EMA_SHORT_FAST = 5
EMA_SHORT_SLOW = 20
RSI_PERIOD = 7
STOCH_PERIOD = 5
STOCH_SMOOTH_K = 3
STOCH_SMOOTH_D = 3
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

CANDLE_TIMEFRAME = 60
TRADE_DURATION_MINUTES = 1

SIGNAL_COOLDOWN_MINUTES = 3
COOLDOWN_AFTER_RESULT_MINUTES = 2

CANDLES_TO_TRACK = 100
CANDLES_FOR_WARMUP = 80
CANDLES_MIN_REQUIRED = 50