import os
from dotenv import load_dotenv

load_dotenv()

# --- Credentials ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
OTCHARTS_API_KEY = os.environ.get("OTCHARTS_API_KEY")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")

# --- ACTIVE PAIR (switch via Railway variable "ACTIVE_PAIR") ---
ACTIVE_PAIR = os.environ.get("ACTIVE_PAIR", "USDBRL_otc")

# Available pairs:
# USDBRL_otc, USDMXN_otc, USDINR_otc, USDPKR_otc, USDDZD_otc, USDARS_otc,
# USDBDT_otc, USDCOP_otc, USDIDR_otc, USDPHP_otc, USDEGP_otc, USDNGN_otc

# --- Confluence ---
MIN_SCORE = 4               # 4 of 6 votes required

# Indicator periods
EMA_TREND_FAST = 9
EMA_TREND_SLOW = 21
EMA_SHORT_FAST = 5
EMA_SHORT_SLOW = 20
RSI_PERIOD = 7
BB_PERIOD = 20
BB_STD = 2.0
STOCH_PERIOD = 5
STOCH_SMOOTH_K = 3
STOCH_SMOOTH_D = 3
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Timeframe
CANDLE_TIMEFRAME = 60
TRADE_DURATION_MINUTES = 1

# Cooldowns
SIGNAL_COOLDOWN_MINUTES = 3
COOLDOWN_AFTER_RESULT_MINUTES = 2

# Analyzer history
CANDLES_TO_TRACK = 100
CANDLES_FOR_WARMUP = 80
CANDLES_MIN_REQUIRED = 50