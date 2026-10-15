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
| `PAIRS` | | optional comma list, default: all 12 pairs |
| `SHADOW_RELAXED` | | `1` (default) runs the relaxed shadow engine (admin chat only, never posted); `0` turns it off |
| `PUBLIC_CHANNEL_ID` | | optional: public channel (e.g. Dunya Bazaar Signals) for `/public` / `/both` |
| `VIP_CHANNEL_ID` | falls back to `TELEGRAM_CHANNEL_ID` | optional: VIP channel. Default target is VIP if set, else PUBLIC |
| `SWITCH_MIN_BODY_ATR` | | default `0.8`: the :57 check SWITCHes only on a clearly opposite forming candle (body ≥ 0.8 ATR) |
| `DISPLAY_TZ_OFFSET_HOURS` | `DISPLAY_TZ` | default `3`: every time shown in Telegram (cards, :57, results, admin logs, summaries) is UTC+3 |
| `DEFAULT_LEVEL` | | `auto` (default, starts at `LEVEL_AUTO_START`=4) or `1`–`5`; `/level` changes it at runtime |
| `MIN_SIGNALS_PER_HOUR` | | default `4`: AUTO loosens (up to level 5, only while win rate ≥ breakeven) until this rate is met |
| `ENTRY_CHECK` | | default `1`: no group signal if the closed candle closed > level's ATR distance from EMA9 (overextended) |
| `BUG_WATCH` | | default `1`: experimental bug/glitch watcher (admin only) |
| `STATE_DIR` | | optional; point at a Railway Volume mount so `pair_stats.json` survives redeploys |
| `DATA_SOURCE`, `REPLAY_CSV`, `TELEGRAM_ENABLED`, `DRY_RUN_MINUTES` | | testing only |

If a required one is missing the bot logs `Missing required env vars: ...` and exits (code 2).
The bot must be an admin of the channel to post. Deploy as a worker (Procfile `worker: python bot.py`).

## Admin commands (from TELEGRAM_ADMIN_ID only)
`/pause`, `/resume`, `/status`, `/stats` (today's full summary on demand, nothing reset), `/mode` + `/stream` `/poll` `/auto` (runtime mode), `/channel` + `/public` `/vip` `/both` (where group posts go), `/shadow` (relaxed shadow stats: per-pair W/L, win rate vs breakeven), `/test` (alias `/testcard`: posts one sample card to the group, headed `🧪 TEST CARD — NOT A LIVE SIGNAL`; no stats/trades/cooldowns touched), `/help`. Stop the Railway service between sessions, or use `/pause` / `SESSIONS_UTC`.


Filters were eased slightly (midway toward the shadow thresholds) for more group signals; payout floor, gap rejection and :57 never-skip are unchanged.
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

# Stream package (otc_bot_stream)
`MODE=stream` (default here) reads live prices from **one** OTCharts `/v1/stream` connection carrying
`STREAM_PAIRS` (default `USDINR,USDIDR,USDCOP,USDMXN,USDBRL`; max `STREAM_MAX_INSTRUMENTS`=5 on Lite) and builds M1 candles from ticks.
`MODE=poll` is the original once-a-minute `/v1/candles` bot. `MODE=auto` polls and switches to the stream when the budget hits 95%.

**Facts from otcharts.com/api.html (14 Oct 2026):**
- Opening a stream costs **one request**; holding it costs nothing more.
- Lite allows **1 stream × 5 instruments**.
- 429 means daily requests are exhausted **or** all streams are in use.
- The stream sends `connected`, `tick` (`price`, `symbol`, `time` always present), `dropped` and `closed` events, plus a keep-alive comment every 20 s.
- Not documented: whether an already-open stream keeps running after the quota is used up. Reopening after exhaustion would need a request, so it would likely be refused.

**Behaviour:**
- **Warm-up:** loads `STATE_DIR/candles/<PAIR>.csv` (last 300 closed candles, saved every minute). Otherwise it fetches `/v1/candles` once per pair if the budget allows (1 request each). Otherwise it builds candles from ticks. Signals start at `MIN_BARS` (60 in stream mode); admin gets `⏳ Warm-up USDINR: 10/60 candles … ✅ Warm-up done`.
- **:57 precheck:** uses the live forming candle built from ticks, at no request cost.
- **Payouts:** `/v1/symbols` every 5 min while REST is allowed, cached to `payouts.json`. If no live or cached payout exists, the bot uses `STREAM_FALLBACK_PAYOUT` (0.85) and logs it to admin. Symbol resolutions (including the BRLUSD flip) are cached to `resolutions.json`.
- **95% budget:** REST calls stop and the stream keeps running (no pause).
- **Reconnects:** automatic with backoff 2→60 s; 5 min after a 429. A `dropped` event reopens the stream after 3 min. Only one connection is ever held.
- **Railway:** mount a volume at `STATE_DIR` so candles, payouts and resolutions survive restarts.
- **Tests:** `python test_stream.py`; dry-run: `DATA_SOURCE=mockstream TELEGRAM_ENABLED=0 DRY_RUN_MINUTES=400 python bot.py`.

## Relaxed shadow engine (`SHADOW_RELAXED`, default on)
On every closed-candle scan the bot also runs a **relaxed** copy of the engine on the same candles (no extra OTCharts requests).
When the relaxed engine signals a pair that the strict engine did not, the **admin chat only** gets
`👻 Shadow (relaxed): USDINR CALL entry 16:42 — not posted`. At expiry it is resolved from candles already fetched/streamed
(`👻 Shadow result USDINR: WIN/LOSS`). Shadow trades are never posted to the group and never affect real signals, the :57 check,
cooldowns, compounding, the pair gate or real stats. They are stored separately in `STATE_DIR/shadow_stats.json` and
`STATE_DIR/shadow_trades.csv`, shown by `/shadow`, and added to the daily summary as a "Shadow (relaxed)" section.

| Setting | Strict | Relaxed shadow |
|---|---|---|
| Min confluence (`MIN_CONF`) | 0.62 | 0.55 |
| ADX trend / range | 23 / 19 | 21 / 20 |
| Efficiency ratio for trend | 0.30 | 0.25 |
| ATR percentile window | 15–96 | 10–98 |
| Min S/R range | 2.5 ATR | 2.0 ATR |
| Strong-candle body | 0.55 | 0.50 |
| Chop reject | 4 of 5 bars | 5 of 5 bars |
| Conflicting votes | reject | allowed, net score decides |
| Overlap rule | log/require | ignored |
| Pair win-rate gate | on | off (shadow has its own stats) |
| 77% payout floor, gap rejection | on | **on (unchanged)** |

Limits: shadows are only evaluated on minutes when the strict scan runs (not during a pending trade/cooldown or pause);
in poll mode a shadow result waits until that pair's candles are next fetched (gives `UNKNOWN` after 20 min); in auto mode
after the switch to stream, shadows on non-stream pairs may end `UNKNOWN`. One open shadow per pair, 3-min cooldown per pair.

## Stream reliability (one connection, 429 handling, fallback)
- Only ONE `/v1/stream` connection can exist in the process: a process-wide lock, the old response/socket/session is closed before
  every reconnect, and a new stream object closes any older one. SIGTERM (Railway restart/redeploy) closes the stream right away.
- 429 is classified from the body: **stream in use** ("one live stream at once ... 1 is open") or **daily quota exhausted**.
  Retries back off 60 → 120 → 300 → 600 s (cap). Before retrying after "in use" the bot checks the free `/v1/usage` `streams.open`.
  The admin chat gets ONE `🔴 Stream down: ...` per episode and ONE `🟢 Stream reconnected`.
- Watchdog: no tick on any stream pair for > 90 s (`STREAM_STALE_SEC`) = dropped → close and reconnect.
- While the stream is down, `STREAM_FALLBACK_POLL=1` (default) polls the 5 stream pairs over REST (≈5 requests/min) as long as
  usage is below quota − `STREAM_FALLBACK_RESERVE` (default 50). If the budget is exhausted, signals pause and the admin chat is told once.
- OTCharts has no endpoint to list or close streams (no DELETE, no stream id). The slot is freed when the connection closes.
  The limit is per ACCOUNT across all keys, so any other program/deploy holding a stream with the same account blocks this one.
  On Railway, turn off overlapping deploys (or expect up to a few retries during a redeploy while the old container shuts down).

## :57 switch rule (direction mode)
The :57 line is always ✅ CONFIRMED or 🔄 SWITCH (never a skip). It SWITCHes only when the forming candle is clearly against the
signal: opposite body ≥ `SWITCH_MIN_BODY_ATR` (0.8 ATR). This applies to every switch reason (momentum and reversion).
A weaker opposite candle or a doji keeps the original direction; the admin log says e.g. `opposite candle 0.53 ATR < 0.8 → kept PUT`.

## /stats
Sends today's full summary right away (per-pair W/L, win rate, skips, prechecks, :57 switches, API budget, compounding
active and eligibility, 👻 shadow section). Read-only: the midnight summary and resets work as before.

## Public / VIP channel switch
`/public`, `/vip`, `/both` choose where group posts go (signal card, :57 line, result photo, /test card); `/channel` shows the target.
The choice is saved in `STATE_DIR/channel_target.json`. An open trade keeps the channel(s) its signal card went to for its :57 line
and result, even if you switch mid-trade. Choosing a channel whose ID isn't set is refused and nothing changes.

## Runtime mode: /stream, /poll, /auto, /mode
`/stream` switches now to the live stream (STREAM_PAIRS, instant warm-up); `/poll` switches now to polling all PAIRS (stream closed
cleanly; refused at/over the 95% stop threshold, warned over 93%); `/auto` returns to automatic switching; `/mode` shows the mode,
manual/auto and budget. A manual choice stays until `/auto` (saved in `STATE_DIR/mode_choice.json`; env `MODE` is only the startup
default). Safety: in manual POLL, hitting the 95% threshold falls back to the stream automatically and tells you. A switch requested
during an open trade runs after it settles. Every switch reuses the single-stream close/lock, so two streams never exist.

## Benched pairs and stream swap
A pair with 2 lost ladders today is benched: no signals for the rest of the UTC day (poll and stream mode), saved in
`STATE_DIR/benched.json`. If it was in the live stream, it is replaced by the best non-benched pair that is open on Quotex with a cached
payout ≥ 77% (highest payout first, then fewer losses today, then better win rate; USDBRL stays eligible):
`🔄 Stream swap: USDINR OUT (2 lost ladders) → USDPHP IN (payout 84%)`. The stream is closed and reopened with the new list
(OTCharts has no "change instruments" call) and only the new pair gets the instant warm-up (1 request). With no eligible replacement
it streams 4 pairs and says so. After 00:00 UTC benched pairs return and the stream goes back to STREAM_PAIRS.

## Time zone
Everything shown in Telegram uses `DISPLAY_TZ_OFFSET_HOURS` (default 3, labelled `UTC+3`): signal card entry/expiry, :57 lines,
results, admin rounds, stream swaps, /mode, /stats and the daily summary. Internally everything stays UTC (candles, OTCharts).
The OTCharts budget resets at 00:00 UTC = **03:00 UTC+3**, and the daily-stats day also runs 03:00 → 03:00 UTC+3 so both match.
Railway log timestamps stay UTC.

## /commands (alias /help) and the Telegram Menu
`/commands` lists every admin command grouped (Control, Stats, Channels, Mode, Test). At startup the same list is registered with
Telegram `setMyCommands`, scoped to the admin chat only (BotCommandScopeChat = TELEGRAM_ADMIN_ID), so it appears under the Menu button
there and nowhere else. The list and the handlers come from one table in bot.py, so they always match.

## Frequency levels (/level)
`/level` shows the current level and every level's thresholds; `/level 1`…`/level 5` sets it; `/level auto` lets the bot choose
(saved in `STATE_DIR/level.json`). Every level keeps the 77% payout floor, gap rejection and the :57 0.8 ATR switch rule.
The 👻 shadow engine always runs one level looser (capped at 5).

| Level | conf | ADX trend/range | ER | ATR pctl | S/R | body | agree | strong-opp | conflict | max ext. from EMA9 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 very conservative | 0.75 | 27/17 | 0.40 | 25–93 | 3.5 | 0.65 | 3 | 0.40 | no | 1.5 ATR |
| 2 conservative | 0.70 | 25/18 | 0.35 | 20–95 | 3.0 | 0.60 | 2 | 0.50 | no | 1.8 ATR |
| 3 balanced (default) | 0.62 | 23/19 | 0.30 | 15–96 | 2.5 | 0.55 | 2 | 0.60 | no | 2.0 ATR |
| 4 aggressive (≈ old shadow) | 0.55 | 21/20 | 0.25 | 10–98 | 2.0 | 0.50 | 2 | 0.70 | yes | 2.5 ATR |
| 5 extreme | 0.45 | 18/18 | 0.20 | 5–99 | 1.5 | 0.45 | 2 | 0.80 | yes | 3.0 ATR |

**Default is AUTO, starting at level 4** (`DEFAULT_LEVEL=auto`, `LEVEL_AUTO_START=4`). Target: `MIN_SIGNALS_PER_HOUR` (default 4).
Every 20 active (unpaused) minutes AUTO counts the signals in the last 60 active minutes; below the target it loosens one level
(up to 5). Level 5 is only used while the last 20 settled trades are at/above breakeven 1/(1+avg payout); otherwise it stays at 4
and tells you once: `⚠️ Target 4/h needs level 5 but win rate below breakeven; staying at 4`. Every 20 settled real trades it also
checks the win rate: below breakeven → one level stricter (never below 1). /status and /level show signals/hour vs the target.
Note: more signals per hour means looser filters; on random-walk test data looser levels did not win more often.
Every change is announced in the admin chat.

## Strategy ensemble (/strategies)
Four independent strategies vote on each closed candle (same candles, no extra requests):
A trend-pullback (EMA9/21 + ADX, pullback to EMA9), B range mean-reversion (low ADX, Bollinger extreme + RSI/Stoch exhaustion),
C S/R rejection (long wick at support/resistance), D the original voting engine. A group signal needs at least `agree` strategies
on the same side (2 by default, 3 at level 1) and no opposing strategy with score ≥ `strong-opp`. The admin "Sent" message and round
log show who agreed. Each strategy is scored on its own direction whenever it voted on a traded signal (/strategies, /stats).
Entry is still the open of the next candle after the closed signal candle: there is no "best entry price" prediction. The only
entry filter is `ENTRY_CHECK`: a signal candle that closed too far from EMA9 (overextended) is not posted (the shadow may still log it).

## Bug / glitch watcher (/bugs) — EXPERIMENTAL, admin only
Using candles already in memory, the bot looks for statistical oddities per pair: colour runs (after 3/4/5 reds or greens),
alternation (RGRG), big-candle reversals (> 1.2 ATR), minute-of-10 and hour-of-day colour bias, and identical consecutive bodies
(feed glitch). A pattern alerts (`🐞 Bug watch (experimental): USDBRL after 3 reds → next green 74% (129 samples)`) only with
≥ 20 samples, ≥ 65% and a binomial p-value below 0.01 divided by the ~50 patterns tested per pair, so random data stays quiet
(0 of 20 random series triggered in tests; small edges like 71% of 24 do NOT pass). Live stream pairs are scanned every closed
candle (minimum 15 samples); other pairs every 5 minutes from candles already fetched. Max 3 alerts/hour, same pattern once per 6 h.
Found patterns are paper-traded forward (W/L) and never posted to the group.
Daily bug review at 00:00 UTC (03:00 UTC+3): forward W/L vs breakeven per pattern; ≥ 20 forward trades at ≥ breakeven + 5 pts →
CANDIDATE (kept across days); a candidate falling below breakeven → back to watch; a non-candidate below breakeven → dropped.
Candidates are information for you only; nothing is auto-posted. Saved in `STATE_DIR/bug_watch.json`.

## Bug signals (/bugsignals) — EXPERIMENTAL
⚠️ Every "bug" below is a hypothesis. Patterns found in history can be false (multiple testing, regime change, feed artefacts).
Treat them as paper research until forward results say otherwise.

- `/bugsignals admin` (default): bug signals go to the admin chat only, e.g.
  `🐞 BUG SIGNAL USDIDR PUT — entry 06:41 UTC+3 · 1 min · pattern: … (fwd 5W/2L) · payout 93% · ⏱ lag …`.
- `/bugsignals group`: the same signals also go to the current group target, labelled `🐞 BUG`, with card → :57 CONFIRMED/SWITCH
  (same 0.8 ATR rule; never skip) → result photo. Tick strategies post a text `ENTER NOW` card and a text result.
- `/bugsignals candidates`: the group gets only patterns whose status is `candidate`. `/bugsignals off`: paper only.
- The mode is saved in `bug_signals.json` and shown in `/bugs` and `/status`.
- Rules: 77% payout floor, gap rejection, no bug signal on a pair that has a pending group trade, at most 1 per pair per
  3 min (`BUG_PAIR_COOLDOWN_MIN`), 1 per pattern per 5 min (`BUG_SIGNAL_COOLDOWN_MIN`). A signal held back by a cooldown is still
  paper-tracked. Results (`🐞 Bug result PAIR: WIN/LOSS (entry→close)`) update the forward W/L.
- Auto-demote: below breakeven after ≥10 forward trades (`BUG_DEMOTE_MIN_FW`) → it stops firing (still paper-tracked) and the admin is told.
- Stream pairs: B2–B6/E/F and patterns are checked on every closed candle (free). Tick strategies (B1/B4/B5s) are checked at
  :15/:30/:45 (`TICK_CHECK_SECONDS`). Poll mode checks closed-candle strategies during the normal scan only (no extra requests).
- `BUG_STRATS` (default `B1,B2,B3,B4,B5,B6,E,F`) enables strategies. `BUG_STRAT_VARIANT=improved|original` (default improved).

### Strategies and critical analysis (original → improved)
| Key | Name | Original rule | Weakness found | Improved adds |
|---|---|---|---|---|
| B1 | 5s Gap Rejection (stream) | 5 s bar spike/gap ≥3× median or wick ≥60% & ≥2.5× median → fade now | fades breakouts; spikes inside news/trend continue | spike must be isolated (prev 3 bars calm), 1m ADX < trend level, fade only away from EMA9, never in the last 10 s |
| B2 | 1m Momentum Gap to S/R | strong body (≥0.8 ATR, ≥60% of range) closing ≤0.3 ATR short of a 2-touch swing level → continue into it | a strong candle near a level often stalls; one-off spikes | close in top/bottom 20%, prior strong body or ATR expanding, skip if the level rejected sharply in the last 15 bars, trend vote must agree |
| B3 | 3-Point Indicator Desync | ZigZag pivot (1.5 ATR, depth 12) after a 3-candle push + DeMarker/RSI14 extreme hooking → fade | in trends the oscillator stays extreme; pivots get broken | range regime (ADX ≤ range+2) and a BB/S-R touch at the pivot or RSI divergence |
| B4 | Frozen Candle micro-lag (stream) | trend + no price change ≥2.5 s, no rejection wick, tick momentum with trend → enter with trend | a freeze in OTCharts may not be a freeze on Quotex; long freezes are outages | `pause` (no ticks) and `flat` (ticks, same price) tracked separately (B4p/B4f); long pauses (>4× threshold) skipped |
| B5 | 5-Consecutive Exhaustion | 5 full bodies (≥70% of range) same colour → opposite; 10 s & 15 s from ticks (B5s10/B5s15), 1 m (B5m) | strong breakouts keep running | skip if ADX ≥ trend+10; last body must not be growing |
| B6 | Bollinger Out-of-Bounds | BB(14,2)/BB(20,2) (B6_14/B6_20): body fully outside the band with a gap → revert | band walks in trends | whole candle (incl. wicks) outside; skip if previous 2 closes also outside (band walk); require ATR spike |
| E | ZigZag + DeMarker scalping | confirmed ZigZag leg end + DeMarker(14) crosses 0.30 up (CALL) / 0.70 down (PUT) | many crosses in trends; MT "deviation 5" = 5 points = noise on 1m OTC | deviation = max(5 points, 1.0 ATR); no strong ADX trend against; cross candle colour must agree |
| F | Double Oscillator Reversal | RSI12 (20/80) and CCI17 (±100, the standard CCI extremes) both extreme in the last 3 bars and both hook on a GREEN (CALL) / RED (PUT) close | hooks in a trend are just pauses | no strong ADX trend against; the hook candle must recover the previous close |

ZigZag is computed causally (confirmed pivots only: the pivot must be the extreme of the last 12 bars, with a ≥ deviation leg into it and a ≥ deviation
retrace after it, all from bars ≤ the signal bar). B3 and E share this confirmation logic. Tests check that every candle strategy gives the same answer on a
truncated frame (no lookahead). E and F also vote in the group ensemble (`ENSEMBLE_EF=1`) and keep their own W/L in `/strategies`.

**B1/B4 latency caveat:** OTCharts ticks arrive with network + aggregation delay (see `/telemetry` lag). Quotex may already have moved, so any
sub-second edge can disappear. Tick strategies are stream-only and experimental.

**Validation caveat:** the bundled replay data and the test data are synthetic random walks. Signal counts show how selective each variant is;
win rates on that data are ~50% by construction and say nothing about real edge. Only forward results on the live feed count.

## Telemetry (/telemetry) — admin only, no extra requests
- Delivery (stream pairs): inter-arrival median/p95, ticks per second-of-minute (text heatmap), pauses > 2 s, duplicate prices, jumps ≥ 5× median move.
- Lag: tick event time → receive time (needs a synced clock), candle close → first tick of the next minute, :57 precheck read time.
- Over-extension: per closed candle z = (close − EMA20)/ATR, body/ATR and run length → next-candle reversal %, binned per pair. Bins that pass the
  same Bonferroni binomial test become bug patterns (`PAIR:ox:z2.5+` etc.) and fire through the bug pipeline. This overlaps with B6 by design.
- Every bug and group signal admin message carries `⏱ lag med/p95` at signal time. The 03:00 UTC+3 review includes a short telemetry section.
- CSV: `STATE_DIR/telemetry_YYYY-MM-DD.csv`, 1 in 10 normal ticks + every anomaly, capped at `TELEMETRY_MAX_MB` (20) per day, 3 days kept.
  Memory is bounded (fixed-size deques). `TELEMETRY=0` turns it off.

## Leaderboard (/best)
- All ensemble members (A–F, scored on their own vote when they voted on a traded signal) and all bug strategies/patterns (forward W/L).
- Score = Wilson 90% lower bound of win rate − breakeven; ranked only with ≥20 trades.
- Daily review (03:00 UTC+3): score > +3 pts → ensemble weight boost / bug strategy becomes `candidate` (posted to the group only if
  `/bugsignals candidates|group`); below breakeven after ≥30 trades → muted (still paper-tracked, can recover). Ensemble weights are bounded 0.5–1.5
  and only change once a member has ≥20 trades. The group invariants (≥2 agreeing members, 77% floor, gap rejection, :57 never skips) are unchanged.

## Group gets only the BEST strategies (`GROUP_FILTER=best`, default)
- A group post needs a best-list strategy (Wilson LB ≥ breakeven + 3 pts with ≥ 20 forward trades; see `/best`). For ensemble signals the weighted
  vote of best-list members must agree with the signal direction. Anything else goes to admin as `🔒 Not sent (not yet proven): …` and is
  paper-tracked (`ENS` in `/bugs`, and every member's vote is scored, so members can still qualify).
- Bootstrap, until anything qualifies: `BEST_BOOTSTRAP=ensemble` (default; the normal ensemble at the current level posts) or `silent`.
  When the first strategy qualifies, the admin gets one message saying the filter is live (stored in `leaderboard.json`).
- Bug signals reach the group only if their key is on the best list: `/bugsignals group` behaves like `candidates`. `GROUP_FILTER=all` restores the old behaviour.
- The group still gets only card, :57 line and result. 77% floor, gap rejection and the :57 never-skip rule are unchanged.

## PUBLIC session (/publiccap)
- The PUBLIC session opens daily at `PUBLIC_SESSION_START=10:00` (UTC+3). Before that, and after it closes, new signals go to VIP only
  (in `/both` mode as well as when the target is `public`).
- It closes when the target is reached (`PUBLIC_NET_TARGET=2`). The default `PUBLIC_CLOSE_RULE=wins` follows the user's examples:
  close when wins ≥ 2 and net ≥ +1, so W,W / W,L,W / W,L,L,W,W all close. `PUBLIC_CLOSE_RULE=net` is the strict reading
  (wins − losses ≥ +2); under it, W,L,W would not close. Check which one you mean.
- Safety stops: `PUBLIC_MAX_SIGNALS=10` signals or net ≤ `PUBLIC_MAX_NET_LOSS=-3`. Either one closes the session with an honest closing
  message (W/L and net) and an admin alert explaining it is a protection.
- Losses are never hidden. Trades already open finish with their :57 line and result, and still count. Closing messages go to PUBLIC
  (`PUBLIC_TARGET_MSG=1`).
- `/publiccap` shows start time, state, W/L, net and signals used. You can change it with `/publiccap target N`, `/publiccap max N`,
  `/publiccap maxloss -N` and `/publiccap start HH:MM`. Settings are saved in `public_cap.json`, so a restart mid-session keeps the
  counts. The state also shows in `/status` and `/stats`. Counts reset at local midnight; a new session starts at the next start time.

## Dedicated 🐞 bug group (`BUG_CHANNEL_ID`, optional)
- When `BUG_CHANNEL_ID` is set, every live bug signal (B1–B6, E/F bug-style, statistical and over-extension patterns) posts there.
  That covers next-candle cards, `ENTER NOW` cards, the :57 CONFIRMED/SWITCH line and the result with its chart photo, all labelled `🐞 BUG SIGNAL`.
  The posts carry no internal stats or reasoning; those stay in admin.
- It is always on. There is no off switch. `/bugsignals` modes only change the extra routing (`admin` copy, public/VIP via `group`/`candidates`, `off` = no admin copy).
  Only `/pause` stops it.
- Bug signals still never reach PUBLIC/VIP unless the strategy is on the `/best` list and `GROUP_FILTER` allows it.
- Cooldowns, the 77% floor and gap rejection apply. At 03:00 UTC+3 the bug group gets a short daily W/L per bug strategy.
- `/status` shows `bug group ON (always)` or `not set`.

### Env summary (new this round)
`BUG_CHANNEL_ID`, `BUG_SIGNALS`, `BUG_STRATS`, `BUG_STRAT_VARIANT`, `BUG_SIGNAL_COOLDOWN_MIN`, `BUG_PAIR_COOLDOWN_MIN`, `BUG_DEMOTE_MIN_FW`, `B4_FREEZE_S`,
`ENSEMBLE_EF`, `GROUP_FILTER`, `BEST_BOOTSTRAP`, `TELEMETRY`, `TELEMETRY_CSV`, `TELEMETRY_MAX_MB`, `TELEMETRY_TICK_SAMPLE`, `PUBLIC_SESSION_START`,
`PUBLIC_NET_TARGET`, `PUBLIC_CLOSE_RULE`, `PUBLIC_MAX_SIGNALS`, `PUBLIC_MAX_NET_LOSS`, `PUBLIC_TARGET_MSG`, `PUBLIC_TARGET_TEXT`, `PUBLIC_STOP_TEXT`.

## Scheduled posts and event hooks (/sched)
Saved in `STATE_DIR/schedule.json`. Times are UTC+3 (checked every minute, so a post goes out at about HH:MM:02).
Targets: `public`, `vip`, `bug`, `both` (public+vip), `all` (public+vip+bug), each mapped to its `*_CHANNEL_ID`.

```
/sched                                            list (with ids)
/sched add 10:00 public daily text <b>Good morning traders</b> 🚀
/sched add 09:55 vip mon,wed,fri sticker last      (last = the last sticker you sent the bot)
/sched add 12:00 all once 2026-10-20 copy last     (copyMessage of the last channel post you forwarded: no "forwarded" label)
/sched add 12:00 bug daily forward -100123:45      (forwardMessage)
/sched add 10:00 public daily photo AgAC...|Caption <b>HTML</b>
/sched add 10:01 public daily pin 42:silent        (pinChatMessage; repin = unpin + pin)
/sched on on_public_session_start public sticker last
/sched on on_public_session_close public text See you tomorrow 👋
/sched on on_daily_target public copy last
/sched on on_bug_signal_first_of_day bug text 🐞 Bug hunting is live
/sched del 3 · /sched test 3 (send now) · /sched pause 3 · /sched resume 3
```
- Capture: send the bot a sticker or photo and it replies with the `file_id`. Forward a channel post to it and it saves `from_chat_id:message_id`.
  Either can then be used as `last`.
- Events: when no hook is configured, the built-in texts are used (`PUBLIC_START_TEXT` at session start, the target/safety closing messages at close).
  A configured hook replaces the default for that event.
- Rate limiting: at least `SCHED_MIN_GAP_S` (0.05 s) between calls and `SCHED_PER_CHAT_GAP_S` (1 s) per chat. A 429 is retried once after
  `retry_after`. Failures go to admin; for pins you get a hint to give the bot the "Pin messages" admin right.
- `/sched` text keeps your capitalisation and HTML. `/pause` does not stop scheduled posts. Use `/sched pause <id>` for that.


## Live-feedback changes (first real session 2026-10-15)
- Session results: real L4 4W/1L. Shadow at L5 went 8W/14L (36%, breakeven 54.4%). The bug pattern USDIDR hour03 went 7W/10L forward.
- Shadow level: live+1 capped at 4. When live ≥ 4, the shadow is "L4 with a single B or C strategy" (`ENSEMBLE_MIN_AGREE=1`, `ENSEMBLE_ONLY=BC`),
  which tests whether the two strongest members are worth posting alone. The shadow never runs at L5.
- Auto level is capped at 4 (`LEVEL_AUTO_MAX=4`). `/level 5` still works manually and shows a warning.
- Bug watcher: hour-of-day and minute colour-bias patterns are off (`BUG_TIME_PATTERNS=0`), and stored ones are dropped on load.
  Statistical patterns need ≥40 samples and ≥68%. They only send live signals after ≥10 forward trades at ≥ breakeven; until then they are
  paper-tracked. They are auto-demoted below breakeven after 10. The named B1–B6/E/F rule-based strategies signal from the start.
- Ensemble prior weights: B and C 1.2, A/D/E/F 1.0 (`STRAT_PRIOR`). The leaderboard overrides these after ≥20 trades.

## Restarts keep today's stats (fixed 2026-10-15)
- Bug: on shutdown/redeploy the bot ran the daily rollover by force. It sent "📊 Daily summary", then cleared and saved the day/strategy/shadow
  counters, and today's per-pair W/L was never written to disk.
- Fix: rollover only happens when the UTC day has actually changed. On shutdown the bot sends "Stats so far … NOT reset" and saves.
  `day_state.json` holds today's W/L, kinds, prechecks, switches, kept and ladders. It is saved after every result and precheck and reloaded at startup.
  If the saved day is older than today, its summary is sent once and then reset.
- Saved stores: `pair_stats.json` (all-time W/L = 30-trade eligibility), `day_state.json`, `strategy_stats.json`, `shadow_stats.json`,
  `benched.json` (bench/compound/lost ladders), `bug_watch.json`, `leaderboard.json`, `public_cap.json`, `schedule.json`, `level.json`, mode/channel files.
- At startup the bot warns the admin loudly if STATE_DIR is unset or has no marker from a previous run:
  "⚠️ STATE_DIR not persistent: stats will reset on every deploy. Add a Railway volume at /data and set STATE_DIR=/data".
  **On Railway, add a volume mounted at /data and set `STATE_DIR=/data`.** Without one, every deploy starts from zero.

## /reset (admin only)
`/reset` shows usage. `/reset today|stats|bugs|telemetry|all` asks for confirmation, and `/reset confirm` within 60 s runs it.
A backup is written first to `STATE_DIR/backups/reset_<scope>_<time>.json` (telemetry CSVs are copied there), and the admin gets a list of what was cleared.
- `today`: today's stats and public-session counters.
- `stats`: all W/L (per-pair trade counts, shadow, strategies, leaderboard) plus today.
- `bugs`: bug-watcher patterns, samples and forward W/L.
- `telemetry`: telemetry counters and CSVs.
- `all`: everything above plus bench/compound. It keeps channel, mode, level, schedules, bug-signal mode and public-session settings.
Confirmation is text only (`/reset confirm`); there are no inline buttons.

## Admin button menu (/menu, /start)
`/menu` (or `/start` in the admin chat) sends an inline keyboard with Status, Stats, Best, Bugs, Telemetry, Schedules, Pause/Resume (toggle), Level (1–5/auto),
Channel (public/vip/both), Mode (stream/poll/auto), Public session, Reset (today/stats/bugs/telemetry/all) and Commands. Sub-menus have a ⬅️ Back button.
- Buttons call the same handlers as the typed commands, so typed commands keep working.
- Read-only buttons run immediately. Buttons that change a setting or delete data (pause/resume, level, channel, mode, reset) ask ✅ Yes / ❌ No first.
  A reset button writes the backup and clears in one step after Yes.
- Only `TELEGRAM_ADMIN_ID` can use the buttons; anyone else gets "Not allowed". The bot asks Telegram for `message` and `callback_query` updates.
- At startup `setMyCommands` registers every command for the admin chat only, so they show in Telegram's "/" menu.

## Historical confirmation gate (/history) — applies to ALL signals by default
`HIST_GATE=all|bugs|off` (default `all`). Before a card is sent (so the :57 never-skip rule is untouched once a card is out), the bot replays
the same detector over this pair's stored 1m candles and simulates next-candle expiry for every past trigger.
- It sends only if 30d n ≥ `HIST_MIN_N` (15) and the Wilson 90% lower bound (7d occurrences counted ×2) is ≥ breakeven(payout) + `HIST_MARGIN` (0.03).
  The 7d win rate must also not be below breakeven when 7d n ≥ 5, and at least `HIST_MIN_COVER_DAYS` (7) days must be stored.
- Ensemble: the agreeing members' past triggers are pooled. D (the full voting engine) is too slow to replay over 30 days and is left out;
  the ensemble needs ≥2 agreeing members, so at least one replayable member is always present. B1–B6/E/F use their own detectors.
  Statistical patterns use their scanned history sample with the same thresholds. Tick strategies (B1/B4/B5s) have no stored tick history,
  so they are always filtered (admin only). The shadow is tagged pass/filtered and still tracked.
- A failure goes to admin as `📚 Filtered by history: … (reason, seen N× …)` and is paper-tracked. On a pass, the history line (`📚 seen 23× in 30d, 17 won (74%)`) goes to admin only.
  `/stats` shows passed/filtered counts for today (saved across restarts).
- Superseded by the tiers below: the gate itself is never loosened, but Tier-2 fill signals can top up to `MIN_SIGNALS_PER_HOUR`.
- **Until ≥7 days of candles are stored, every signal is filtered** (admin only, still tracked). With `HIST_GATE=all` the group stays quiet
  until the store fills. Use `/history bugs` or `/history off` to keep the group posting in the meantime.
- Candle store: `STATE_DIR/candles/<pair>.pkl`, filled from the stream/polls you already receive (free), 30 days kept, saved every 15 min.
  Backfill: one `/v1/candles` call per pair per day with `limit=HIST_BACKFILL_LIMIT`, capped at `HIST_BACKFILL_MAX_REQ` (800)/day.
  OTCharts' `/v1/candles` only takes `limit` (no start/end), so how deep a backfill reaches depends on the API's maximum limit (not verified without a key).
- CPU: replaying 30 days costs ~3–23 s per strategy per pair the first time (measured: B2 slowest). The scan is incremental and runs as a background
  warm-up of `HIST_WARM_BUDGET_S` (3 s) per minute. A check that hits unscanned bars waits at most `HIST_CHECK_BUDGET_S` (2 s), then filters as "warming up".
- `/history` shows mode, coverage per pair, passed/filtered and backfill requests. `/history all|bugs|off` changes the mode (saved). Menu: 📚 History.


## Signal tiers (≥ 4 signals/hour)
- **Tier 1 (admin tag: ✅ history-confirmed x/n)**: passes the history gate and is always sent. All candidates of the minute are checked best-confidence first.
- **Tier 2 📘 standard (fill)**: from minute :`FILL_START_MIN` (40), if fewer than ceil(4 × minute/60) signals have gone out this hour
  (3 by :40, 4 by :45), the best candidate that failed or lacked history may be sent. It must still meet the normal level, GROUP_FILTER and
  public-session rules, and its history must be ≥ breakeven or missing/thin. **Never** when history is clearly negative (Wilson upper bound < breakeven).
  The admin tag is "📘 standard".
- Tier W/L is tracked separately: today and all-time appear in `/stats` and `/best`, and the totals are saved in `tiers.json`.
- `/fill on|off` (menu: 🔁 Fill 4/h) toggles Tier 2 (`FILL_MIN_HOUR=1` default). `HIST_GATE=all|bugs|off` stays configurable.
- `/reset stats` clears the tier totals and `/reset today` clears today's tiers.

**Visibility:** public, VIP and bug-group cards are unchanged (signal card, :57 line, result photo). History stats, tier labels, "filtered by history"
notices and per-tier W/L appear only in the admin chat. Each sent signal gets a short admin mirror: `🏷 PAIR DIR HH:MM · Tier N · history x/n`.

## Loss-streak cooldown
After `LOSS_STREAK_N` (3) losses in a row, new signals freeze for `COOLDOWN_MIN` (30) minutes. Public/VIP and the bug group have separate counters. Ties are ignored and a win resets the count. State is persisted in `streak.json`. The admin gets start and end alerts. During a freeze there is no Tier-2 fill, and frozen minutes don't count toward the hourly quota. Commands: `/cooldown`, `/cooldown n 3`, `/cooldown min 30`, `/cooldown clear live|bug`. Also available from the menu.

## Session-transition blackouts and the news blackout
`BLACKOUT_WINDOWS` is written in zone-local time and converted with zoneinfo, so it follows DST. The defaults:
- London open 07:45–08:30: 09:45 UTC+3 in summer, 10:45 in winter.
- New York open 09:15–09:45.
- UTC rollover 23:45–00:15.

`NEWS_FILTER` blocks a pair for ±`NEWS_BLACKOUT_MIN` (15) minutes around high-impact ForexFactory events for either of its currencies.
- The feed is cached as `ff_calendar.json` in STATE_DIR and refreshed every 6 h.
- If a refresh fails, the bot keeps using the cache and alerts the admin once.

Commands: `/blackout [on|off]`, `/news [on|off]`, plus the menu. The public session now starts at **10:30 UTC+3**, which is after the London-open blackout.

## Double confirmation (`DOUBLE_CONFIRM=on|off`, default on; `/confirm2`, menu toggle)
Every source (ensemble, every bug strategy including tick ones, shadow) needs both checks below in the signal direction, evaluated on the last closed 1m bar:
- **Momentum:** RSI ≤35/≥65, DeMarker ≤0.3/≥0.7, CCI17 beyond ∓100, Stoch %K ≤20/≥80, a 3-candle streak into the level, or a body ≥0.5 ATR in the signal direction.
- **Structure:** Bollinger z ≤−1.5/≥1.5, price within 0.3 ATR of the swing low/high (S/R), a ZigZag pivot (12-bar extreme in the last 3 bars), or a rejection wick ≥50% of the range.

Signals that fail are dropped and logged in the admin chat as `single-confirmation` (at most one note per pair every 5 minutes). The daily summary shows how many were dropped.

How existing logic maps onto the two categories:
- **Momentum:** A (trend/ADX), D, E crosses, F oscillators, B4, B5 streaks.
- **Structure:** B (BB), C (S/R and wick), B2 levels, B3/E pivots, B6 bands, B1 wick.

Strategies that used only one category now need the other as well.

Breakeven is the dynamic `1/(1+P)` from each pair's live payout everywhere. The leaderboard now uses the mean live payout, and the compound gate uses the pair payout; both fall back to `DEFAULT_PAYOUT` only when no payout is cached.

## Frequency target and the Tier-2 fill pool
- **Wide fill pool.** Every pair is scanned every minute. When no strict signal exists, a candidate may still come from the level-4 thresholds (`FILL_LEVEL=4`).
- **Relaxed structure for fills.** A fill needs any one of: a Bollinger touch, S/R within 0.3 ATR, or a wick ≥ 40% (`FILL_WICK_MIN`). It still needs a momentum trigger, the 77% payout, gap rejection, and history that is not clearly negative.
- **Ranking.** Fills are ranked by score (confidence plus how far history sits above breakeven). At most one fill per pair per 10 min (`FILL_PAIR_GAP_MIN`).
- **Pacing.** The aim is one signal per 15-minute slot (`FILL_SLOT_MIN`). Tier 1 gets the first 3 minutes of each slot (`FILL_SLOT_WAIT`), and missed slots may catch up. Blocked minutes (blackout, cooldown, daily stop, news) are excluded.
- **Simulation result:** see the report. On 7 days of real FX 1m data across 15 pairs, the clean rules give ~1–2 signals per open hour, not 4. The target is **not achievable** without dropping safety rules.

## Candle patterns P
See `PATTERNS.md`. Command `/patterns`, plus a menu button. History is mandatory and admin-only until proven.

## Filter precedence (`/filters`)
The first rule that applies decides, so filters never clash:
1. pause
2. news
3. session blackout
4. loss-streak cooldown
5. daily loss/profit stop
6. payout floor (77%, re-checked at send time)
7. history gate (mandatory for P)
8. double-confirm (relaxed structure for fills)
9. ensemble/level
10. Tier-2 fill
11. the :57 never-skip line, which applies only after a card is sent

## Daily loss / profit stops (`/dailystop`, menu)
Counted on net trades (wins − losses, ties ignored) per trading day, which runs 08:00 → 08:00 UTC+3 (`TRADING_DAY_START`). Public/VIP and the bug group keep separate counters.
- **Loss stop.** `DAILY_STOP_NET=5` net losses → no new signals until 08:00 next day.
- **Profit alert.** At `DAILY_PROFIT_ALERT=15` net wins, the admin gets a one-time alert: "You're +15 today (W/L) — consider taking profits".
- **Profit close.** At `DAILY_PROFIT_CLOSE=20` net wins, signals close until 08:00 next day and the admin is alerted.
  - If the public session is open, it closes with the normal closing post. Groups get nothing else.
  - Scheduled posts and private tracking continue.
- **Persistence.** State is kept in `streak.json`.
- **Commands:** `/dailystop loss N | alert N | close N | off | clear [live|bug] | resume HH:MM`.

## Weekly report and self-tune (Sunday 03:00 UTC+3)
At the Sunday rollover the admin gets a weekly report covering:
- W/L and win rate vs breakeven
- expectancy
- per-day lines
- the top strategies
- pattern records

`ablation.py` then re-runs the filter ablation in a background thread, on the stored candles (live plus cached) and the cached news.
- **What can be auto-applied:** only *safe* changes (double-confirm on/off, level 3↔4 when auto-level is off), and only if the winning config has expectancy > 0 and a Wilson LB ≥ breakeven.
- **What is never touched:** hard blocks (blackout, news, cooldown, daily stops, payout).
- Everything else is a proposal. `/weekly` and `/weekly tune` run them on demand. `WEEKLY_TUNE=off` and `WEEKLY_TUNE_APPLY=off` disable them.

## Volume mode: Tier 3 (`VOLUME_MODE=on|off`, default **on**; `/volume on|off`, menu toggle)
Volume mode adds Tier-3 "volume" fills, aiming for 4–6 signals per open hour. They run after Tier 1 (history-confirmed) and Tier 2 (standard fill), and only when the hour is behind its pace.

**Pacing and limits**
- Target 4 per hour (`VOLUME_TARGET`), cap 6 per hour (`VOLUME_MAX_HOUR`; this also caps Tier 2).
- About one per 10-minute slot (`VOLUME_SLOT_MIN`). Tier 1/2 get the first 3 minutes of each slot.
- At most one per pair per 10 minutes. Blocked minutes are not caught up.

**Candidates**
- Level-4 candidates only, ranked by score.
- They must pass the 77% payout floor (re-checked at send time) and every hard block: pause, news, blackouts, cooldown, daily loss/profit stops.
- Double-confirm uses the fill-pool structure rules: BB touch, S/R within 0.3 ATR, or wick ≥ 40%, plus a momentum trigger.
- History must only be "not clearly negative": Wilson UB ≥ breakeven, or no history.
- P patterns are never used (they always need history).

**Display and tracking**
- The group card is unchanged. The tier appears only in the admin mirror (`🏷 … · Tier 3 · 📙 volume`).
- Tier 3 is tracked separately in `/stats`, `/best` and the weekly report.
- **Safeguard:** if Tier 3 falls below breakeven after ≥ 40 live trades (`VOLUME_GUARD_N`), the admin gets a one-time alert suggesting `/volume off`. Volume mode is never disabled automatically.

**Simulation** (`sim/volume_sim.py`, results in `sim/volume_results.txt`): on 7 days of real FX 1-minute data across 15 pairs, volume mode raises the rate from about 1.2 to about 1.7–2.0 signals per open hour. Only 14–20% of hours land in the 4–6 range. Tier 3 wins about 48–52%, below breakeven, so it adds volume, not edge.
