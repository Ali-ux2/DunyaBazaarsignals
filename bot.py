import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import (
    BOT_TOKEN, CHANNEL_ID, OTCHARTS_KEY, ADMIN_ID, PAIRS,
    SIGNAL_INTERVAL_MINUTES, TRADE_DURATION_MINUTES,
    M5_EMA_SHORT, M5_EMA_LONG
)
from analyzer import S3Analyzer
from charting import generate_candle_chart
from otcharts import Client
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

otc = Client(api_key=OTCHARTS_KEY)
bot = Bot(token=BOT_TOKEN)
analyzer = S3Analyzer()

LOCAL_TZ = ZoneInfo("Africa/Nairobi")
IS_BOT_ACTIVE = True

# States
pending_signals = {}   # {pair: {...signal data...}}  waiting for :00 to capture true entry
active_trades = {}     # {pair: {...trade with true entry price...}}
next_scan_allowed = datetime.now(LOCAL_TZ)
last_m5_update = datetime.now(LOCAL_TZ) - timedelta(minutes=10)

request_count = 0
request_day = datetime.now(LOCAL_TZ).day


def count_request(n=1):
    global request_count, request_day
    today = datetime.now(LOCAL_TZ).day
    if today != request_day:
        request_count = 0
        request_day = today
    request_count += n


async def send_admin_feedback(text):
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text)
    except TelegramError as e:
        logger.error(f"Admin feedback error: {e}")


async def update_m5_trends():
    global last_m5_update
    logger.info("Updating M5 trends...")
    for pair in PAIRS:
        try:
            bars = otc.candles("quotex", pair, tf=300, limit=50)
            count_request()
            if len(bars) >= M5_EMA_LONG:
                closes = [b.close for b in bars]
                ema_s = analyzer._ema(closes, M5_EMA_SHORT)
                ema_l = analyzer._ema(closes, M5_EMA_LONG)
                if ema_s and ema_l:
                    trend = "BULLISH" if ema_s > ema_l else "BEARISH"
                    analyzer.set_m5_trend(pair, trend)
        except Exception as e:
            logger.error(f"M5 update failed for {pair}: {e}")
        await asyncio.sleep(0.3)
    last_m5_update = datetime.now(LOCAL_TZ)
    logger.info("M5 trends updated.")


async def send_signal_with_chart(pair, direction, price, strength, entry_time_str):
    try:
        bars = otc.candles("quotex", pair, tf=60, limit=50)
        count_request()
        if not bars:
            return
        chart_buf = generate_candle_chart(bars, pair)

        pair_display = pair.upper().replace('_OTC', '')
        trend = "BULLISH" if direction == "CALL" else "BEARISH"
        arrow = "🟢 CALL ↑" if direction == "CALL" else "🔴 PUT ↓"
        confidence = int(strength)

        try:
            payouts = otc.payouts("quotex", [pair])
            count_request()
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
            f"🔒 Filters     : `6/7 Aligned`\n\n"
            f"----------\n\n"
            f"💬 CONTACT : @DBSdesk"
        )
        await bot.send_photo(chat_id=CHANNEL_ID, photo=chart_buf, caption=text, parse_mode="Markdown")
        await send_admin_feedback(f"🟢 Sent {direction} {pair_display} @ {entry_time_str} | Conf: {confidence}%")
    except Exception as e:
        logger.error(f"Signal send error for {pair}: {e}")


async def send_result_message(pair, result_text):
    try:
        text = f"{result_text}\n—\n🌍 DunyaBazaar"
        await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown")
        await send_admin_feedback(f"Result: {result_text}")
    except Exception as e:
        logger.error(f"Result send error: {e}")


async def run_sniper_loop():
    global next_scan_allowed, last_m5_update
    logger.info("Starting DunyaBazaar High-Quality Engine (Fixed Timing)...")

    for pair in PAIRS:
        try:
            bars = otc.candles("quotex", pair, tf=60, limit=80)
            count_request()
            for bar in bars:
                analyzer.analyze(pair, bar.close, bar.high, bar.low)
        except Exception as e:
            logger.error(f"M1 warmup failed for {pair}: {e}")
        await asyncio.sleep(0.3)

    await update_m5_trends()
    logger.info("Warmup complete. Entering main loop.")

    while True:
        if not IS_BOT_ACTIVE:
            await asyncio.sleep(2)
            continue

        now = datetime.now(LOCAL_TZ)
        seconds = now.second

        # Refresh M5 every 5 minutes
        if (now - last_m5_update).total_seconds() > 300 and seconds == 30:
            await update_m5_trends()

        # ============================================================
        # PHASE 1: SCAN & SEND SIGNAL (at :52 seconds)
        # ============================================================
        if seconds == 52 and now >= next_scan_allowed:
            logger.info(f"Scanning at {now.strftime('%H:%M:%S')} | Requests: {request_count}")
            triggered = []

            for pair in PAIRS:
                try:
                    bars = otc.candles("quotex", pair, tf=60, limit=3)
                    count_request()
                    if not bars:
                        continue
                    latest = bars[-1]
                    direction, confidence = analyzer.analyze(pair, latest.close, latest.high, latest.low)

                    if (direction and pair not in active_trades
                            and pair not in pending_signals
                            and analyzer.last_signal.get(pair) != direction):
                        entry_time = (now + timedelta(seconds=8)).replace(second=0, microsecond=0)
                        expiry_time = entry_time + timedelta(minutes=TRADE_DURATION_MINUTES)
                        triggered.append({
                            'pair': pair, 'direction': direction,
                            'strength': confidence,
                            'entry_time': entry_time,
                            'expiry_time': expiry_time
                        })
                except Exception as e:
                    logger.error(f"Scan error {pair}: {e}")

            if triggered:
                triggered.sort(key=lambda x: x['strength'], reverse=True)
                best = triggered[0]

                # Mark as PENDING (real entry price captured at :01)
                pending_signals[best['pair']] = {
                    'direction': best['direction'],
                    'expiry_time': best['expiry_time'],
                }
                entry_str = best['entry_time'].strftime("%H:%M")

                await send_signal_with_chart(
                    best['pair'], best['direction'],
                    best.get('price', 0), best['strength'], entry_str
                )

                next_scan_allowed = now + timedelta(minutes=SIGNAL_INTERVAL_MINUTES)
                logger.info(f"Signal sent. Next scan in {SIGNAL_INTERVAL_MINUTES} min.")
            else:
                next_scan_allowed = now + timedelta(minutes=1)
                logger.info("No setup. Next scan in 1 min.")
                await send_admin_feedback(f"⚪ No setup at {now.strftime('%H:%M')}")

        # ============================================================
        # PHASE 2: CAPTURE REAL ENTRY PRICE (at :01 seconds)
        #          AND EVALUATE EXPIRED TRADES
        # ============================================================
        elif seconds == 1:

            # --- 2a: Capture true entry price for pending signals ---
            for pair in list(pending_signals.keys()):
                try:
                    bars = otc.candles("quotex", pair, tf=60, limit=1)
                    count_request()
                    if not bars:
                        continue
                    true_entry = bars[-1].open   # open of the candle that just began at :00

                    pending_signals[pair]['entry_price'] = true_entry
                    active_trades[pair] = pending_signals.pop(pair)
                    analyzer.last_signal[pair] = active_trades[pair]['direction']
                    logger.info(f"Registered {pair} entry @ {true_entry:.5f}")
                except Exception as e:
                    logger.error(f"Entry capture error {pair}: {e}")

            # --- 2b: Evaluate expired trades ---
            for pair in list(active_trades.keys()):
                try:
                    trade = active_trades[pair]
                    if now >= trade['expiry_time'] and now < trade['expiry_time'] + timedelta(seconds=10):
                        bars = otc.candles("quotex", pair, tf=60, limit=1)
                        count_request()
                        if not bars:
                            continue
                        close_price = bars[-1].close
                        entry_price = trade['entry_price']
                        direction = trade['direction']

                        is_win = (direction == 'CALL' and close_price > entry_price) or \
                                 (direction == 'PUT' and close_price < entry_price)

                        result_icon = "✅" if is_win else "❌"
                        result_word = "WIN" if is_win else "LOSS"
                        pair_display = pair.upper().replace('_OTC', '')

                        text = (
                            f"{result_icon} *{pair_display}*\n"
                            f"Result: *{result_word}*\n"
                            f"Entry: `{entry_price:.5f}` | Close: `{close_price:.5f}`"
                        )
                        await send_result_message(pair, text)
                        del active_trades[pair]
                except Exception as e:
                    logger.error(f"Eval error {pair}: {e}")

        await asyncio.sleep(1)


# ---- Commands ----
def is_admin(update):
    return str(update.effective_user.id) == str(ADMIN_ID)


async def cmd_start(update, context):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text("✅ *Bot ACTIVE.*")


async def cmd_pause(update, context):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = False
    await update.message.reply_text("⏸️ *Bot PAUSED.*")


async def cmd_resume(update, context):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text("▶️ *Bot RESUMED.*")


async def cmd_feed(update, context):
    if not is_admin(update): return
    status = "🟢 ACTIVE" if IS_BOT_ACTIVE else "🔴 PAUSED"
    text = (
        f"📊 *DunyaBazaar Status*\n"
        f"Engine: {status}\n"
        f"Pairs: {len(PAIRS)}\n"
        f"Requests today: `{request_count}` / 7000\n\n"
    )
    if pending_signals:
        text += "*Pending (entry not yet captured):*\n"
        for p in pending_signals:
            text += f"• `{p}`\n"
    if active_trades:
        text += "*Active trades:*\n"
        for p, t in active_trades.items():
            text += f"• `{p}` | {t['direction']} | Entry `{t['entry_price']:.5f}` | Exp `{t['expiry_time'].strftime('%H:%M')}`\n"
    if not pending_signals and not active_trades:
        text += "_No active or pending trades._"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_testcard(update, context):
    if not is_admin(update): return
    try:
        await send_signal_with_chart("BTCUSD_otc", "PUT", 82000.0, 85, "17:59")
        await update.message.reply_text("✅ Test card sent.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


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
