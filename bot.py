import asyncio
import logging
from datetime import datetime, timedelta
from config import BOT_TOKEN, CHANNEL_ID, OTCHARTS_KEY, ADMIN_ID, PAIRS, SIGNAL_INTERVAL_MINUTES, TRADE_DURATION_MINUTES, MTG_MAX_STEPS
from analyzer import S3Analyzer
from otcharts import Client, QuotaExceeded
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

otc = Client(api_key=OTCHARTS_KEY)
bot = Bot(token=BOT_TOKEN)
analyzer = S3Analyzer()

# --- Global State ---
IS_BOT_ACTIVE = True # Controls the sniper loop
active_trades = {}   # Tracks current MTG cycles

# --- Helper Functions ---
async def send_channel_message(text: str):
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown")
        logger.info(f"Sent to channel: {text.splitlines()[0]}")
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")

# --- Sniper Loop (Runs in the background) ---
async def run_sniper_loop():
    logger.info("Starting DunyaBazaar M1 Sniper Engine...")
    
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
        if not IS_BOT_ACTIVE:
            await asyncio.sleep(2)
            continue

        now = datetime.now()
        seconds = now.second
        minutes_mod = now.minute % SIGNAL_INTERVAL_MINUTES

        # --- PHASE 1: SEND INITIAL SIGNAL (At :52 seconds) ---
        if minutes_mod == (SIGNAL_INTERVAL_MINUTES - 1) and seconds == 52:
            logger.info(f"Scanning for signals at {now.strftime('%H:%M:%S')}...")
            for pair in PAIRS:
                try:
                    bars = otc.candles("quotex", pair, tf=60, limit=2)
                    if not bars: continue
                    
                    latest_price = bars[-1].close
                    direction, strength = analyzer.analyze(pair, latest_price)
                    
                    if direction and pair not in active_trades and analyzer.last_signal.get(pair) != direction:
                        entry_time = (now + timedelta(seconds=8)).replace(second=0, microsecond=0)
                        expiry_time = entry_time + timedelta(minutes=TRADE_DURATION_MINUTES)
                        
                        active_trades[pair] = {
                            'direction': direction, 'entry_price': latest_price,
                            'step': 1, 'expiry_time': expiry_time
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
                        await send_channel_message(text)
                        await asyncio.sleep(8)
                except Exception as e:
                    logger.error(f"Error scanning {pair}: {e}")

        # --- PHASE 2: EVALUATE OUTCOME (At :01 seconds) ---
        elif seconds == 1:
            for pair in list(active_trades.keys()):
                try:
                    trade = active_trades[pair]
                    
                    if now >= trade['expiry_time'] and now < trade['expiry_time'] + timedelta(seconds=5):
                        bars = otc.candles("quotex", pair, tf=60, limit=1)
                        if not bars: continue
                        
                        close_price = bars[-1].close
                        direction = trade['direction']
                        entry_price = trade['entry_price']
                        step = trade['step']

                        is_win = (direction == 'CALL' and close_price > entry_price) or \
                                 (direction == 'PUT' and close_price < entry_price)

                        if is_win:
                            text = f"✅ *{pair.upper().replace('_OTC', '')}*\nResult: *{'WIN' if step == 1 else 'WIN BY MTG'}*\nCycle closed."
                            await send_channel_message(text)
                            del active_trades[pair]
                        else:
                            if step < MTG_MAX_STEPS:
                                logger.info(f"Loss on {pair} Step {step}. Moving to Step {step+1}.")
                                trade['step'] += 1
                                trade['entry_price'] = close_price
                                trade['expiry_time'] = now.replace(second=0, microsecond=0) + timedelta(minutes=TRADE_DURATION_MINUTES)
                            else:
                                text = f"❌ *{pair.upper().replace('_OTC', '')}*\nResult: *LOSS* (Max MTG Reached)\nCycle closed."
                                await send_channel_message(text)
                                del active_trades[pair]
                except Exception as e:
                    logger.error(f"Error evaluating {pair}: {e}")

        await asyncio.sleep(1)

# --- Telegram Command Handlers (Admin Only) ---
def is_admin(update) -> bool:
    return str(update.effective_user.id) == str(ADMIN_ID)

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text("✅ *Bot is now ACTIVE.* Scanning for signals every 5 minutes.")

async def cmd_pause(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = False
    await update.message.reply_text("⏸️ *Bot is PAUSED.* No new signals will be sent. Active MTG cycles will still be tracked.")

async def cmd_resume(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text("▶️ *Bot is RESUMED.* Scanning for new signals.")

async def cmd_feed(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    status = "🟢 ACTIVE" if IS_BOT_ACTIVE else "🔴 PAUSED"
    text = f"📊 *DunyaBazaar Status*\nEngine: {status}\nPairs Tracked: {len(PAIRS)}\n\n"
    
    if not active_trades:
        text += "_No active trades in progress._"
    else:
        text += "*Active MTG Cycles:*\n"
        for pair, trade in active_trades.items():
            text += f"• `{pair}` | Step {trade['step']} | {trade['direction']} | Exp: `{trade['expiry_time'].strftime('%H:%M')}`\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_testcard(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        await send_channel_message("🧪 *Test Signal*\nThis is a test message from DunyaBazaar Bot.\n—\n🌍 DunyaBazaar")
        await update.message.reply_text("✅ Test card sent to the channel successfully.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send test card: {e}")

# --- Main Runner ---
async def main():
    # 1. Build Telegram Application
    application = Application.builder().token(BOT_TOKEN).build()

    # 2. Add Command Handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("feed", cmd_feed))
    application.add_handler(CommandHandler("testcard", cmd_testcard))

    # 3. Start the Sniper Loop in the background
    asyncio.create_task(run_sniper_loop())

    # 4. Start the Telegram Bot Polling
    logger.info("Starting Telegram command listener...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    # Keep the main loop alive
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
