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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import TelegramError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

otc = Client(api_key=OTCHARTS_API_KEY)
bot = Bot(token=BOT_TOKEN)

analyzer = None
VALID_PAIRS = []

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


async def send_admin_feedback(text):
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text)
    except TelegramError as e:
        logger.error(f"Admin feedback error: {e}")


# ---------------- Inline Keyboard Menu ----------------
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 Live Status", callback_data="feed"),
         InlineKeyboardButton("▶️ Resume", callback_data="resume")],
        [InlineKeyboardButton("⏸️ Pause", callback_data="pause"),
         InlineKeyboardButton("🧪 Test Card", callback_data="testcard")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_signal_with_chart(pair, direction, strength, entry_time_str, vote_count, support, resistance):
    try:
        bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=50)
        count_request()
        if not bars:
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
            f"🔒 Confluence : `{vote_count}/{TOTAL_VOTES} votes`\n"
            f"🛡️ Support    : `{support:.5f}`\n"
            f"🚧 Resistance : `{resistance:.5f}`\n\n"
            f"⚠️ *MTG : STEP 1 IF LOSS*\n\n"
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


async def verify_pairs():
    global VALID_PAIRS
    valid = []
    logger.info(f"Verifying {len(PAIRS)} pairs...")
    for pair in PAIRS:
        try:
            bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=2)
            count_request()
            if bars and len(bars) > 0:
                valid.append(pair)
                logger.info(f"✅ {pair}")
            else:
                logger.warning(f"❌ {pair} — no candles")
        except Exception as e:
            logger.warning(f"❌ {pair} — {e}")
        await asyncio.sleep(0.3)
    VALID_PAIRS = valid
    logger.info(f"Verified {len(VALID_PAIRS)}/{len(PAIRS)} pairs.")
    return valid


async def run_sniper_loop():
    global analyzer, next_scan_allowed

    logger.info("Starting DunyaBazaar engine...")
    valid = await verify_pairs()
    if not valid:
        await send_admin_feedback("🚨 ERROR: No valid pairs found.")
        return

    analyzer = S3Analyzer(pairs=valid)

    for pair in VALID_PAIRS:
        try:
            bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=CANDLES_FOR_WARMUP)
            count_request()
            for bar in bars:
                analyzer.analyze(pair, bar.close, bar.high, bar.low, bar.open)
        except Exception as e:
            logger.error(f"Warmup failed for {pair}: {e}")
        await asyncio.sleep(0.3)

    logger.info("Warmup complete.")
    await send_admin_feedback(f"✅ Bot started. Valid pairs: {', '.join(VALID_PAIRS)}")

    while True:
        if not IS_BOT_ACTIVE:
            await asyncio.sleep(2)
            continue

        now = datetime.now(LOCAL_TZ)
        seconds = now.second

        # :03 — feed closed candle
        if seconds == 3:
            for pair in VALID_PAIRS:
                try:
                    bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=1)
                    count_request()
                    if bars:
                        c = bars[-1]
                        analyzer.analyze(pair, c.close, c.high, c.low, c.open)
                except Exception as e:
                    logger.error(f"Candle feed error {pair}: {e}")

        # :01 — capture entries + evaluate
        elif seconds == 1:
            for pair in list(pending_signals.keys()):
                try:
                    bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=1)
                    count_request()
                    if not bars: continue
                    true_entry = bars[-1].open
                    pending_signals[pair]['entry_price'] = true_entry
                    active_trades[pair] = pending_signals.pop(pair)
                    analyzer.last_signal[pair] = active_trades[pair]['direction']
                except Exception as e:
                    logger.error(f"Entry capture error {pair}: {e}")

            for pair in list(active_trades.keys()):
                try:
                    trade = active_trades[pair]
                    if now >= trade['expiry_time'] and now < trade['expiry_time'] + timedelta(seconds=10):
                        bars = otc.candles("quotex", pair, tf=CANDLE_TIMEFRAME, limit=1)
                        count_request()
                        if not bars: continue
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

        # :52 — scan
        elif seconds == 52 and now >= next_scan_allowed:
            triggered = []
            for pair in VALID_PAIRS:
                if pair in pending_signals or pair in active_trades:
                    continue
                expl = analyzer.latest_explanation(pair)
                if expl is None: continue
                score = expl["score"]
                if abs(score) >= MIN_SCORE:
                    direction = "CALL" if score > 0 else "PUT"
                    confidence_pct = min(abs(score) / TOTAL_VOTES, 1.0) * 100
                    triggered.append({
                        "pair": pair,
                        "direction": direction,
                        "confidence": confidence_pct,
                        "votes": abs(score),
                        "support": expl.get("support", 0.0),
                        "resistance": expl.get("resistance", 0.0),
                    })

            if triggered:
                triggered.sort(key=lambda x: x["votes"], reverse=True)
                best = triggered[0]

                entry_time = (now + timedelta(seconds=8)).replace(second=0, microsecond=0)
                expiry_time = entry_time + timedelta(minutes=TRADE_DURATION_MINUTES)
                entry_str = entry_time.strftime("%H:%M")

                sent_ok = await send_signal_with_chart(
                    best["pair"], best["direction"], best["confidence"],
                    entry_str, best["votes"], best["support"], best["resistance"]
                )

                if sent_ok:
                    pending_signals[best["pair"]] = {
                        'direction': best["direction"],
                        'expiry_time': expiry_time,
                    }
                    next_scan_allowed = now + timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
                else:
                    logger.warning("Signal send failed.")
            else:
                logger.info(f"No setup across {len(VALID_PAIRS)} pairs.")

        await asyncio.sleep(1)


# ---------------- Command Handlers ----------------
def is_admin(update):
    return str(update.effective_user.id) == str(ADMIN_ID)

async def cmd_start(update, context):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text(
        "✅ *Bot ACTIVATED.*\n\nUse the menu below:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def cmd_help(update, context):
    if not is_admin(update): return
    await update.message.reply_text(
        "📋 *Commands*\n"
        "/start - Activate & Show Menu\n"
        "/pause - Pause scanning\n"
        "/resume - Resume scanning\n"
        "/feed - Bot status\n"
        "/testcard - Test signal",
        parse_mode="Markdown"
    )

async def cmd_pause(update, context):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = False
    await update.message.reply_text("⏸️ *Bot PAUSED.*", parse_mode="Markdown")

async def cmd_resume(update, context):
    if not is_admin(update): return
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    await update.message.reply_text("▶️ *Bot RESUMED.*", parse_mode="Markdown")

async def cmd_feed(update, context):
    if not is_admin(update): return
    try:
        status = "ACTIVE" if IS_BOT_ACTIVE else "PAUSED"
        lines = [
            "DunyaBazaar Status",
            f"Engine: {status}",
            f"Valid pairs: {len(VALID_PAIRS)}",
            f"MIN_SCORE: {MIN_SCORE} of {TOTAL_VOTES}",
            f"Requests today: {request_count} / 7000",
            f"Next scan: {next_scan_allowed.strftime('%H:%M:%S')}",
        ]
        await update.message.reply_text("\n".join(lines), reply_markup=main_menu_keyboard())
    except Exception as e:
        await update.message.reply_text(f"Status unavailable: {e}")

async def cmd_testcard(update, context):
    if not is_admin(update): return
    pair = VALID_PAIRS[0] if VALID_PAIRS else "USDINR_otc"
    try:
        await send_signal_with_chart(pair, "PUT", 67, "17:59", 4, 0.0, 0.0)
        await update.message.reply_text("✅ Test card sent.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")

# ---------------- Callback Handler ----------------
async def button_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "feed":
        await cmd_feed(update, context)
    elif data == "pause":
        await cmd_pause(update, context)
    elif data == "resume":
        await cmd_resume(update, context)
    elif data == "testcard":
        await cmd_testcard(update, context)
    elif data == "help":
        await cmd_help(update, context)

# ---------------- Main ----------------
async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("feed", cmd_feed))
    application.add_handler(CommandHandler("testcard", cmd_testcard))
    application.add_handler(CallbackQueryHandler(button_callback))

    asyncio.create_task(run_sniper_loop())

    logger.info("Starting Telegram command listener...")
    await application.initialize()
    await application.start()

    try:
        await application.bot.set_my_commands([
            ("start", "Activate bot & show menu"),
            ("pause", "Pause signal scanning"),
            ("resume", "Resume signal scanning"),
            ("feed", "Show bot status"),
            ("testcard", "Send a test signal"),
            ("help", "Show available commands"),
        ])
    except Exception as e:
        logger.error(f"set_my_commands failed: {e}")

    await application.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())