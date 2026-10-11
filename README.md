# DunyaBazaar OTC Signals Bot

An automated M1 signal bot for the **DunyaBazaar** Telegram channel. It reads live OTC price data from OTCharts, applies a 7-indicator vote-based confluence analyzer, and posts formatted signals to a Telegram channel — no MTG, no fake promises, no hidden magic.

---

## ⚠️ Disclaimer

This bot is for **educational and research purposes only**. It is not financial advice.

- OTC binary options are synthetic markets where the broker controls the price feed.
- At 85% payout you need **~54.1%** win rate just to break even. At 70% payout you need **~58.8%**.
- Most retail binary options traders lose money over time.
- Never risk more than **1–2%** of your account per trade.
- Past performance does not guarantee future results.

Use only on **demo accounts** until you have at least 100 tracked trades confirming a real edge.

---

## 🎯 What It Does

- Tracks **12 exotic OTC pairs** on the Quotex book via OTCharts
- Analyzes **7 indicators** per candle (EMA trend, EMA cross, RSI, Bollinger, Stochastic, MACD, candle patterns)
- Fires a signal only when **at least 4 of 7** agree on direction
- Sends **one best signal per scan cycle** to the Telegram channel
- Enforces a **3-minute cooldown** after every signal
- Posts results with **entry and close prices** for transparency
- Sends **admin feedback** so the operator can monitor every action in real time

---

## 🧠 Analyzer Logic

Each candle, 7 independent indicators cast a vote:

| # | Indicator | Vote |
|---|---|---|
| 1 | EMA 9 vs EMA 21 | +1 (CALL) / −1 (PUT) |
| 2 | EMA 5/20 fresh cross | ±1 only on cross candle |
| 3 | RSI(7) below 25 or above 75 | +1 / −1 |
| 4 | Bollinger(20,2) band touch | +1 / −1 |
| 5 | Stochastic(5,3,3) cross in extreme zone | +1 / −1 |
| 6 | MACD(12,26,9) histogram zero-cross | +1 / −1 |
| 7 | Candle pattern (engulfing / pin) | +1 / −1 |

**Score = sum of votes.**  
**Signal fires when |score| ≥ `MIN_SCORE`** (default: 4).

---

## 📂 File Structure
