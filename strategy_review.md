# Strategy review: M1 OTC voting engine

## Breakeven
Win rate needed = 1 / (1 + payout).  77% → 56.5% · 80% → 55.6% · 85% → 54.1% · 90% → 52.6% · 95% → 51.3%.
`MIN_PAYOUT = 0.77`: never signal below that. Each pair must also keep a rolling win rate (last 50, after 30 trades)
above breakeven + 1% for the payout on offer right now, otherwise it is skipped.

## Main problems in the original engine
1. **Redundancy.** EMA9/21, EMA5/20 and the MACD histogram all measure the same trend. One trend move gives 3 votes,
   so "5/7 = 71%" is really one fact plus two weak votes. The confidence % was overstated.
2. **Contradictions.** S/R gave CALL near support (mean reversion), while the trend votes needed upward momentum. Price
   sits near support with EMA9>21 and RSI 55–70 only rarely, so that vote mostly sat at 0 or opposed the others.
3. **Scanning at :52.** The candle-body vote and every indicator were reading a candle 8 seconds before it closed. The
   last 8 seconds can flip the body. Now: evaluate the closed candle at :02 and enter at the next candle open.
4. **Lag.** All the EMA/MACD votes lag. On a 1-minute expiry, the trend is usually already priced in.
5. **OTC reality.** OTC prices are produced by the broker, not the market. OTCharts reports that none of 38 OTC
   instruments tracked their real-market namesake. Backtests on real FX do not carry over; record and test on the
   broker's own OTC feed.

## Keep / change / remove / add
| Item | Decision | Reason |
|---|---|---|
| EMA9/21 trend + slope + EMA9 touch | **Keep** as the single trend vote | best of the three trend measures; includes a pullback condition |
| EMA5/20 | **Remove** | duplicate of the trend vote |
| MACD histogram | **Remove** | duplicate of the trend vote and lags |
| RSI(7) 55–70 / 30–45 | **Merge** into one momentum vote | trend mode: continuation zone |
| Stoch(5,3,3) | **Merge** into momentum | trend: K vs D agrees; range: cross out of the extremes |
| Candle body ≥60%, wicks <30% | **Keep (trend mode)**, add a rejection-wick rule for range mode | candle shape should match the market type |
| 20-bar S/R | **Change** | trend mode: room left before the opposing level; range mode: fade the edge + Bollinger z ≥1.5; ignored if range < 3×ATR |
| 0.08% chop rule | **Keep** as an extra check | combined with the ADX/efficiency-ratio classifier |
| ADX(14) + Kaufman efficiency ratio | **Add** | picks trend mode (ADX≥25, ER≥0.35) or range mode (ADX≤18); unclear → no trade |
| ATR percentile (240 bars) | **Add** | skip dead markets (<20th percentile) and spikes (>95th) |
| Weighted score + any-opposing-vote veto | **Add** | trend: 2/1.5/1/1; range: location 2, momentum 1, candle 1; needs ≥70% of max |
| Feed-gap / closed-pair / stale-data rejection | **Add** | a gap merges several minutes into one fake breakout candle |
| Per-pair rolling win rate + payout gate | **Add** | automatically pauses pairs and payouts that cannot break even |
| Session filter (`SESSIONS_UTC`) | **Add (optional)** | trade only the sessions you choose |
| Trade log + backtest with 95% CI | **Add** | only recorded results show whether there is an edge |
| Overlap-candle confirmation | **Add as a filter** (`OVERLAP_MODE` log/require) | can only cut signals; keep it in `log` mode until the agreed-vs-not split proves it helps |
| Pre-entry confirmation at :57 | **Add** (`PRECHECK_MODE` confirm/log/off) | removes setups that fail or run out before entry; the backtest is approximate, so judge on recorded data |
| `SCAN_MODE` early52 | **Kept as an option** for comparison | the closed-candle mode is the default |
| Martingale | **None** | unchanged: no MTG |

## Evidence and honesty
- Intraday time-series momentum is documented on real markets over 30-minute windows (Li, Sakkas & Urquhart, *J. Financial
  Markets* 2022: https://ideas.repec.org/a/eee/finmar/v57y2022ics138641812100001x.html). That says nothing direct
  about 1-minute broker-generated OTC prices.
- No indicator set guarantees an edge. On a pure random walk this engine scores ~41–50% (expected, and a check against
  look-ahead bugs). Collect at least a few hundred live or recorded OTC trades per pair. Treat a pair as working only
  if the lower end of the 95% confidence interval is above breakeven.
