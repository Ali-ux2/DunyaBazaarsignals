# Compounding Session: Dunyabazaar Signals (@dunyabazaarsignals)
Quotex OTC · M1 · 1-minute expiry · **No martingale**: the stake never goes up after a loss. A loss ends the session.

> The math in this document assumes every trade wins with the same probability p and that trades are independent.
> No win rate for this strategy has been measured yet. All numbers below are illustrations, not results.

---

## 1. What a compounding session is
A short, separate run alongside the normal signals: **one pair, a few very strict setups in a row, each stake = previous stake + profit**
(or part of it). The session ends at the first loss, at the target step, or on a time or regime stop. Normal signals keep running
with their usual flat stake and are never mixed into a session's count.

## 2. When a session may start (all must be true)
1. **Regime = trend** on the chosen pair: ADX(14) ≥ 25 **and** efficiency ratio(10) ≥ 0.35. No sessions in range or unclear regimes.
   Continuation in an established trend is the only setup type used (reversal fades are excluded).
2. **ATR percentile between 30 and 90** (stricter than normal: no dead or spiking markets).
3. **Pair selection:** the top 1–2 pairs by rolling win rate (last 50 normal signals), and only if that rate is at least **breakeven + 5 points**
   at the current payout (e.g. ≥ 59% at 85%). Pairs that are auto-paused never qualify.
4. **Payout ≥ 85% preferred** (hard minimum 80% for sessions; 77% stays the floor for normal signals only).
   Lower payout = higher breakeven = sessions much less likely to pay.
5. Pair is `open` on Quotex, no feed gaps in the last 30 bars, data not stale.
6. The admin chose the session window (e.g. the first hour of your active session), or started it with a command.

## 3. Entry rules inside a session (stricter than normal signals)
| Check | Normal signal | Compounding step |
|---|---|---|
| Original 7-vote engine (EMA9/21 bounce, EMA5/20, MACD hist, RSI7, Stoch, candle body, S/R) | — (replaced by redesign) | **score ≥ 6/7, all non-zero votes in the same direction**, used only as a cross-check |
| Redesigned weighted score | ≥ 70% of max | **≥ 85% of max**, no opposing vote |
| Votes that must be present | any mix | **trend + momentum + candle** all agree; location shows "room to the target" |
| 0.08% chop filter around EMA21 | applied | applied **and** price must not have touched EMA21 in the last 3 candles |
| Closed-candle evaluation (:02) | yes | yes (never early52) |
| Overlap candle (close beyond previous green high / red low) | log | **require** (it suits trend continuation) |
| Pre-entry check at :57 | confirm | **confirm required**; any cancel = step skipped (session not ended, but counts toward the no-setup timer) |
| Distance to S/R target | room left | at least **1.5 ATR** of room before the opposing 20-bar level |
| Payout at entry | ≥ 77% | ≥ 80% (85%+ preferred) and must not drop more than 3 points from session start |

Why the original votes only cross-check: EMA9/21, EMA5/20 and MACD all measure the same trend, so "6/7" mostly repeats one fact.
Requiring them **and** the decorrelated engine helps only by rejecting setups where they disagree.

## 4. Session limits and stop conditions
- **One pair, one session at a time.** No new session for 15 minutes after one ends.
- **Target:** 5 steps by default. 6–8 only after the backtest checklist (section 9) shows it is justified.
- **Hard stops (end immediately):**
  1. First loss (no recovery trades, no martingale).
  2. Target step reached → bank and announce.
  3. No qualifying setup within **15 minutes** of the last step (setups go stale).
  4. Regime changes on the pair (ADX < 22 or ER < 0.30) or ATR percentile leaves 30–90.
  5. Payout falls below 80%, the pair closes, or a feed gap appears.
  6. Session running longer than **45 minutes** in total.
  7. Daily limit: at most 3 sessions per day; stop for the day after 2 failed sessions.
- A **tie** (close = entry) neither advances nor ends the session; the same stake carries to the next step.

## 5. Stake progression (start $10, payout 85%, exact)
### A. Full compounding: stake = previous stake + profit
| Step | Stake | Profit if win | Balance after win | Net profit if you stop here | Net if this step loses |
|---|---|---|---|---|---|
| 1 | $10.00 | $8.50 | $18.50 | $8.50 | $-10.00 |
| 2 | $18.50 | $15.72 | $34.23 | $24.23 | $-10.00 |
| 3 | $34.23 | $29.09 | $63.32 | $53.32 | $-10.00 |
| 4 | $63.32 | $53.82 | $117.14 | $107.14 | $-10.00 |
| 5 | $117.14 | $99.56 | $216.70 | $206.70 | $-10.00 |
| 6 | $216.70 | $184.19 | $400.89 | $390.89 | $-10.00 |
| 7 | $400.89 | $340.76 | $741.66 | $731.66 | $-10.00 |
| 8 | $741.66 | $630.41 | $1,372.06 | $1,362.06 | $-10.00 |

A loss at any step leaves you $10 down from the start, but it also **throws away everything the earlier steps made**:
a loss at step 6 gives back $206.70 of profit plus the $10, and a loss at step 8 gives back $731.66 plus the $10.

### B. Partial compounding: reinvest 50% of each profit, bank the other 50%
| Step | Stake | Profit if win | Banked after win | Net (bank+next stake−$10) after win | Net if this step loses |
|---|---|---|---|---|---|
| 1 | $10.00 | $8.50 | $4.25 | $8.50 | $-10.00 |
| 2 | $14.25 | $12.11 | $10.31 | $20.61 | $-5.75 |
| 3 | $20.31 | $17.26 | $18.94 | $37.87 | $0.31 |
| 4 | $28.94 | $24.60 | $31.23 | $62.47 | $8.94 |
| 5 | $41.23 | $35.05 | $48.76 | $97.52 | $21.23 |
| 6 | $58.76 | $49.95 | $73.73 | $147.46 | $38.76 |
| 7 | $83.73 | $71.17 | $109.32 | $218.63 | $63.73 |
| 8 | $119.32 | $101.42 | $160.03 | $320.05 | $99.32 |

From step 3 on, a loss still finishes the session in profit. The upside is lower ($320 vs $1,362 after 8 wins).

### C. Fixed fraction: 2% of a $500 bankroll, recalculated after every win
| Step | Stake (2% of bankroll) | Profit if win | Bankroll after win | Net vs $500 |
|---|---|---|---|---|
| 1 | $10.00 | $8.50 | $508.50 | $8.50 |
| 2 | $10.17 | $8.64 | $517.14 | $17.14 |
| 3 | $10.34 | $8.79 | $525.94 | $25.94 |
| 4 | $10.52 | $8.94 | $534.88 | $34.88 |
| 5 | $10.70 | $9.09 | $543.97 | $43.97 |
| 6 | $10.88 | $9.25 | $553.22 | $53.22 |
| 7 | $11.06 | $9.40 | $562.62 | $62.62 |
| 8 | $11.25 | $9.56 | $572.19 | $72.19 |

Lowest variance. A loss costs about 2% of bankroll, never the session's whole profit.

### D. Recommended ladder: compound 3, then bank
Full compounding for steps 1–3 (→ $63.32). Then **bank $43.32 and keep only $20 in play** for steps 4–6, compounding that $20.

| Step | Stake | Balance in play after win | Net if you stop after this win | Net if this step loses |
|---|---|---|---|---|
| 1 | $10.00 | $18.50 | +$8.50 | −$10.00 |
| 2 | $18.50 | $34.23 | +$24.23 | −$10.00 |
| 3 | $34.23 | $63.32 | +$53.32 | −$10.00 |
| 4 | $20.00 | $37.00 | +$70.32 | **+$33.32** |
| 5 | $37.00 | $68.45 | +$101.77 | **+$33.32** |
| 6 | $68.45 | $126.63 | +$159.95 | **+$33.32** |

Once step 3 is won, the session can no longer end in a loss. That is the main protection against ruin.

## 6. The math
**Probability of N wins in a row = p^N** (with independent trades and constant p; in reality p varies, and some sources of edge
cluster together, which can cut either way).

| p (win rate) | N=5 | N=6 | N=7 | N=8 |
|---|---|---|---|---|
| 55% | 5.03%  (1 in 19.9) | 2.77%  (1 in 36.1) | 1.52%  (1 in 65.7) | 0.84%  (1 in 119.4) |
| 60% | 7.78%  (1 in 12.9) | 4.67%  (1 in 21.4) | 2.80%  (1 in 35.7) | 1.68%  (1 in 59.5) |
| 65% | 11.60%  (1 in 8.6) | 7.54%  (1 in 13.3) | 4.90%  (1 in 20.4) | 3.19%  (1 in 31.4) |
| 70% | 16.81%  (1 in 5.9) | 11.76%  (1 in 8.5) | 8.24%  (1 in 12.1) | 5.76%  (1 in 17.3) |
| 75% | 23.73%  (1 in 4.2) | 17.80%  (1 in 5.6) | 13.35%  (1 in 7.5) | 10.01%  (1 in 10.0) |
| 80% | 32.77%  (1 in 3.1) | 26.21%  (1 in 3.8) | 20.97%  (1 in 4.8) | 16.78%  (1 in 6.0) |


**Breakeven per trade** = 1/(1+r). At 85% payout that is **54.05%**; at 80%, 55.56%; at 77%, 56.50%.

**EV of one full-compounding session to N steps, stake S:** you end with S(1+r)^N with probability p^N, otherwise 0. So
EV = S·[(p(1+r))^N − 1]. It is **positive only if p > 1/(1+r)**, the same breakeven as a single trade.
Compounding does not create an edge: it **raises the edge (or the deficit) to the power N**. At p = 52% and 85% payout,
a 5-step session has EV = 10·(0.962^5 − 1) = −$1.76, worse than five flat $10 trades (−$1.90 total, but spread over five chances).

EV per $10 session (85% payout):
| p | N=5 | N=6 | N=7 | N=8 |
|---|---|---|---|---|
| 55% | +0.91 | +1.10 | +1.29 | +1.49 |
| 60% | +6.85 | +8.70 | +10.76 | +13.05 |
| 65% | +15.14 | +20.24 | +26.36 | +33.72 |
| 70% | +26.42 | +37.16 | +51.08 | +69.10 |
| 75% | +41.42 | +61.35 | +89.00 | +127.36 |
| 80% | +61.01 | +95.09 | +145.54 | +220.19 |


Stopping early vs going long: with p above breakeven, EV increases with N, but the chance of **any** profit falls fast
(p^N above). With p below breakeven, every extra step makes EV worse. Most sessions lose $10 and a few win big, so posting only
the winners is misleading (section 8).

**Realistic streak lengths:** nothing published shows a sustained 70%+ win rate on 1-minute OTC binaries, and OTC
prices are generated by the broker. At an honest 55–60%, a 5-step streak comes about 1 session in 13–20, and an 8-step streak about
1 in 60–120. **Treat 5 as the default target; 6–8 are long-shot sessions** that your own recorded data must justify.

## 7. Channel communication
- **Separate label:** every session message starts with `🔥 COMPOUND`. Normal signals keep their existing format.
- **Session start:** `🔥 COMPOUND SESSION START: USDINR OTC · trend regime · payout 87% · target 5 steps · stops at first loss · NO MTG`
- **Step signal:** `🔥 COMPOUND Step 3/5: USDINR CALL ⬆️ · entry 14:07:00 · 1 min · wait for ✅ at :57`
- **Confirm/cancel:** `✅ CONFIRMED Step 3/5` or `❌ CANCELLED Step 3/5, skip (session continues)`.
- **Step result:** `✅ Step 3/5 WIN` / `❌ Step 3/5 LOSS: session over`.
- **Session end:** `🏁 SESSION END: 3/5 wins, stopped at step 4 (loss)`, or `5/5 target reached`, or `stopped: regime changed`.
  Always state the reason and the final step reached.
- **Honest record:** a pinned weekly summary of **all** sessions: started, reached target, average steps, longest streak,
  and how often sessions reached step N compared with p^N. Never delete losing sessions or post only screenshots of wins.
- Never write "guaranteed", "sure shot", "100%", "no loss". Add a line to every start message: *"Not financial advice. High risk. Demo first."*

## 8. Risk warnings
- **A broken streak wipes all compounded profit** (section 5A). Only partial banking (5B/5D) protects earlier steps.
- **OTC prices are broker-generated**, not the real FX market. OTCharts measured 38 OTC instruments against their real-market
  namesakes and found none tracked them ([OTCharts API docs, "What this data is"](https://otcharts.com/api.html)). Patterns can
  change without notice, so an edge measured last month may be gone.
- **Counterparty/account risk:** regulators warn that some binary-options platforms have refused withdrawals and manipulated
  prices or expiry times ([CFTC/SEC Investor Alert: Binary Options and Fraud](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/fraudadv_binaryoptions.html);
  [CFTC Binary Options Fraud](https://www.cftc.gov/BinaryOptionsFraud/index.htm)). Quotex is an offshore broker; withdraw profits regularly.
- **Survivorship bias:** members see posted 8-step streaks and not the dozens of sessions that broke at step 1–2. Report every session.
- **Payout is also a risk factor:** a 5-point payout drop raises breakeven by about 1.5–2 points.
- Results are **never guaranteed**. Run everything on a **demo account** first, then real money at the smallest stake.

## 9. Backtest / forward-test checklist (before going live)
**Record for every session candidate** (including ones that would have been skipped):
date/time UTC, pair, payout at each step, regime values (ADX, ER, ATR percentile), original 7-vote score, redesigned score,
overlap result, :57 precheck verdict and reason, entry/exit price, result per step, step at which the session ended, stop reason.

**Minimum sample:** **300+ session candidates** (and 1,000+ individual qualifying setups) on **recorded Quotex OTC data**,
not real-market FX. Split chronologically: build on the first 60%, test unchanged on the last 40%.

**Metrics:**
- per-step win rate p (with a 95% confidence interval); the low end of the interval must be above breakeven for the payout used
- **streak distribution:** how many sessions ended at step 1, 2, …, 8, compared with p^N; clustered streaks (more than p^N) or
  more breaks than expected show that trades are not independent
- session success rate (reached target) vs p^N
- EV per session for each stake method (5A–5D), using actual payouts
- **max drawdown** and longest run of failed sessions (bankroll needed)
- results by pair, by hour and by payout bucket; disable any bucket below breakeven
- cancellation rate of the :57 precheck and the win rate of confirmed vs cancelled steps
- how much the extra strictness (score ≥ 85%, overlap required) changes p and the number of setups

Go live only if the forward test confirms the backtest. Re-check monthly, because OTC behaviour can change.
