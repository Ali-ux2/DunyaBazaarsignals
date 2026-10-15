# Candle-pattern family P

These patterns come from the user's pattern sheets. The code is in `patterns.py`; tests are in `test_patterns.py`; the backtest is `sim/pattern_bt.py` (not shipped in the bot).

## The sheets versus what the bot does
- The sheets' "sure shot" wording and their martingale (MTG) advice are **ignored**. No pattern is a sure shot, and the bot never martingales.
- Each signal is one 1-minute trade on the next candle. P13 is the exception: it uses a 2-minute expiry and is tracked separately.

## Definitions
Body sizes are measured against the median body of the 20 bars before the signal bar.

| Term | Meaning |
|---|---|
| normal body | 0.4–1.5× the median body |
| long wick (tail = lower, head = upper) | wick ≥ body, or wick ≥ 50% of the candle's range |
| marubozu | body ≥ 85% of range and ≥ 1.8× the median body |
| doji / small | body ≤ 0.3× the median body |
| not break | does not go beyond the reference candle's high/low (for the "body" rules: does not close beyond its open) |
| SNR | a swing high or low within 0.3 ATR; a swing is a 3-bar fractal in the last 40 bars |
| G / R | green / red candle |

## Rules (the prediction is for the next candle)

| Key | Rule | Signal | DC role |
|---|---|---|---|
| P1 | 8 candles of the same colour | same colour (9th) | M |
| P2 | G,G,R,R where the 4-bar high is at resistance (SNR) | PUT | S |
| P3 | G,R,G, and the last G has a long lower wick | PUT | S |
| P4 | R then G, where the R's tail is longer than the G's head | CALL | S |
| P5 | R,R both with long tails, the 2nd inside the 1st, then G | PUT | S |
| P6 | R,G,R | PUT | M |
| P6b | R,G,R,G (a separate pattern; not a martingale step) | CALL | M |
| P7 | R, normal G, normal G | PUT | M |
| P8 | G, normal R, normal R | CALL | M |
| P9 | G,G,G, then R with a long tail | CALL | S |
| P10 | R,R,R, then G with a long head | PUT | S |
| P11 | R,R,R, then a G inside the previous candle, on **30 s bars** built from stream ticks (skipped on non-stream pairs) | CALL | S |
| P13 | On **2-minute bars**, a level (high or low of the last 20 bars) is retested ≥ 3 times within 0.3 ATR, then a close breaks through it | reversal; **2-minute expiry** | S |
| P14 | Support line through the lows of the first 2 G after an R (rising or flat); closes stay above it; then an R closes below it | PUT; mirror (resistance from the first 2 R after a G, broken by a G) → CALL | S |
| P15 | V: a down leg (≥ 2 R) into a low 2–4 bars back, depth ≥ 1 ATR, then an up leg (≥ 2 G) that closes above the leg's starting level | PUT; inverted V → CALL | S |
| P16 | 3 or 4 normal G, then R | CALL | M |
| P17 | 3 or 4 normal R, then G | PUT | M |
| P18 | R marubozu, 3 normal G, then an R inside the marubozu's range | PUT | S |
| P19 | G marubozu, 3 normal R, then a G inside its range | CALL | S |
| P20 | 2 normal R, G, then an R whose low does not break the G's low | PUT | S |
| P21 | 2 normal G, R, then a G whose high does not break the R's high | CALL | S |
| P22 | 3–5 G, then an R with a smaller body than the last G that closes at or above that G's open | CALL | M |
| P23 | Mirror of P22 | PUT | M |
| P24 | Sideways (range ≤ 2 ATR) at an SNR: G, R, doji R, G, then an R smaller than the first R | CALL | S |
| P25 | doji R, normal R, then a long G (≥ 1.5× median) closing through a swing high | CALL | S |
| P26 | G,G,G with descending highs, then 2 small R not breaking the last G's low, at an SNR | CALL | S |

P12 is not on the sheets, so there is no P12.

## Pipeline
P patterns run through the bug pipeline. Each key is a named bug strategy (`*:P1`…).

- **History is mandatory.** Every P signal is checked against stored candle history with the same Wilson-LB ≥ breakeven + 3 pts rule. This applies even when `HIST_GATE=off`, and there is no bypass.
  - If the history check fails, the signal is paper-tracked only. It is counted in `/patterns`, not posted as an admin alert one by one.
  - P patterns are never used as Tier-2 fills.
  - P11 has no candle history (it needs ticks), so it is **always paper-tracked**.
  - P13 history uses the 2-minute outcome.
- **Admin only until proven.** A pattern reaches the bug group only after ≥ 20 forward trades at ≥ breakeven (`P_LIVE_MIN`). Forward trades include paper-tracked ones. The pattern also has to pass history, cooldown, blackouts and news.
  - Public/VIP routing follows the normal `/bugsignals` and `GROUP_FILTER=best` rules.
- **Double confirmation.** The pattern's own shape counts as one element:
  - **S** patterns (level, wick or range shape) count as *structure*, and still need an indicator *momentum* trigger (RSI/DeMarker/CCI/Stoch extreme, a 3-candle streak, or a strong body).
  - **M** patterns (streak or impulse sequences) count as *momentum*, and still need *structure* (Bollinger, swing S/R, ZigZag pivot or rejection wick).
- **Other controls.** Leaderboard (`/best`), auto-mute, the cooldown (3 losses → 30 min), session and news blackouts, the 77% payout floor and gap rejection all apply as for other bug strategies.
- **Commands.** `/patterns` (plus a menu button) shows each pattern's forward record, status and today's history-filtered count. `/patterns on|off` switches the family (env `PATTERNS`).

## Honest note
These are visual chart rules. In the backtest below, **no pattern beat breakeven by a statistically meaningful margin**:
- Most land at a 47–52% win rate, against a breakeven of 55.6% at an 80% payout and 56.5% at 77%.
- Several are mechanically common (P4, P6, P14, P15 fire on 5–10% of bars), which is what you would expect if they carry no edge.
- The backtest data is real interbank FX 1-minute candles (Yahoo, 7 days, 15 majors/crosses), **not** Quotex OTC quotes. OTC prices are broker-generated and may behave differently, which is exactly why every pattern must prove itself on stored OTC history and forward trades before it can post.

## Backtest (real FX 1m, 7 days, 15 pairs; breakeven 55.6% at 80% payout)
```
/workspace/fx_real_7d.csv: 15 pairs, 124245 bars · breakeven 55.6% (80% payout)
key        n     WR WilsonLB   vsBE |   n dc  WR dc  rule
P1       414  50.5%    46.5%  -5.1 |     94  51.1%  8 same colour → 9th same
P2      2995  48.7%    47.2%  -6.8 |   1612  47.6%  2G2R rejecting resistance → R
P3      4652  50.1%    48.9%  -5.4 |   2268  50.2%  G,R,G long lower wick → R
P4     10462  50.1%    49.3%  -5.5 |   7602  50.3%  R tail > next G head → G
P5       746  50.0%    47.0%  -5.6 |    254  50.0%  2R long tails, 2nd inside 1st, then G → R
P6      9291  47.8%    46.9%  -7.8 |   4341  47.5%  R,G,R → R
P6b     4594  50.2%    49.0%  -5.4 |   2713  49.8%  R,G,R,G → G
P7      2254  49.4%    47.7%  -6.2 |   1177  48.9%  R + 2 normal G → R
P8      2186  51.1%    49.3%  -4.5 |   1134  50.1%  G + 2 normal R → G
P9      2262  51.8%    50.1%  -3.7 |    923  52.2%  3G + R long tail → G
P10     1754  48.2%    46.3%  -7.3 |    200  48.0%  3R + G long head → R
P11        0      —        —     — |      0      —  3R + inside G → G (30s bars)
P13     1886  49.7%    47.8%  -5.9 |   1884  49.7%  repeated retests then breakout → reversal (2m)
P14     5759  50.4%    49.3%  -5.2 |   4308  49.7%  trendline from first 2G/2R broken → follow
P15     7519  50.6%    49.6%  -5.0 |   6248  50.9%  V / inverted V breaking the horizontal → opposite
P16      401  50.6%    46.5%  -4.9 |    180  55.0%  3-4 normal G + R → G
P17      384  47.9%    43.8%  -7.6 |    160  45.0%  3-4 normal R + G → R
P18        1   0.0%     0.0% -55.6 |      1   0.0%  R marubozu, 3 normal G, R inside → R
P19        2  50.0%    12.1%  -5.6 |      0      —  G marubozu, 3 normal R, G inside → G
P20      568  49.3%    45.9%  -6.3 |    200  53.5%  2 normal R, G, R not breaking G low → R
P21      651  50.7%    47.5%  -4.9 |    336  50.3%  2 normal G, R, G not breaking R high → G
P22     1808  53.5%    51.6%  -2.0 |    818  52.7%  3-5 G + smaller R not breaking → G
P23     1621  48.1%    46.0%  -7.5 |    565  45.1%  3-5 R + smaller G not breaking → R
P24       65  58.5%    48.3%  +2.9 |     24  58.3%  sideways at SNR: G,R,doji R,G,smaller R → G
P25       87  50.6%    41.9%  -5.0 |     79  49.4%  doji R, normal R, long G breaking SNR → G
P26        0      —        —     — |      0      —  3 descending G + 2 small R near SNR → G
```
