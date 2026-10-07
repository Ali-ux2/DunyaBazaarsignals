import asyncio
import logging
from datetime import datetime, timedelta
from config import BOT_TOKEN, CHANNEL_ID, OTCHARTS_KEY, PAIRS, SIGNAL_INTERVAL_MINUTES, TRADE_DURATION_MINUTES, MTG_MAX_STEPS
from analyzer import S3Analyzer
from otcharts import Client, QuotaExceeded
from telegram import Bot
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

otc = Client(api_key=OTCHARTS_KEY)
bot = Bot(token=BOT_TOKEN)
analyzer = S3Analyzer()

# Global state to track active trades
# Format: {pair: {'direction': 'CALL', 'entry_price': 1.23, 'step': 1, 'expiry_time': datetime_obj}}
active_trades = {}

async def send_message(text: str):
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown")
        logger.info(f"Sent: {text.splitlines()[0]}")
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")

async def run_sniper_loop():
    logger.info(f"Starting DunyaBazaar M1 Sniper Bot (Signals every {SIGNAL_INTERVAL_MINUTES} mins)...")
    
    # Warm up analyzer with historical 1-minute candles
    for pair in PAIRS:
        try:
            bars = otc.candles("quotex", pair, tf=60, limit=50)
            for bar in bars:
                analyzer.analyze(pair, bar.close)
            logger.info(f"Warmed up {pair}")
        except Exception as e:
            logger.error(f"Warmup failed for {pair}: {e}")

    logger.info("Warmup complete. Entering precise loop.")

    while True:
        now = datetime.now()
        seconds = now.second
        minutes_mod = now.minute % SIGNAL_INTERVAL_MINUTES

        # --- PHASE 1: SEND INITIAL SIGNAL (At :52 seconds of the scan minute) ---
        # e.g., at 10:59:52 if interval is 5 minutes
        if minutes_mod == (SIGNAL_INTERVAL_MINUTES - 1) and seconds == 52:
            logger.info(f"Scanning for signals at {now.strftime('%H:%M:%S')}...")
            for pair in PAIRS:
                try:
                    # Fetch last 2 candles to check for fresh crossover
                    bars = otc.candles("quotex", pair, tf=60, limit=2)
                    if not bars: continue
                    
                    latest_price = bars[-1].close
                    direction, strength = analyzer.analyze(pair, latest_price)
                    
                    # Only trigger if no active trade AND we have a fresh crossover
                    if direction and pair not in active_trades and analyzer.last_signal.get(pair) != direction:
                        # Entry is 8 seconds from now (the next minute mark)
                        entry_time = (now + timedelta(seconds=8)).replace(second=0, microsecond=0)
                        expiry_time = entry_time + timedelta(minutes=TRADE_DURATION_MINUTES)
                        
                        active_trades[pair] = {
                            'direction': direction,
                            'entry_price': latest_price,
                            'step': 1,
                            'expiry_time': expiry_time
                        }
                        analyzer.last_signal[pair] = direction
                        
                        arrow = "🟢" if direction == "CALL" else "🔴"
                        entry_str = entry_time.strftime("%H:%M")
                        
                        text = (
                            f"🚀 *Signal ready: {pair.upper().replace('_OTC', '')}*\n"
                            f"Direction: {arrow} *{direction}*\n"
                            f"Timeframe: *M1*\n"
                            f"Entry Time: `{entry_str}`\n"
                            f"Price: `{latest_price:.5f}`\n"
                            f"Strength: `{strength*100:.2f}%`\n"
                            f"⚙️ *MTG Plan:* Step 1. If loss, double amount at {entry_str[0:3]}:00 and follow *same direction*.\n"
                            f"—\n"
                            f"🌍 DunyaBazaar"
                        )
                        await send_message(text)
                        await asyncio.sleep(8) # 8-second delay between pairs if multiple trigger
                except Exception as e:
                    logger.error(f"Error scanning {pair}: {e}")

        # --- PHASE 2: EVALUATE OUTCOME (At :01 seconds of every minute) ---
        elif seconds == 1:
            for pair in list(active_trades.keys()):
                try:
                    trade = active_trades[pair]
                    
                    # Check if this trade's expiry time is right now (or a few seconds ago)
                    if now >= trade['expiry_time'] and now < trade['expiry_time'] + timedelta(seconds=5):
                        # Fetch the closing price of the just-expired 1-minute candle
                        bars = otc.candles("quotex", pair, tf=60, limit=1)
                        if not bars: continue
                        
                        close_price = bars[-1].close
                        direction = trade['direction']
                        entry_price = trade['entry_price']
                        step = trade['step']

                        is_win = (direction == 'CALL' and close_price > entry_price) or \
                                 (direction == 'PUT' and close_price < entry_price)

                        if is_win:
                            if step == 1:
                                text = f"✅ *{pair.upper().replace('_OTC', '')}*\nResult: *WIN*\nCycle closed."
                            else:
                                text = f"✅ *{pair.upper().replace('_OTC', '')}*\nResult: *WIN BY MTG*\nCycle closed."
                            await send_message(text)
                            del active_trades[pair]
                        
                        else: # LOSS
                            if step < MTG_MAX_STEPS:
                                # Silent MTG Step: Update state, do NOT send a message
                                logger.info(f"Loss on {pair} Step {step}. Silently moving to Step {step+1}.")
                                trade['step'] += 1
                                trade['entry_price'] = close_price
                                # Set next expiry for 1 minute from now
                                trade['expiry_time'] = now.replace(second=0, microsecond=0) + timedelta(minutes=TRADE_DURATION_MINUTES)
                            else:
                                # Max MTG reached, cycle is OVER
                                text = f"❌ *{pair.upper().replace('_OTC', '')}*\nResult: *LOSS* (Max MTG Reached)\nCycle closed."
                                await send_message(text)
                                del active_trades[pair]
                                
                except Exception as e:
                    logger.error(f"Error evaluating {pair}: {e}")

        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(run_sniper_loop())
