import asyncio
import logging
import time
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

analyzer = S3Analyzer(pairs=[ACTIVE_PAIR])

LOCAL_TZ = ZoneInfo("Africa/Nairobi")
IS_BOT_ACTIVE = True

pending_signals = {}
active_trades = {}
next_scan_allowed = datetime.now(LOCAL_TZ)

request_count = 0
request_day = datetime.now(LOCAL_TZ).day

live_price = {ACTIVE_PAIR: None}
live_time = {ACTIVE_PAIR: None}
stream_status = {
    "connected": False,
    "last_tick": None,
    "tick_count": 0,
    "error": None,
    "started_at": None,
}

TOTAL_VOTES = 6  # number of vote columns in the analyzer


def count_request(n=1):
    global request_count, request_day
    today = datetime.now(LOCAL_TZ).day
    if today != request_day:
        request_count = 0
        request_day = today
    request_count += n


def sanitize(s: str) -> str:
    """Strip characters that break Telegram Markdown."""
    if s is None:
        return "—"
    return str(s).replace("`", "'").replace("*", "").replace("_", "-")


async def send_admin_feedback(text):
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text)
    except TelegramError as e:
        logger.error(f"Admin feedback error: {e}")


# ------------------------------------------------------------------
# Stream worker
# ------------------------------------------------------------------
async def stream_worker():
    loop = asyncio.get_running_loop()

    def _stream():
        while True:
            try:
                logger.info(f"Opening stream for {ACTIVE_PAIR}...")
                stream_status["connected"] = False
                stream_status["error"] = None
                stream_status["started_at"] = datetime.now(LOCAL_TZ)

                for tick in otc.stream("quotex", [ACTIVE_PAIR]):
                    live_price[ACTIVE_PAIR] = tick.price
                    live_time[ACTIVE_PAIR] = tick.time
                    stream_status["connected"] = True
                    stream_status["last_tick"] = tick.time
                    stream_status["tick_count"] += 1

                logger.warning("Stream closed cleanly. Reconnecting...")
            except Exception as e:
                stream_status["connected"] = False
                stream_status["error"] = f"{type(e).__name__}: {e}"
                logger.error(f"Stream error: {type(e).__name__}: {e}")
                logger.info("Retrying stream in 5 seconds...")

            time.sleep(5)

    await loop.run_in_executor(None, _stream)


# ------------------------------------------------------------------
# Signal dispatch — returns True on success
# ------------------------------------------------------------------
async def send_signal_with_chart(pair, direction, strength,
                                  entry_time_str, live, vote_count):
    try:
        bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=50)
        count_request()
        if not bars:
            logger.warning(f"No candles for chart on {pair}")
            return False
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
            f"🔒 Confluence : `{vote_count}/{TOTAL_VOTES} votes`\n\n"
            f"----------\n\n"
            f"💬 CONTACT : @DBSdesk"
        )

        await bot.send_photo(
            chat_id=CHANNEL_ID, photo=chart_buf,
            caption=text, parse_mode="Markdown"
        )
        await send_admin_feedback(
            f"🟢 Sent {direction} {pair_display} @ {entry_time_str} | "
            f"Conf: {confidence}% | Votes: {vote_count}/{TOTAL_VOTES}"
        )
        return True
    except Exception as e:
        logger.error(f"Signal send error for {pair}: {e}")
        return False


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

    try:
        bars = otc.candles("quotex", ACTIVE_PAIR, tf=CANDLE_TIMEFRAME,
                           limit=CANDLES_FOR_WARMUP)
        count_request()
        for bar in bars:
            analyzer.analyze(ACTIVE_PAIR, bar.close, bar.high, bar.low, bar.open)
        logger.info(f"Warmed up {ACTIVE_PAIR} with {len(bars)} candles")
    except Exception as e:
        logger.error(f"Warmup failed: {e}")

    asyncio.create_task(stream_worker())
    logger.info("Stream worker launched. Entering main loop.")

    while True:
        if not IS_BOT_ACTIVE:
            await asyncio.sleep(2)
            continue

        now = datetime.now(LOCAL_TZ)
        seconds = now.second

        # :01 — feed closed candle + handle trades
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

        # :52 — scan for signal
        elif seconds == 52 and now >= next_scan_allowed:
            expl = analyzer.latest_explanation(ACTIVE_PAIR)

            if expl is None:
                await send_admin_feedback(f"⚪ No setup at {now.strftime('%H:%M')}")
                logger.info(f"No setup. Stream ticks: {stream_status['tick_count']}")
            else:
                score = expl["score"]
                if abs(score) >= MIN_SCORE:
                    direction = "CALL" if score > 0 else "PUT"
                    confidence_pct = min(abs(score) / TOTAL_VOTES, 1.0) * 100
                    vote_count = abs(score)

                    entry_time = (now + timedelta(seconds=8)).replace(second=0, microsecond=0)
                    expiry_time = entry_time + timedelta(minutes=TRADE_DURATION_MINUTES)
                    entry_str = entry_time.strftime("%H:%M")

                    sent_ok = await send_signal_with_chart(
                        ACTIVE_PAIR, direction, confidence_pct, entry_str,
                        live_price[ACTIVE_PAIR], vote_count
                    )

                    if sent_ok:
                        pending_signals[ACTIVE_PAIR] = {
                            'direction': direction,
                            'expiry_time': expiry_time,
                        }
                        next_scan_allowed = now + timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
                        logger.info(
                            f"Signal sent (score {score:+d}, votes {vote_count}/6). "
                            f"Next scan at {next_scan_allowed.strftime('%H:%M:%S')}"
                        )
                    else:
                        logger.warning("Signal send failed. Skipping trade registration.")
                else:
                    await send_admin_feedback(f"⚪ No setup at {now.strftime('%H:%M')}")
                    logger.info(f"No setup (score {score:+d}).")

        await asyncio.sleep(1)


# ---------------- Commands ----------------
def is_admin(update):
    return str(update.effective_user.id) == str(ADMIN_ID)


COMMAND_MENU = (
    "📋 *DunyaBazaar Bot — Commands*\n\n"
    "▶️ /start — Activate bot + show this menu\n"
    "⏸️ /pause — Pause signal scanning\n"
    "▶️ /resume — Resume signal scanning\n"
    "📊 /feed — Current status, stream, requests, trades\n"
    "🧪 /testcard — Send a test signal to the channel\n"
    "❓ /help — Show this menu again\n"
)


async def cmd_start(update, context):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text(
        "✅ *Bot ACTIVATED.*\n\n" + COMMAND_MENU,
        parse_mode="Markdown"
    )


async def cmd_help(update, context):
    if not is_admin(update): return
    await update.message.reply_text(COMMAND_MENU, parse_mode="Markdown")


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
    try:
        status = "🟢 ACTIVE" if IS_BOT_ACTIVE else "🔴 PAUSED"
        live = live_price.get(ACTIVE_PAIR)
        live_str = f"{live:.5f}" if live is not None else "no tick yet"
        last_tick = stream_status["last_tick"]
        last_tick_str = last_tick.strftime("%H:%M:%S") if last_tick else "—"
        err = sanitize(stream_status.get("error"))
        stream_icon = "🟢 CONNECTED" if stream_status["connected"] else "🔴 OFFLINE"

        text = (
            f"📊 *DunyaBazaar STREAMING Status*\n"
            f"Engine: {status}\n"
            f"Pair: `{ACTIVE_PAIR}`\n"
            f"Live price: `{live_str}`\n"
            f"Stream: {stream_icon}\n"
            f"Stream err: `{err}`\n"
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
    except Exception as e:
        logger.error(f"cmd_feed error: {e}")
        # Fallback: send plain text so user still gets a reply
        try:
            await update.message.reply_text(
                f"⚠️ Status unavailable: {type(e).__name__}: {e}"
            )
        except Exception:
            pass


async def cmd_testcard(update, context):
    if not is_admin(update): return
    try:
        await send_signal_with_chart(
            ACTIVE_PAIR, "PUT", 50, "17:59",
            live_price.get(ACTIVE_PAIR), 3
        )
        await update.message.reply_text("✅ Test card sent.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


# ---------------- Main ----------------
async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("feed", cmd_feed))
    application.add_handler(CommandHandler("testcard", cmd_testcard))

    asyncio.create_task(run_sniper_loop())

    logger.info("Starting Telegram command listener...")
    await application.initialize()
    await application.start()

    # Register the / menu
    try:
        await application.bot.set_my_commands([
            ("start", "Activate bot & show commands"),
            ("pause", "Pause signal scanning"),
            ("resume", "Resume signal scanning"),
            ("feed", "Show bot status"),
            ("testcard", "Send a test signal"),
            ("help", "Show available commands"),
        ])
        logger.info("Command menu registered.")
    except Exception as e:
        logger.error(f"set_my_commands failed: {e}")

    await application.updater.start_polling()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())