import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import (
    BOT_TOKEN, CHANNEL_ID, OTCHARTS_API_KEY, ADMIN_ID, PAIRS,
    CANDLE_TIMEFRAME, TRADE_DURATION_MINUTES,
    SIGNAL_COOLDOWN_MINUTES, COOLDOWN_AFTER_RESULT_MINUTES,
    CANDLES_FOR_WARMUP, MIN_SCORE
)
from analyzer import S3Analyzer, TOTAL_VOTES
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
analyzer = S3Analyzer()

LOCAL_TZ = ZoneInfo("Africa/Nairobi")
IS_BOT_ACTIVE = True

pending_signals = {}
active_trades = {}
next_scan_allowed = datetime.now(LOCAL_TZ)

request_count = 0
request_day = datetime.now(LOCAL_TZ).day


def count_request(n=1):
    global request_count, request_day
    today = datetime.now(LOCAL_TZ).day
    if today != request_day:
        request_count = 0
        request_day = today
    request_count += n


def sanitize(s) -> str:
    if s is None:
        return "—"
    return str(s).replace("`", "'").replace("*", "").replace("_", "-")


async def send_admin_feedback(text):
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text)
    except TelegramError as e:
        logger.error(f"Admin feedback error: {e}")


async def send_signal_with_chart(pair, direction, strength,
                                  entry_time_str, vote_count):
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


async def run_sniper_loop():
    global next_scan_allowed
    logger.info(f"Starting DunyaBazaar 3-PAIR engine — {', '.join(PAIRS)}")

    # Warmup all pairs
    for pair in PAIRS:
        try:
            bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME,
                               limit=CANDLES_FOR_WARMUP)
            count_request()
            for bar in bars:
                analyzer.analyze(pair, bar.close, bar.high, bar.low, bar.open)
            logger.info(f"Warmed up {pair} with {len(bars)} candles")
        except Exception as e:
            logger.error(f"Warmup failed for {pair}: {e}")
        await asyncio.sleep(0.3)

    logger.info("Warmup complete. Entering main loop.")

    while True:
        if not IS_BOT_ACTIVE:
            await asyncio.sleep(2)
            continue

        now = datetime.now(LOCAL_TZ)
        seconds = now.second

        # :03 — feed just-closed candle for all pairs
        if seconds == 3:
            for pair in PAIRS:
                try:
                    bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=1)
                    count_request()
                    if bars:
                        c = bars[-1]
                        analyzer.analyze(pair, c.close, c.high, c.low, c.open)
                except Exception as e:
                    logger.error(f"Candle feed error {pair}: {e}")

        # :01 — capture entries + evaluate expired trades
        elif seconds == 1:
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
                    logger.error(f"Entry capture error {pair}: {e}")

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
                    logger.error(f"Eval error {pair}: {e}")

        # :52 — scan all pairs, pick strongest signal
        elif seconds == 52 and now >= next_scan_allowed:
            triggered = []
            for pair in PAIRS:
                if pair in pending_signals or pair in active_trades:
                    continue
                expl = analyzer.latest_explanation(pair)
                if expl is None:
                    continue
                score = expl["score"]
                if abs(score) >= MIN_SCORE:
                    direction = "CALL" if score > 0 else "PUT"
                    confidence_pct = min(abs(score) / TOTAL_VOTES, 1.0) * 100
                    triggered.append({
                        "pair": pair,
                        "direction": direction,
                        "confidence": confidence_pct,
                        "votes": abs(score),
                    })

            if triggered:
                triggered.sort(key=lambda x: x["votes"], reverse=True)
                best = triggered[0]

                entry_time = (now + timedelta(seconds=8)).replace(second=0, microsecond=0)
                expiry_time = entry_time + timedelta(minutes=TRADE_DURATION_MINUTES)
                entry_str = entry_time.strftime("%H:%M")

                sent_ok = await send_signal_with_chart(
                    best["pair"], best["direction"], best["confidence"],
                    entry_str, best["votes"]
                )

                if sent_ok:
                    pending_signals[best["pair"]] = {
                        'direction': best["direction"],
                        'expiry_time': expiry_time,
                    }
                    next_scan_allowed = now + timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
                    logger.info(
                        f"Signal sent ({best['pair']}, {best['votes']}/6). "
                        f"Next scan at {next_scan_allowed.strftime('%H:%M:%S')}"
                    )
                else:
                    logger.warning("Signal send failed. Skipping trade registration.")
            else:
                await send_admin_feedback(f"⚪ No setup at {now.strftime('%H:%M')}")
                logger.info("No 6/6 setup across all 3 pairs.")

        await asyncio.sleep(1)


# ---------------- Commands ----------------
def is_admin(update):
    return str(update.effective_user.id) == str(ADMIN_ID)


COMMAND_MENU = (
    "📋 *DunyaBazaar Bot — Commands*\n\n"
    "▶️ /start — Activate + show menu\n"
    "⏸️ /pause — Pause scanning\n"
    "▶️ /resume — Resume scanning\n"
    "📊 /feed — Bot status\n"
    "🧪 /testcard — Test signal\n"
    "❓ /help — Show this menu\n"
)


async def cmd_start(update, context):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text(
        "✅ *Bot ACTIVATED.*\n\n" + COMMAND_MENU, parse_mode="Markdown"
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
        status = "ACTIVE" if IS_BOT_ACTIVE else "PAUSED"
        lines = [
            "DunyaBazaar 3-Pair Status",
            f"Engine: {status}",
            f"Pairs: {', '.join(PAIRS)}",
            f"MIN_SCORE: {MIN_SCORE} (only 6/6 fires)",
            f"Requests today: {request_count} / 7000",
            f"Next scan: {next_scan_allowed.strftime('%H:%M:%S')}",
            "",
        ]
        if pending_signals:
            lines.append("Pending entry:")
            for p in pending_signals:
                lines.append(f"  - {p}")
        if active_trades:
            lines.append("Active trades:")
            for p, t in active_trades.items():
                lines.append(
                    f"  - {p} | {t['direction']} | "
                    f"Entry {t['entry_price']:.5f} | "
                    f"Exp {t['expiry_time'].strftime('%H:%M')}"
                )
        if not pending_signals and not active_trades:
            lines.append("No active trades.")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.error(f"cmd_feed error: {e}")
        try:
            await update.message.reply_text(f"Status unavailable: {e}")
        except Exception:
            pass


async def cmd_testcard(update, context):
    if not is_admin(update): return
    try:
        await send_signal_with_chart("USDINR_otc", "PUT", 100, "17:59", 6)
        await update.message.reply_text("✅ Test card sent.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


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