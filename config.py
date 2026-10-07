import os
from dotenv import load_dotenv

load_dotenv()

# --- Credentials ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
OTCHARTS_KEY = os.environ.get("OTCHARTS_API_KEY")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")  # <-- Add this

# --- Trading Settings (7 Pairs) ---
PAIRS = [
    "USDBRL_otc", 
    "USDMXN_otc", 
    "USDINR_otc", 
    "USDPKR_otc", 
    "USDARS_otc", 
    "USDBDT_otc", 
    "USDCOP_otc"
]

# --- S3 Strategy Parameters ---
SHORT_WINDOW = 10
LONG_WINDOW = 30

# --- Timing Settings ---
SIGNAL_INTERVAL_MINUTES = 5  # Scan for new signals every 5 minutes
TRADE_DURATION_MINUTES = 1    # Trades expire after 1 minute
MTG_MAX_STEPS = 2            # 1 = Base bet, 2 = Double once. Stop after 2 losses.
