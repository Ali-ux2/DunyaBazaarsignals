# OTC M1 Signal Bot (NO martingale)

Files: `analyzer.py` (signal engine + backtest), `config.py` (all settings), `bot.py` (Telegram worker),
`strategy_review.md` (why the indicators changed).

## Railway variables
| Variable (primary) | Fallbacks accepted | Required |
|---|---|---|
| `OTCHARTS_API_KEY` | `OTCHARTS_KEY`, `OTC_API_KEY` | yes (live mode) |
| `TELEGRAM_BOT_TOKEN` | `TELEGRAM_TOKEN`, `BOT_TOKEN` | yes |
| `TELEGRAM_CHANNEL_ID` | `CHANNEL_ID`, `CHAT_ID`, `TELEGRAM_CHAT_ID` | yes (group/channel for signals) |
| `TELEGRAM_ADMIN_ID` | `ADMIN_CHAT_ID`, `ADMIN_ID` | yes (your user/chat id) |
| `BROKER_BOOK` | | optional: `quotex` (default; symbols like `USDINR_otc`, UTC timestamps), `otc` (Pocket Option, UTC+2 auto-corrected), `iq`, `binodex` |
| `SESSIONS_UTC` | | optional, e.g. `07:00-11:00,13:00-17:00` (empty = always on) |
| `OVERLAP_MODE` | | optional: `off`, `log` (default: record only), `require` (only signal when the overlap candle confirms) |
| `SCAN_MODE` | | `closed` (default: closed candle at :02, entry next :00) or `early52` (old: forming candle at :52, entry next :00) |
| `PRECHECK_MODE` | | `confirm` (default), `log`, `off` |
| `PRECHECK_SECOND` | | default `57` |
| `STATE_DIR` | | optional; point at a Railway Volume mount so `pair_stats.json` survives redeploys |
| `DATA_SOURCE`, `REPLAY_CSV`, `TELEGRAM_ENABLED`, `DRY_RUN_MINUTES` | | testing only |

If a required one is missing the bot logs `Missing required env vars: ...` and exits (code 2).
The bot must be an admin of the channel to post. Deploy as a worker (Procfile `worker: python bot.py`).

## Admin commands (from TELEGRAM_ADMIN_ID only)
`/pause`, `/resume`, `/status`. Stop the Railway service between sessions, or use `/pause` / `SESSIONS_UTC`.

## Loop
Every minute at :02 UTC (the closed candle, not :52 on an unfinished one): fetch `/v1/candles` per pair
(300 bars at first, then 60), drop the forming candle (Quotex bars are already UTC; only Pocket Option `otc` gets a −2h shift), then skip pairs that are
closed, under `MIN_PAYOUT` (77%), have feed gaps, stale data, or rolling win rate below breakeven for the current payout.
The best signal is posted with entry at the next candle open and 1-minute expiry; the result is settled from that candle.
Cooldowns: 3 min after a signal, 2 min after a result. Admin chat receives ⚪ No setup / 🟢 Sent / Result.

## API budget (OTCharts Lite: 7,000 requests/day, resets 00:00 UTC)
12 pairs × 1 call/min ≈ 720/hour (+12/hour for payouts) → about **9.5 hours of active session per day**.
Minutes in cooldown use fewer calls. `/v1/usage` (free) is checked every 5 min: warning at 80%, auto-pause at 95%
(`/resume` re-pauses on the next check until the quota resets).

## Test locally
```
pip install -r requirements.txt
python analyzer.py                                  # self-test + random-walk backtest
DATA_SOURCE=replay REPLAY_CSV=replay_m1.csv TELEGRAM_ENABLED=0 DRY_RUN_MINUTES=1100 python bot.py
```
Backtest real data: `from analyzer import backtest_csv; backtest_csv("USDINR.csv","USDINR",0.85)`
(CSV: time,open,high,low,close, optional `pair`). Use **recorded broker OTC candles**, not real FX data.

## Quotex notes (OTCharts docs)
- Book id `quotex`, symbols `USDINR_otc` format, timeframe 60s, timestamps true UTC, max 500 bars per page.
- On weekdays Quotex closes some OTC pairs; the bot only trades pairs with `"open": true`.
- Verify your 12 pairs exist: at startup the bot logs any pair missing from `/v1/symbols?venue=quotex`.

## Overlap-candle confirmation (filter only)
CALL confirmed when the last closed candle is green and closes above the high of the previous green candle (within 5 bars);
PUT is the mirror (red, closing below the previous red candle's low). It can only remove signals, never create them.
`log` records agreement in trades.csv and the backtest's `overlap_comparison` (win rate agreed vs disagreed vs all).
Switch to `require` only if recorded OTC data shows the "agreed" group beating breakeven by a meaningful sample.

## Pre-entry confirmation (PRECHECK_MODE)
At :PRECHECK_SECOND (default :57) right before entry, the bot fetches the forming candle once (one extra request per
active signal) and judges it against the signal (thresholds in config.py, in ATR units):
- cancel if it ran > 1.5 ATR in the signal direction (edge used up) or already reached the S/R target
- cancel if the opposing body is >= 0.6 ATR, price moved > 0.5 ATR against, or it crossed back through EMA9
  (trend setups only) or through the signal candle's midpoint
- confirm if the candle is with the signal or the opposing body is < 0.3 ATR
`confirm` posts "✅ CONFIRMED" or "❌ CANCELLED, skip" to the group; cancelled trades are logged as CANCELLED and are
not counted as losses. `log` only reports the verdict to admin. Timing: closed mode signal 01:02 → precheck 01:57 → entry 02:00.
Budget: +1 request per signal. The /v1/usage figures include it automatically.

**Backtest approximation:** M1 OHLC has no :57 snapshot, so the close of the candle before entry stands in for the :57
price. In `early52` mode the signal candle's final OHLC also stands in for its :52 snapshot (8 s look-ahead, optimistic).
Results are marked `"approximate": true`. Record real ticks/snapshots to validate.

## Symbol resolution (all pairs, incl. USDBRL)
At startup the bot reads `/v1/symbols?venue=<BROKER_BOOK>` and resolves every pair. It tries explicit variants first
(`PAIR_VARIANTS` in config.py; for USDBRL: `USDBRL_otc`, `BRLUSD_otc`, `BRL/USD`, `USD/BRL`, `USDBRL-OTC`, …), then
generic forms, then scans the catalogue for any id containing both currencies. Startup log examples:
`USDBRL -> resolved to USDBRL_otc on quotex (variant 1)` or
`USDBRL -> no variant found on quotex; tried [...]; kept in pairs, retry every 30 min`.
Pairs are **never removed**: an unresolved pair is skipped and retried every `RESOLVE_RETRY_MIN` (30) minutes.
- **Flipped quote** (e.g. `BRLUSD_otc`): the bot analyses and trades that instrument's own candles. The card shows the tradeable id
  and the equivalent, e.g. `BRLUSD_otc CALL (= USDBRL PUT)`.
- **Cross-book fallback** (`ALLOW_CROSS_BOOK_FALLBACK=true`, default false; books from `FALLBACK_BOOKS`, default `otc,iq`):
  reads another broker's book. Logs a warning: those prices are **not Quotex's**, so signals won't match Quotex charts.
  Payout is assumed as `CROSS_BOOK_PAYOUT` (0.80); check it on Quotex. A Lite plan opens one book, so the fallback is refused (403) and logged.
- As of 14 Oct 2026, OTCharts' public symbols.md lists USD/BRL OTC only on Pocket Option (`USDBRL_otc`) and IQ Option (`USDBRL-OTC`).
- `COMPOUND_PAIRS = USDINR, USDMXN, USDBRL`. Signal cards for these carry a 🔥COMPOUND-PAIR tag.
- Test: `python test_resolver.py` (mocked catalogues, no network).

## Telegram output split
- **Group (`TELEGRAM_CHANNEL_ID`)** gets only actionable messages: the signal card (Quotex symbol, direction, entry time, expiry,
  :57 playbook), the `✅ CONFIRMED` / `❌ SKIP` line, and, if `GROUP_SHOW_RESULTS=true` (default), a result **photo**
  (dark M1 chart of the last 30 candles, EMA9/21, entry line, entry/expiry candles marked) captioned `✅ WIN | USDINR_otc CALL`.
  Caption format lives in `chart.result_caption()`; chart style in `chart.render_result_chart()`. If rendering fails, the bot sends text.
  `RESULT_CHART=false` sends text only. Charts are drawn from the same OTCharts candles. No Quotex screenshots (no browser, ToS).
- **Admin (`TELEGRAM_ADMIN_ID`)** gets everything else: startup symbol resolution, full vote breakdown and playbook reasoning,
  per-round log (signal → candle at :57 → traded/skipped → result), no-setup heartbeats, ladder events, compounding-pair rotations
  (a pair rotates out for the day after `MAX_LOST_LADDERS_PER_DAY`=2 lost ladders; the replacement is the best pair by rolling WR),
  daily summary (W/L/skipped per pair, budget used), and all warnings.

## Channel style (DunyaBazaar)
- Signal card: chart photo (`SIGNAL_CHART=true`) with caption starting `🚀 Signal ready`, `------ { DUNYABAZAAR } ------`,
  pair, direction, entry, expiry, :57 playbook, and footer `CONTACT : @DBSdesk`. No "sureshot" or guarantee wording.
- Result post (photo + HTML caption): `✅ <b>USDPHP</b>` / `Result: <b>WIN</b>` / `Entry: 62.50800 | Close: 62.49800` / `—` / `🌍 DunyaBazaar`
  (❌ and LOSS for losses). The pair is shown without `_otc`. A flipped quote shows the instrument traded (e.g. BRLUSD).
- Charts: black background, plain green/red candles, small `PAIR-OTC` label top-left, subtle dashed entry line.

## Compounding eligibility
`COMPOUND_MIN_TRADES=30`: a pair may rotate INTO compounding only with ≥ 30 settled trades (cancels and ties excluded) and no
breakeven gate active. If no pair qualifies, the slot stays empty and this is logged. `/status` and the daily summary list
`PAIR n/30 eligible|not eligible`. A one-time `🎓 PAIR now eligible for compounding (30 trades, WR x%)` is sent when a pair reaches 30.
