import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import (
    BOT_TOKEN, CHANNEL_ID, OTCHARTS_API_KEY, ADMIN_ID, ACTIVE_PAIR,
    CANDLE_TIMEFRAME, TRADE_DURATION_MINUTES,
    SIGNAL_COOLDOWN_MINUTES, COOLDOWN_AFTER_RESULT_MINUTES,
    CANDLES_FOR_WARMUP, MIN_SCORE
)
from analyzer import S3Analyzer
from charting import generate_candle_chart
from otcharts import Client
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

otc = Client(api_key=OTCHARTS_API_KEY)
bot = Bot(token=BOT_TOKEN)

# Streaming is strictly single-symbol on Quotex → analyzer tracks just one pair
analyzer = S3Analyzer(pairs=[ACTIVE_PAIR])

LOCAL_TZ = ZoneInfo("Africa/Nairobi")
IS_BOT_ACTIVE = True

pending_signals = {}
active_trades = {}
next_scan_allowed = datetime.now(LOCAL_TZ)

request_count = 0
request_day = datetime.now(LOCAL_TZ).day

# Live tick from stream — updated in the background worker
live_price = {ACTIVE_PAIR: None}
live_time = {ACTIVE_PAIR: None}
stream_status = {"connected": False, "last_tick": None, "tick_count": 0}


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


# ------------------------------------------------------------------
# Background stream worker — writes into live_price / live_time
# ------------------------------------------------------------------
async def stream_worker():
    """Runs the blocking otc.stream() in a thread, pushing ticks into state."""
    loop = asyncio.get_event_loop()

    def _stream():
        try:
            logger.info(f"Opening stream for {ACTIVE_PAIR}...")
            for tick in otc.stream("quotex", [ACTIVE_PAIR]):
                live_price[ACTIVE_PAIR] = tick.price
                live_time[ACTIVE_PAIR] = tick.time
                stream_status["connected"] = True
                stream_status["last_tick"] = tick.time
                stream_status["tick_count"] += 1
        except Exception as e:
            logger.error(f"Stream error: {e}")
            stream_status["connected"] = False

    await loop.run_in_executor(None, _stream)


# ------------------------------------------------------------------
# Signal dispatch
# ------------------------------------------------------------------
async def send_signal_with_chart(pair, direction, strength, entry_time_str, live):
    try:
        bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=50)
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
        except Exception:
            payout = 89

        live_str = f"{live:.5f}" if live is not None else "—"

        text = (
            f"🚀 *Signal ready*\n"
            f"*{pair_display}*\n\n"
            f"------ {{ DUNYABAZAAR }} ------\n\n"
            f"📊 Asset      : `{pair_display}`\n"
            f"📈 Trend      : `{trend}`\n"
            f"Direction    : {arrow}\n"
            f"⏳ Timeframe  : `M1`\n"
            f"🕒 Entry Time : `{entry_time_str} (+3:00 UTC)`\n"
            f"💰 Payout     : `{payout}%`\n"
            f"💹 Live       : `{live_str}`\n\n"
            f"----------\n\n"
            f"✨ Confidence : `{confidence}%`\n"
            f"🔒 Confluence : `{MIN_SCORE}/7 votes`\n\n"
            f"----------\n\n"
            f"💬 CONTACT : @DBSdesk"
        )

        await bot.send_photo(
            chat_id=CHANNEL_ID, photo=chart_buf,
            caption=text, parse_mode="Markdown"
        )
        await send_admin_feedback(
            f"🟢 Sent {direction} {pair_display} @ {entry_time_str} | "
            f"Conf: {confidence}% | Live: {live_str}"
        )
    except Exception as e:
        logger.error(f"Signal send error: {e}")


async def send_result_message(result_text):
    global next_scan_allowed
    try:
        text = f"{result_text}\n—\n🌍 DunyaBazaar"
        await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown")
        await send_admin_feedback(f"Result: {result_text}")

        result_cooldown = datetime.now(LOCAL_TZ) + timedelta(
            minutes=COOLDOWN_AFTER_RESULT_MINUTES
        )
        if result_cooldown > next_scan_allowed:
            next_scan_allowed = result_cooldown
    except Exception as e:
        logger.error(f"Result send error: {e}")


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------
async def run_sniper_loop():
    global next_scan_allowed
    logger.info(f"Starting DunyaBazaar STREAMING engine — pair: {ACTIVE_PAIR}")

    # 1. Warmup — fetch 80 historical M1 candles for the active pair
    try:
        bars = otc.candles("quotex", ACTIVE_PAIR, tf=CANDLE_TIMEFRAME,
                           limit=CANDLES_FOR_WARMUP)
        count_request()
        for bar in bars:
            analyzer.analyze(ACTIVE_PAIR, bar.close, bar.high, bar.low, bar.open)
        logger.info(f"Warmed up {ACTIVE_PAIR} with {len(bars)} candles")
    except Exception as e:
        logger.error(f"Warmup failed: {e}")

    # 2. Start the streaming worker in the background
    asyncio.create_task(stream_worker())
    logger.info("Stream worker launched. Entering main loop.")

    while True:
        if not IS_BOT_ACTIVE:
            await asyncio.sleep(2)
            continue

        now = datetime.now(LOCAL_TZ)
        seconds = now.second

        # ----------------------------------------------------------
        # FEED CLOSED CANDLE TO ANALYZER (at :01 of every minute)
        # ----------------------------------------------------------
        if seconds == 1:
            try:
                bars = otc.candles("quotex", ACTIVE_PAIR, tf=CANDLE_TIMEFRAME, limit=1)
                count_request()
                if bars:
                    c = bars[-1]
                    analyzer.analyze(ACTIVE_PAIR, c.close, c.high, c.low, c.open)
                    logger.info(
                        f"Candle closed @ {now.strftime('%H:%M')} | "
                        f"O:{c.open:.5f} H:{c.high:.5f} L:{c.low:.5f} C:{c.close:.5f}"
                    )
            except Exception as e:
                logger.error(f"Candle feed error: {e}")

            # Capture true entry price for any pending signal
            for pair in list(pending_signals.keys()):
                try:
                    bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=1)
                    count_request()
                    if not bars:
                        continue
                    true_entry = bars[-1].open
                    pending_signals[pair]['entry_price'] = true_entry
                    active_trades[pair] = pending_signals.pop(pair)
                    analyzer.last_signal[pair] = active_trades[pair]['direction']
                    logger.info(f"Registered entry {pair} @ {true_entry:.5f}")
                except Exception as e:
                    logger.error(f"Entry capture error: {e}")

            # Evaluate expired trades
            for pair in list(active_trades.keys()):
                try:
                    trade = active_trades[pair]
                    if now >= trade['expiry_time'] and now < trade['expiry_time'] + timedelta(seconds=10):
                        bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=1)
                        count_request()
                        if not bars:
                            continue
                        close_price = bars[-1].close
                        entry_price = trade['entry_price']
                        direction = trade['direction']

                        is_win = (direction == 'CALL' and close_price > entry_price) or \
                                 (direction == 'PUT'  and close_price < entry_price)

                        icon = "✅" if is_win else "❌"
                        word = "WIN" if is_win else "LOSS"
                        pair_display = pair.upper().replace('_OTC', '')

                        text = (
                            f"{icon} *{pair_display}*\n"
                            f"Result: *{word}*\n"
                            f"Entry: `{entry_price:.5f}` | Close: `{close_price:.5f}`"
                        )
                        await send_result_message(text)
                        del active_trades[pair]
                except Exception as e:
                    logger.error(f"Eval error: {e}")

        # ----------------------------------------------------------
        # SCAN FOR SIGNAL (at :52 of every minute, after cooldown)
        # ----------------------------------------------------------
        elif seconds == 52 and now >= next_scan_allowed:
            direction, confidence = analyzer.analyze(ACTIVE_PAIR, live_price[ACTIVE_PAIR] or 0)
            # Note: we don't re-feed the live price — just query the analyzer's
            # current state built from closed candles.

            # Re-run analyze on the latest closed candle state to check signal
            # The analyzer already has 100 closed candles in its history, so its
            # latest score reflects the just-closed candle.

            # Pull latest explanation to check current signal
            expl = analyzer.latest_explanation(ACTIVE_PAIR)
            if expl is None:
                await send_admin_feedback(f"⚪ No setup at {now.strftime('%H:%M')}")
                logger.info(f"No setup. Stream ticks: {stream_status['tick_count']}")
            else:
                score = expl["score"]
                if abs(score) >= MIN_SCORE:
                    direction = "CALL" if score > 0 else "PUT"
                    confidence_pct = min(abs(score) / 7, 1.0) * 100

                    entry_time = (now + timedelta(seconds=8)).replace(second=0, microsecond=0)
                    expiry_time = entry_time + timedelta(minutes=TRADE_DURATION_MINUTES)
                    pending_signals[ACTIVE_PAIR] = {
                        'direction': direction,
                        'expiry_time': expiry_time,
                    }
                    entry_str = entry_time.strftime("%H:%M")

                    await send_signal_with_chart(
                        ACTIVE_PAIR, direction, confidence_pct, entry_str,
                        live_price[ACTIVE_PAIR]
                    )
                    next_scan_allowed = now + timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
                    logger.info(
                        f"Signal sent (score {score:+d}). Next scan at "
                        f"{next_scan_allowed.strftime('%H:%M:%S')}"
                    )
                else:
                    await send_admin_feedback(f"⚪ No setup at {now.strftime('%H:%M')}")
                    logger.info(f"No setup (score {score:+d}).")

        await asyncio.sleep(1)


# ---------------- Commands ----------------
def is_admin(update):
    return str(update.effective_user.id) == str(ADMIN_ID)


async def cmd_start(update, context):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text(f"✅ *Bot ACTIVE.* Streaming {ACTIVE_PAIR}.")


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
    live = live_price.get(ACTIVE_PAIR)
    live_str = f"{live:.5f}" if live is not None else "no tick yet"
    last_tick = stream_status["last_tick"]
    last_tick_str = last_tick.strftime("%H:%M:%S") if last_tick else "—"

    text = (
        f"📊 *DunyaBazaar STREAMING Status*\n"
        f"Engine: {status}\n"
        f"Pair: `{ACTIVE_PAIR}`\n"
        f"Live price: `{live_str}`\n"
        f"Stream: {'🟢 CONNECTED' if stream_status['connected'] else '🔴 OFFLINE'}\n"
        f"Ticks received: `{stream_status['tick_count']}`\n"
        f"Last tick: `{last_tick_str}`\n"
        f"Requests today: `{request_count}` / 7000\n"
        f"Next scan: `{next_scan_allowed.strftime('%H:%M:%S')}`\n\n"
    )
    if pending_signals:
        text += "*Pending entry:*\n"
        for p in pending_signals:
            text += f"• `{p}`\n"
    if active_trades:
        text += "*Active trade:*\n"
        for p, t in active_trades.items():
            text += (f"• `{p}` | {t['direction']} | "
                     f"Entry `{t['entry_price']:.5f}` | "
                     f"Exp `{t['expiry_time'].strftime('%H:%M')}`\n")
    if not pending_signals and not active_trades:
        text += "_No active trades._"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_testcard(update, context):
    if not is_admin(update): return
    try:
        await send_signal_with_chart(ACTIVE_PAIR, "PUT", 57, "17:59", live_price.get(ACTIVE_PAIR))
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