# DunyaBazaar OTC Signals Bot

Automated trading signal bot for the DunyaBazaar Telegram channel. It connects to OTCharts to track live Quotex data across 7 currency pairs and uses the S3 (Simple Moving Average Crossover) strategy to generate CALL/PUT signals.

## Architecture
- **OTCharts API Lite:** Fetches live tick data.
- **S3 Analyzer:** Detects moving average crossovers.
- **Telegram Bot:** Posts formatted signals to the channel.
- **Railway:** Hosts the bot 24/7.

## Strategy
- **Timeframe:** M1 (1-minute trades)
- **Signal Frequency:** Every 5 minutes (scans at :52 seconds)
- **Martingale (MTG):** 1 silent recovery step. If Step 1 loses, the bot waits for the M1 candle result. If Step 1 wins, cycle ends. If Step 1 loses, the bot silently tracks Step 2. No second signal is sent; the initial signal contains the MTG instructions.

## Environment Variables (Railway)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHANNEL_ID`
- `OTCHARTS_API_KEY`
