import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
OTCHARTS_KEY = os.environ.get("OTCHARTS_API_KEY")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")

PAIRS = [
    "USDBRL_otc", "USDMXN_otc", "USDINR_otc", "USDPKR_otc",
    "USDDZD_otc", "USDARS_otc", "USDBDT_otc", "USDCOP_otc",
    "USDIDR_otc", "USDPHP_otc", "USDEGP_otc", "USDNGN_otc"
]

EMA_SHORT = 9
EMA_MID = 21
EMA_LONG = 50
RSI_PERIOD = 14
ADX_PERIOD = 14
ADX_MIN = 25
BB_PERIOD = 20
BB_STD = 2.0
CONFLUENCE_THRESHOLD = 6

# Higher-timeframe trend (M5)
TREND_EMA_SHORT = 9
TREND_EMA_LONG = 21
TREND_TIMEFRAME = 300      # M5
CANDLE_TIMEFRAME = 60      # M1

# Cooldowns for M1
SIGNAL_COOLDOWN_MINUTES = 3        # <-- Hard 3-min gap between signals
COOLDOWN_AFTER_RESULT_MINUTES = 2  # Extra cooldown after result (if longer)

TRADE_DURATION_MINUTES = 1