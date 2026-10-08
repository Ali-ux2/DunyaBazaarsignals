import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import BOT_TOKEN, CHANNEL_ID, OTCHARTS_KEY, ADMIN_ID, PAIRS, TRADE_DURATION_MINUTES, MTG_MAX_STEPS
from analyzer import S3Analyzer
from charting import generate_candle_chart
from otcharts import Client, QuotaExceeded
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

otc = Client(api_key=OTCHARTS_KEY)
bot = Bot(token=BOT_TOKEN)
analyzer = S3Analyzer()

# Set local timezone (GMT+3)
LOCAL_TZ = ZoneInfo("Africa/Nairobi")

IS_BOT_ACTIVE = True
active_trades = {}

# NEW: Dynamic scan timer
next_scan_allowed = datetime.now(LOCAL_TZ) 

# --- Admin Feedback Function ---
async def send_admin_feedback(text: str):
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text)
        logger.info(f"Admin feedback sent: {text}")
    except TelegramError as e:
        logger.error(f"Failed to send admin feedback: {e}")

# --- Channel Signal Sender ---
async def send_signal_with_chart(pair, direction, price, strength, entry_time_str, step):
    try:
        bars = otc.candles("quotex", pair, tf=60, limit=50)
        if not bars:
            return

        chart_buf = generate_candle_chart(bars, pair)

        pair_display = pair.upper().replace('_OTC', '')
        trend = "BULLISH" if direction == "CALL" else "BEARISH"
        arrow = "🟢 CALL ↑" if direction == "CALL" else "🔴 PUT ↓"
        confidence = int(strength * 100)
        mtg_text = f"STEP {step} ({'Base Bet' if step == 1 else 'Recovery'})"
        
        try:
            payouts = otc.payouts("quotex", [pair])
            payout = payouts.get(pair, 89)
        except:
            payout = 89

        text = (
            f"🚀 *Signal ready*\n"
            f"*{pair_display}*\n\n"
            f"------ {{ DUNYABAZAAR }} ------\n\n"
            f"📊 Asset      : `{pair_display}`\n"
            f"📈 Trend      : `{trend}`\n"
            f"Direction    : {arrow}\n"
            f"⏳ Timeframe  : `M1`\n"
            f"🕒 Entry Time : `{entry_time_str} (+3:00 UTC)`\n"
            f"💰 Payout     : `{payout}%`\n\n"
            f"----------\n\n"
            f"✨ AI Confidence : `{confidence}%`\n"
            f"🎲 MTG        : `{mtg_text}`\n\n"
            f"----------\n\n"
            f"💬 CONTACT : @AMNdesk"
        )

        await bot.send_photo(
            chat_id=CHANNEL_ID, 
            photo=chart_buf, 
            caption=text, 
            parse_mode="Markdown"
        )
        await send_admin_feedback(f"🟢 Sent {direction} {pair_display} @ {entry_time_str} (+3:00 UTC)")

    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
    except Exception as e:
        logger.error(f"Error generating signal for {pair}: {e}")

# --- Channel Result Sender ---
async def send_result_message(pair, result_text):
    try:
        text = f"{result_text}\n—\n🌍 DunyaBazaar"
        await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown")
        await send_admin_feedback(f"Result: {result_text}")
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")

# --- Sniper Loop ---
async def run_sniper_loop():
    global next_scan_allowed
    logger.info("Starting DunyaBazaar Adaptive Sniper Engine...")
    
    # Warm up
    for pair in PAIRS:
        try:
            bars = otc.candles("quotex", pair, tf=60, limit=50)
            for bar in bars:
                analyzer.analyze(pair, bar.close)
        except Exception as e:
            logger.error(f"Warmup failed for {pair}: {e}")

    logger.info("Warmup complete. Entering adaptive loop.")

    while True:
        if not IS_BOT_ACTIVE:
            await asyncio.sleep(2)
            continue

        now = datetime.now(LOCAL_TZ)
        seconds = now.second

        # --- PHASE 1: ADAPTIVE SCANNING (Triggers at :52 seconds if allowed) ---
        if seconds == 52 and now >= next_scan_allowed:
            logger.info(f"Scanning for signals at {now.strftime('%H:%M:%S')}...")
            signals_found = 0
            
            for pair in PAIRS:
                try:
                    bars = otc.candles("quotex", pair, tf=60, limit=2)
                    if not bars: 
                        continue
                    
                    latest_price = bars[-1].close
                    direction, strength = analyzer.analyze(pair, latest_price)
                    
                    if direction and pair not in active_trades and analyzer.last_signal.get(pair) != direction:
                        signals_found += 1
                        entry_time = (now + timedelta(seconds=8)).replace(second=0, microsecond=0)
                        expiry_time = entry_time + timedelta(minutes=TRADE_DURATION_MINUTES)
                        
                        active_trades[pair] = {
                            'direction': direction, 
                            'entry_price': latest_price,
                            'step': 1, 
                            'expiry_time': expiry_time
                        }
                        analyzer.last_signal[pair] = direction
                        entry_str = entry_time.strftime("%H:%M")
                        
                        await send_signal_with_chart(pair, direction, latest_price, strength, entry_str, 1)
                        await asyncio.sleep(8) # 8-second delay between multiple pairs
                except Exception as e:
                    logger.error(f"Error scanning {pair}: {e}")

            # DYNAMIC TIMER LOGIC
            if signals_found > 0:
                next_scan_allowed = now + timedelta(minutes=5)
                logger.info("Signal(s) sent. Next scan in 5 minutes.")
            else:
                next_scan_allowed = now + timedelta(minutes=1)
                logger.info("No setup. Next scan in 1 minute.")
                await send_admin_feedback(f"⚪ No setup at {now.strftime('%H:%M')}")

        # --- PHASE 2: EVALUATE OUTCOMES (At :01 seconds) ---
        elif seconds == 1:
            for pair in list(active_trades.keys()):
                try:
                    trade = active_trades[pair]
                    
                    if now >= trade['expiry_time'] and now < trade['expiry_time'] + timedelta(seconds=5):
                        bars = otc.candles("quotex", pair, tf=60, limit=1)
                        if not bars: 
                            continue
                        
                        close_price = bars[-1].close
                        direction = trade['direction']
                        entry_price = trade['entry_price']
                        step = trade['step']

                        is_win = (direction == 'CALL' and close_price > entry_price) or \
                                 (direction == 'PUT' and close_price < entry_price)

                        if is_win:
                            if step == 1:
                                text = f"✅ *{pair.upper().replace('_OTC', '')}*\nResult: *WIN*"
                            else:
                                text = f"✅ *{pair.upper().replace('_OTC', '')}*\nResult: *WIN BY MTG*"
                            await send_result_message(pair, text)
                            del active_trades[pair]
                        else:
                            if step < MTG_MAX_STEPS:
                                logger.info(f"Loss on {pair} Step {step}. Moving to Step {step+1}.")
                                await send_admin_feedback(f"❌ LOSS: {pair.upper().replace('_OTC', '')}. Moving to MTG Step {step+1}.")
                                trade['step'] += 1
                                trade['entry_price'] = close_price
                                trade['expiry_time'] = now.replace(second=0, microsecond=0) + timedelta(minutes=TRADE_DURATION_MINUTES)
                            else:
                                text = f"❌ *{pair.upper().replace('_OTC', '')}*\nResult: *LOSS* (Max MTG Reached)"
                                await send_result_message(pair, text)
                                del active_trades[pair]
                except Exception as e:
                    logger.error(f"Error evaluating {pair}: {e}")

        await asyncio.sleep(1)

# --- Telegram Command Handlers ---
def is_admin(update) -> bool:
    return str(update.effective_user.id) == str(ADMIN_ID)

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text("✅ *Bot is now ACTIVE.* Adaptive scanning enabled.")

async def cmd_pause(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = False
    await update.message.reply_text("⏸️ *Bot is PAUSED.* No new signals will be sent. Active MTG cycles will still be tracked.")

async def cmd_resume(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text("▶️ *Bot is RESUMED.* Adaptive scanning enabled.")

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
        await send_signal_with_chart("BTCUSD_otc", "PUT", 82000.0, 0.88, "17:59", 1)
        await update.message.reply_text("✅ Test card sent to the channel successfully.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send test card: {e}")

# --- Main Runner ---
async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("feed", cmd_feed))
    application.add_handler(CommandHandler("testcard", cmd_testcard))

    asyncio.create_task(run_sniper_loop())

    logger.info("Starting Telegram command listener...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
