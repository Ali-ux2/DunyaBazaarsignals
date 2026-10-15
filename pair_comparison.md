# Quotex OTC pairs for compounding sessions: comparison
Prepared 14 Oct 2026. Desk research only: no backtest, no live API key.
Labels: **[E]** = evidence from a public source (linked) · **[I]** = inference · **[U]** = unverified · **[P]** = promotional/anecdotal claim.

## 1. Key findings
1. **USDBRL may not be readable on OTCharts' Quotex book.** OTCharts' public symbol cross-reference lists `USDBRL_otc` on
   Pocket Option and `USDBRL-OTC` on IQ Option, with **a dash under Quotex** [E] ([symbols.md](https://otcharts.com/symbols.md)).
   A site using the Quotex name (quotex.me, not confirmed as the official domain [U]) lists "USD/BRL OTC 92%"
   ([quotex.me/assets](https://quotex.me/assets/)). So the pair may exist on Quotex while OTCharts doesn't carry it, and the bot would log it as
   "not in quotex catalogue". **Check with `/v1/symbols?venue=quotex` before using USDBRL for compounding.**
2. The other 11 pairs **are** listed on OTCharts' Quotex book [E]: USDINR, USDMXN, USDARS, USDPKR, USDBDT, USDCOP, USDPHP,
   USDIDR, USDEGP, USDNGN, USDDZD (all `_otc`).
3. **Payout is the biggest measurable differentiator.** A snapshot from OTCharts' live payout table, read **07:33 UTC 14 Oct 2026** [E]
   ([payouts.md](https://otcharts.com/payouts.md)). It moves during the day; this is a single snapshot:

| Quotex pair | Payout | Breakeven | | Quotex comparison | Payout |
|---|---|---|---|---|---|
| USDINR_otc | 92% | 52.1% | | EURUSD_otc | 93% |
| USDIDR_otc | 92% | 52.1% | | GBPUSD_otc | 93% |
| USDCOP_otc | 92% | 52.1% | | USDJPY_otc | 93% |
| USDARS_otc | 88% | 53.2% | | AUDCAD_otc | 93% |
| USDMXN_otc | 87% | 53.5% | | XAUUSD_otc (gold) | 93% |
| USDPHP_otc | 77% | 56.5% | | BTC/USD (Quotex) | 74% |
| USDBDT_otc | 77% | 56.5% | | | |
| USDPKR / USDEGP / USDNGN / USDDZD | not in that table [U] | — | | | |
| USDBRL | not on OTCharts' Quotex book (see 1) | — | | | |

4. There is **no credible public evidence** that any particular pair suits streaks or compounding. Everything found that ranks
   pairs is promotional. For example, a GitHub README claims "100.00%" win rates on USDCOP/USDBDT/USDPKR_otc [P]
   ([usmanch96/QxbrokerFutures](https://github.com/usmanch96/QxbrokerFutures/blob/main/README.md)). Win rates like that are not credible. Telegram
   "compounding" channels post USDBRL/USDINR calls alongside "recover your losses" referral links [P]
   ([telemetr.io @viccmac](https://telemetr.io/en/channels/1480274942-viccmac)). That these pairs are popular in such channels is
   anecdotal; it says nothing about performance.
5. On how OTC behaves: OTCharts measured 38 OTC instruments against their real-market namesakes and found none tracked them [E]
   ([api.html](https://otcharts.com/api.html)). A broker-industry blog likewise advises journalling each OTC symbol as its own instrument [E, opinion]
   ([SignalBots](https://signalbots.ai/blog/quotex-otc-market-signals)). Real-world facts about the INR, BRL and other currencies (central banks,
   pegs, news) **do not carry over** to these broker-generated series [I].
6. Quotex closes the OTC versions of some *major* pairs on weekdays (OTCharts names EUR/USD, EUR/CAD, AUD/JPY) [E] ([api.html](https://otcharts.com/api.html)).
   So majors may be **unavailable on weekdays**. The exotic USD pairs are quoted 24/7 according to OTCharts [E], but
   whether each one is open at a given moment has to be read from the `open` flag [U].

## 2. The 12 pairs against our compounding criteria
Criteria: payout ≥ 80% (ideally ≥ 85%), trend-regime frequency, moderate ATR, few feed gaps, available on weekdays and on
OTCharts' Quotex book. **Trend frequency, ATR and gaps are unmeasured for all 12 [U]** and need the bot's own data (section 4).

| Pair | Status | Reason |
|---|---|---|
| USDINR | **Strong candidate** | on OTCharts Quotex [E]; 92% payout in the snapshot [E]; already a compounding pair; behaviour unmeasured [U] |
| USDIDR | **Strong candidate** | on OTCharts Quotex [E]; 92% [E]; not currently used, so worth testing [I] |
| USDCOP | **Strong candidate** | on OTCharts Quotex [E]; 92% [E] (quotex.me shows 90% [U]) |
| USDMXN | **Good** | on OTCharts Quotex [E]; 87% [E] (above the 85% preference, below the top three); already a compounding pair |
| USDARS | **Good / middling** | on OTCharts Quotex [E]; 88% [E] (quotex.me shows 81% [U], so payout may swing; watch it) |
| USDBRL | **Blocked until checked** | not in OTCharts' Quotex catalogue [E]; payout claim 92% only on quotex.me [U]. If `/v1/symbols` doesn't list it, drop it from the bot and from compounding |
| USDPKR | **Middling (unknown)** | on OTCharts Quotex [E]; payout not published in the snapshot [U] |
| USDEGP | **Middling (unknown)** | as USDPKR. EGP has historically been a managed currency, but that doesn't apply to an OTC series [I] |
| USDNGN | **Middling (unknown)** | as USDPKR; one site reports NGN/USD 92% [U] |
| USDDZD | **Middling (unknown)** | as USDPKR |
| USDPHP | **Weak: drop for compounding** | 77% payout [E] means 56.5% breakeven; fails the session minimum (80%); keep for normal signals only if its own stats beat 56.5% |
| USDBDT | **Weak: drop for compounding** | 77% [E]; same as USDPHP |

## 3. Would the commonly recommended pairs help?
- **EURUSD / GBPUSD / USDJPY / AUDCAD _otc:** all on OTCharts' Quotex book [E] and paying **93%** in the snapshot [E], so breakeven is 51.8%.
  The lowest breakeven of anything checked. Quotex closes some major OTC pairs on weekdays [E], so availability is the catch.
  **Worth adding as compounding candidates** for the times they are open [I]; the bot's open-pair check already handles closures.
- **Gold (XAUUSD_otc):** on Quotex [E], 93% [E]. A reasonable candidate; volatility behaviour unmeasured [U].
- **BTC OTC on Quotex:** 74% [E], below the 77% floor. **Not suitable** at that payout.
- A higher payout **lowers the win rate you need; it does not create an edge** (OTCharts says the same [E]). Add these pairs to the
  bot's watchlist and let their measured stats decide.
- Budget note: each extra pair costs about 1 request/minute. On Lite (7,000/day), 12 pairs ≈ 9.5 h/day; 16 pairs ≈ 7 h/day [I, arithmetic].

## 4. How to rank pairs with your own data (recommended)
1. **At startup and every 5 minutes**, read `/v1/symbols?venue=quotex`: keep only pairs that exist, are `open: true`, and pay ≥ 80% (≥ 85% preferred).
   The bot already reads these fields; it logs pairs that are missing.
2. **Collect 2–4 weeks of normal signals in `log` mode** (precheck and overlap logged). For each pair record the share of minutes in trend regime,
   the median ATR percentile, gap count (`gaps.runs`) and payout spread over the day.
3. **Rank by edge over breakeven, not raw win rate:** score = (rolling win rate − 1/(1+payout)), using the lower end of the 95% confidence interval,
   with **at least 100 signals per pair**. Use the trades.csv / pair_stats.json the bot writes.
4. **Compounding eligibility:** lower confidence bound ≥ breakeven + 5 points, trend regime ≥ ~30% of session minutes, gaps rare,
   payout stable ≥ 85% [I: suggested thresholds]. Take the top 2–3 at most.
5. **Re-rank weekly.** OTC behaviour and payouts change. A pair whose rolling rate falls below breakeven is auto-paused already;
   drop it from compounding immediately.
6. Watch for streaks clustering more or less than p^N per pair (see compounding_strategy.md §9).

## 5. Bottom line
- **Keep:** USDINR and USDMXN. **Test as additions:** USDIDR and USDCOP (high payouts in the snapshot).
- **Check USDBRL now.** It doesn't appear on OTCharts' Quotex book, so the bot may not be able to read it at all.
- **Drop for compounding:** USDPHP and USDBDT (77% payout).
- **Unknown:** USDPKR/EGP/NGN/DZD. Let the payout reads and recorded stats decide.
- **Consider adding** EURUSD/GBPUSD/USDJPY/AUDCAD/XAUUSD _otc (93%) for the hours they're open. Don't add BTC OTC (74%).
- Nothing here measures predictability. Rank pairs on your own recorded results, not on popularity.
