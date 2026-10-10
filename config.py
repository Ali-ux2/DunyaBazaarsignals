import os
from dotenv import load_dotenv

load_dotenv()

# --- Credentials ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
OTCHARTS_KEY = os.environ.get("OTCHARTS_API_KEY")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")

# --- Trading Settings (12 Pairs) ---
PAIRS = [
    "USDBRL_otc", "USDMXN_otc", "USDINR_otc", "USDPKR_otc",
    "USDDZD_otc", "USDARS_otc", "USDBDT_otc", "USDCOP_otc",
    "USDIDR_otc", "USDPHP_otc", "USDEGP_otc", "USDNGN_otc"
]

# --- Confluence Settings ---
EMA_SHORT = 9
EMA_MID = 21
EMA_LONG = 50
RSI_PERIOD = 14
ADX_PERIOD = 14
ADX_MIN = 25
BB_PERIOD = 20
BB_STD = 2.0

CONFLUENCE_THRESHOLD = 6

# --- M15 Higher-Timeframe Trend Filter ---
TREND_EMA_SHORT = 9
TREND_EMA_LONG = 21

# --- Timing (M5) ---
TRADE_DURATION_MINUTES = 5   # M5 trade
