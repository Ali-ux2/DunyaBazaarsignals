# DunyaBazaar OTC Signals Bot

An automated **M1 signal bot** for the **DunyaBazaar** Telegram channel. It reads live OTC price data from OTCharts, applies a **7-indicator vote-based confluence analyzer**, and posts formatted signals to a Telegram channel — no Martingale, no auto-trading, no fake promises.

---

## ⚠️ Disclaimer — Read This First

This bot is for **educational and research purposes only**. It is **not financial advice**.

- OTC binary options are **synthetic markets** where the broker controls the price feed.
- At **85% payout** you need **~54.1%** win rate just to break even. At **70% payout**, **~58.8%**.
- Most retail binary options traders **lose money over time**.
- **Never risk more than 1–2%** of your account per trade.
- **Past performance does not guarantee future results.**
- Use only on **demo accounts** until you have at least **100 tracked trades** confirming a real edge.

If you are not comfortable with these facts, do not deploy this bot.

---

## 🎯 What It Does

- Tracks **12 exotic OTC pairs** on the Quotex book via OTCharts.
- Analyzes **7 indicators** on every 1-minute candle.
- Fires a signal only when **at least 4 of 7** indicators agree.
- Sends **only the single best signal per scan cycle** (no spam).
- Enforces a **3-minute cooldown** after every signal.
- Posts results with **entry and close prices** for full transparency.
- Sends **real-time admin feedback** so the operator can monitor every action.
- Tracks **100 candles per pair** for analyzer precision.

---

## 🧠 Analyzer Logic

Every 1-minute candle, 7 independent indicators each cast a vote:

| # | Indicator | Vote |
|---|---|---|
| 1 | EMA 9 vs EMA 21 (trend) | +1 (CALL) / −1 (PUT) |
| 2 | EMA 5/20 fresh crossover | ±1 only on the cross candle |
| 3 | RSI(7) below 25 or above 75 | +1 / −1 |
| 4 | Bollinger(20, 2) band touch | +1 / −1 |
| 5 | Stochastic(5, 3, 3) cross in extreme zone | +1 / −1 |
| 6 | MACD(12, 26, 9) histogram zero-cross | +1 / −1 |
| 7 | Candle pattern (engulfing / pin bar) | +1 / −1 |

**Score = sum of all votes.**

- Score ≥ +4 → **CALL signal**
- Score ≤ −4 → **PUT signal**
- −3 to +3 → **No signal** (filters out weak setups)

**Confidence** = `|score| / 7 × 100%`.  
For example, a 5/7 alignment shows as **71% confidence**.

Adjust `MIN_SCORE` in `config.py` to change sensitivity:

- `MIN_SCORE = 3` → more signals, lower quality
- `MIN_SCORE = 4` → **default**, balanced
- `MIN_SCORE = 5` → fewer signals, stricter
- `MIN_SCORE = 6` → rare, high-conviction signals only

---

## 📂 File Structure
 
.
├── analyzer.py          # Vote-based confluence engine
├── bot.py               # Main loop, Telegram I/O, signal dispatch
├── charting.py          # Candlestick chart generator (Pillow)
├── config.py            # Pairs, periods, thresholds, cooldowns
├── requirements.txt     # Python dependencies
├── Procfile             # Railway worker command
├── runtime.txt          # Python version pin
├── .gitignore           # Keeps .env and cache out of Git
└── README.md

---

## ⚙️ Setup

### 1. Prerequisites

- A **Telegram bot** created via `@BotFather`
- A **Telegram channel** where the bot is an admin with **Post Messages** permission
- An **OTCharts API Lite** plan ($15/month) with a valid API key
- A **Railway** account (free trial works)

### 2. Environment Variables (Railway → Variables)

Add these **four** variables. Nothing else.

| Variable | Description | Example |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather | `123456:ABC-DEF...` |
| `TELEGRAM_CHANNEL_ID` | Numeric ID of your channel (must start with `-100`) | `-1001234567890` |
| `OTCHARTS_API_KEY` | From otcharts.com/account | `otc_live_...` |
| `TELEGRAM_ADMIN_ID` | Your personal Telegram user ID | `123456789` |

**How to get them:**

- **Bot token**: Talk to `@BotFather` → `/newbot` → copy the token.
- **Channel ID**: Post a message in your channel → forward it to `@JsonDumpBot` → look for `"chat": {"id": -100...}`.
- **OTCharts key**: Log in to otcharts.com → Account → Create API key (shown only once).
- **Admin ID**: Talk to `@userinfobot` → it replies with your personal ID.

### 3. Deploy to Railway

1. Push this repo to GitHub.
2. Railway → **New Project** → **Deploy from GitHub repo** → select this repo.
3. Paste the four variables into the **Variables** tab.
4. Railway detects the `Procfile` and runs `python bot.py` as a worker.

### 4. Verify

In your bot's private chat:

1. Send `/feed` → expect a status panel with engine status, request count, next scan time.
2. Send `/testcard` → sends a test signal + chart to your channel.
3. Send `/resume` → the bot begins scanning every minute.

If `/feed` returns an error or no response, one of the four variables is wrong. Check them.

---

## 🕹️ Admin Commands

Only the user whose Telegram ID equals `TELEGRAM_ADMIN_ID` can run these.

| Command | Effect |
|---|---|
| `/start` | Resume the scanning engine |
| `/pause` | Pause scanning (active trades still resolve) |
| `/resume` | Resume scanning |
| `/feed` | Show status: engine, request count, pending/active trades, cooldown |
| `/testcard` | Send a test signal + chart to the channel |

---

## 🕐 Timing & Behaviour

| Event | When |
|---|---|
| Warmup | On startup — fetches 80 candles per pair |
| Scan | Every minute at `:52` seconds |
| Entry | 8 seconds later, at `:00` of the next minute |
| Expiry | 1 minute after entry |
| Result posted | 1 second after expiry |
| Signal cooldown | 3 minutes after each signal |
| Result cooldown | 2 minutes after each result (if longer than signal cooldown) |

**No two signals will ever be sent within 3 minutes of each other.** If a setup appears during cooldown, it is skipped. This prevents channel spam and reduces API load.

---

## 🔄 Timeframe Variants

The bot ships with two configurations, kept in separate local folders:

| Setup | `CANDLE_TIMEFRAME` | `TREND_TIMEFRAME` | Notes |
|---|---|---|---|
| **M1 (No Filter)** | `60` | — | Faster sample collection, more signals |
| **M1 (With M5 Filter)** | `60` | `300` | Filters against M5 higher-timeframe trend |

**To switch:** replace `config.py` and `bot.py` on GitHub with the corresponding version. `analyzer.py`, `charting.py`, `requirements.txt`, `Procfile`, `runtime.txt`, and `.gitignore` stay the same.

`/feed` shows which version is running:

- **No-Filter:** `DunyaBazaar M1 Status (No Filter)`
- **With-Filter:** `DunyaBazaar M1 Status (M5 Filter)` — also shows current M5 trend per pair

---

## 📊 Tracking & Tuning

**Log every signal** for the first 30–50 trades:
 
date | time | pair | direction | confidence | result

Example:
 
11/10 | 05:12 | USDBRL | CALL | 71% | WIN
11/10 | 05:18 | USDMXN | PUT  | 57% | LOSS
11/10 | 05:25 | USDINR | CALL | 86% | WIN

After 30+ trades, evaluate:

- **Win rate** — must exceed 52.9% (at 89% payout) to be profitable
- **Confidence bands** — do 6/7 signals (86%) beat 4/7 signals (57%)?
- **Pair performance** — is one pair dragging the average down?
- **Session performance** — are losses clustered at specific hours?

Tune `MIN_SCORE` upward or downward from real data, not from feeling.

---

## 🔒 Request Budget

OTCharts API Lite allows **7,000 requests/day**.

At default settings (12 pairs, scan every minute, 3-minute cooldown):

| Activity | Approx. requests/day |
|---|---|
| Warmup (one-time) | ~1,000 |
| Scanning | ~4,000 |
| Outcome checks + charts | ~1,000 |
| **Total** | **~5,000** |

Well under the 7,000 limit. Watch the request counter in `/feed` — if it climbs above **5,500** by mid-day, pause the bot until the next session.

---

## 🚫 What This Bot Does NOT Do

- **No Martingale** — flat risk only, no recovery betting
- **No auto-trading** — it sends signals; the user executes manually
- **No guaranteed win rate** — anyone claiming that is lying
- **No fake AI** — confidence is simply the vote alignment percentage
- **No stored secrets** — API keys and tokens live only in Railway env variables

---

## 🛠️ Troubleshooting

| Symptom | Likely Cause |
|---|---|
| Bot crashes on Railway | Missing or wrong env var — verify all four |
| Bot runs but doesn't post | Bot isn't admin in channel, or `TELEGRAM_CHANNEL_ID` is wrong |
| `/testcard` returns "Forbidden" | Bot lacks **Post Messages** permission in channel |
| Wrong results shown (WIN as LOSS) | Broker-vs-API timing difference on exotic pairs — check entry/close prices in the result |
| Too many losses | Market regime; raise `MIN_SCORE` to 5 for stricter filter |
| Requests exhausted early | Reduce pairs in `config.py` or increase cooldowns |
| `SyntaxError: invalid syntax` on Railway | Code corruption from mobile GitHub paste — re-upload `bot.py` via file upload, not text paste |

---

## 📈 Realistic Expectations

**What this bot can do:**

- Filter out ~60% of low-quality signals
- Send only the highest-confluence setups per cycle
- Operate 24/7 without emotion or fatigue
- Log every result honestly with real prices

**What this bot cannot do:**

- Predict OTC price movement with 80%+ accuracy
- Overcome the broker's structural advantage
- Guarantee profit

**The honest target is 55–65% win rate over 100+ trades.** Anything higher is variance. Anything lower means the strategy has no edge on the current market, and you should stop.

---

## 📜 Version History

- **v1.0** — Initial release with 7-vote confluence analyzer
- **v1.1** — Added 3-minute signal cooldown to prevent channel spam
- **v1.2** — Added entry/close prices to every result message
- **v1.3** — Split into No-Filter and With-M5-Filter variants
- **v1.4** — Removed Martingale logic entirely
- **v1.5** — Current version: vote-based analyzer, admin feedback, request counter

---

## 📄 License

**Personal use only.** Not for redistribution, resale, or public rebranding without permission.

---

## 💬 Support

If you find a bug or want to tune the strategy further, keep a clean log of:

1. The signal message
2. The result message
3. The railway log snippet (if something crashed)

With those three things, any issue can be diagnosed in minutes.

**Trade small. Log everything. Let data decide.**
