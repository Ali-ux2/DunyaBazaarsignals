#!/usr/bin/env python3
"""
analyzer.py - 1-minute indicator confluence analyzer + backtester.
(Merged educational version)

Usage:
    python analyzer.py candles.csv [--min-score 3] [--payout 0.85]
                                   [--stake 0.05] [--balance 50]
    python analyzer.py --demo

CSV columns: time,open,high,low,close  (1-minute candles, oldest first).

DISCLAIMER (READ THIS):
    This script is for educational and research purposes ONLY.
    It is NOT financial advice. It is NOT a "sure shot" or guaranteed system.

    Binary options (including Quotex) have a structural disadvantage:
      - At 85% payout you need ~54.1% win rate just to break even.
      - At 70% payout you need ~58.8%.
      - 1-minute charts are extremely noisy; most short-term edges vanish
        after costs, slippage, and regime changes.
      - Most retail binary options traders lose money over time.
      - Martingale / aggressive compounding is one of the fastest ways
        to blow an account. Do not do it.

    Use only on demo accounts, with strict risk management
    (1-2% risk per trade, daily loss limits). Past or simulated results
    do not guarantee future performance. Trade only money you can lose.
"""

import argparse
import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------
# Indicator primitives
# ----------------------------------------------------------------------------

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 7) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


# ----------------------------------------------------------------------------
# Confluence votes
# ----------------------------------------------------------------------------

VOTE_COLS = [
    "ema_trend",   # EMA 9 vs 21 (trend filter)
    "ema_cross",   # EMA 5/20 fresh crossover (momentum trigger)
    "rsi",         # RSI(7) extremes (mean reversion)
    "bollinger",   # Bollinger(20,2) touch
    "stoch",       # Stochastic(5,3,3) cross in extreme zone
    "macd",        # MACD(12,26,9) histogram zero-cross
    "candle",      # Engulfing / pin bar
]

VOTE_LABELS = {
    "ema_trend": "EMA 9/21 trend",
    "ema_cross": "EMA 5/20 fresh crossover",
    "rsi":       "RSI(7) extreme",
    "bollinger": "Bollinger(20,2) touch",
    "stoch":     "Stochastic cross in extreme zone",
    "macd":      "MACD histogram zero-cross",
    "candle":    "Candle pattern (engulfing / pin)",
}


def build_votes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Each indicator votes +1 (CALL), -1 (PUT) or 0 (no signal).
    Returns a DataFrame with one column per indicator plus 'score'.
    """
    c, o, h, l = df["close"], df["open"], df["high"], df["low"]
    v = pd.DataFrame(index=df.index)

    # 1) Trend filter: EMA 9 vs 21
    v["ema_trend"] = np.where(ema(c, 9) > ema(c, 21), 1, -1)

    # 2) EMA 5/20 fresh crossover (only fires on the cross candle)
    e5, e20 = ema(c, 5), ema(c, 20)
    cross_up = (e5 > e20) & (e5.shift() <= e20.shift())
    cross_dn = (e5 < e20) & (e5.shift() >= e20.shift())
    v["ema_cross"] = np.select([cross_up, cross_dn], [1, -1], 0)

    # 3) RSI(7) extremes (mean reversion)
    r = rsi(c, 7)
    v["rsi"] = np.select([r < 25, r > 75], [1, -1], 0)

    # 4) Bollinger(20,2): close outside band
    ma, sd = c.rolling(20).mean(), c.rolling(20).std()
    v["bollinger"] = np.select([c < ma - 2 * sd, c > ma + 2 * sd], [1, -1], 0)

    # 5) Stochastic(5,3,3) cross in extreme zones
    hh, ll = h.rolling(5).max(), l.rolling(5).min()
    k = (100 * (c - ll) / (hh - ll).replace(0, np.nan)).rolling(3).mean()
    d = k.rolling(3).mean()
    stoch_up = (k > d) & (k.shift() <= d.shift())
    stoch_dn = (k < d) & (k.shift() >= d.shift())
    v["stoch"] = np.select([stoch_up & (k < 30), stoch_dn & (k > 70)], [1, -1], 0)

    # 6) MACD(12,26,9) histogram zero-cross
    macd_line = ema(c, 12) - ema(c, 26)
    hist = macd_line - ema(macd_line, 9)
    v["macd"] = np.select(
        [(hist > 0) & (hist.shift() <= 0), (hist < 0) & (hist.shift() >= 0)],
        [1, -1], 0)

    # 7) Candle patterns: engulfing + pin bars
    body = (c - o).abs()
    up_w = h - np.maximum(c, o)
    lo_w = np.minimum(c, o) - l
    bull_eng = (c.shift() < o.shift()) & (c > o) & (c > o.shift()) & (o < c.shift())
    bear_eng = (c.shift() > o.shift()) & (c < o) & (c < o.shift()) & (o > c.shift())
    hammer = (lo_w > 2 * body) & (up_w < body)
    star = (up_w > 2 * body) & (lo_w < body)
    v["candle"] = np.select([bull_eng | hammer, bear_eng | star], [1, -1], 0)

    v["score"] = v[VOTE_COLS].sum(axis=1)
    return v


# ----------------------------------------------------------------------------
# Backtester
# ----------------------------------------------------------------------------

def backtest(df, score, min_score, payout, stake_pct, balance):
    """1-minute expiry = next candle close. Ties (equal close) are skipped."""
    sig = np.where(score >= min_score, 1,
                   np.where(score <= -min_score, -1, 0))
    nxt = df["close"].shift(-1)
    up, dn = nxt > df["close"], nxt < df["close"]
    win = ((sig == 1) & up) | ((sig == -1) & dn)
    tie = (sig != 0) & (nxt == df["close"])
    trade = (sig != 0) & nxt.notna()

    wins = int((win & trade).sum())
    losses = int((trade & ~win & ~tie).sum())
    n = wins + losses
    wr = wins / n if n else 0.0
    breakeven = 1 / (1 + payout)

    bal, peak, max_dd = balance, balance, 0.0
    for w, t, tr in zip(win.values, tie.values, trade.values):
        if not tr or t:
            continue
        stake = bal * stake_pct
        bal += stake * payout if w else -stake
        peak = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak)

    return dict(trades=n, wins=wins, win_rate=wr, breakeven=breakeven,
                final_balance=bal, max_drawdown=max_dd)


# ----------------------------------------------------------------------------
# Latest-candle explanation
# ----------------------------------------------------------------------------

def explain_latest(votes: pd.DataFrame, min_score: int) -> dict:
    """Turn the latest row of votes into a human-readable signal."""
    last = votes.iloc[-1]
    score = int(last["score"])

    call_reasons, put_reasons = [], []
    for col in VOTE_COLS:
        val = int(last[col])
        if val > 0:
            call_reasons.append(VOTE_LABELS[col])
        elif val < 0:
            put_reasons.append(VOTE_LABELS[col])

    if score >= min_score:
        signal = "CALL (UP)"
    elif score <= -min_score:
        signal = "PUT (DOWN)"
    else:
        signal = "NO TRADE"

    confidence = min(abs(score) / len(VOTE_COLS), 1.0)

    return {
        "signal": signal,
        "score": score,
        "confidence": round(confidence, 2),
        "call_reasons": call_reasons,
        "put_reasons": put_reasons,
        "votes": {col: int(last[col]) for col in VOTE_COLS},
    }


def format_status(latest: dict, min_score: int) -> str:
    """
    Always return a human-readable status line.
    Never silent: even when there is no trade, say so and why.
    """
    s = latest["score"]
    if latest["signal"] != "NO TRADE":
        return (f"{latest['signal']}  (confidence {latest['confidence']:.0%})")

    # No trade -> explain why
    if s == 0:
        return "NO SETUP — indicators are balanced / flat"
    if abs(s) == 1:
        return f"NO SETUP — only 1 vote (need {min_score})"
    if abs(s) == 2:
        return f"NO SETUP — only 2 votes (need {min_score})"
    # abs(s) high but thresholds not aligned (rare with symmetric rule)
    return f"NO SETUP — conflicting votes (score {s:+d}, need ±{min_score})"


def format_warm(latest: dict, min_score: int) -> str:
    """Optional hint: are we one vote away from a setup?"""
    s = latest["score"]
    if latest["signal"] != "NO TRADE":
        return ""
    if abs(s) == min_score - 1 and min_score >= 2:
        return f" (close — 1 more vote gives {min_score})"
    return ""


# ----------------------------------------------------------------------------
# Demo data
# ----------------------------------------------------------------------------

def make_demo_df(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLC just so the script runs without a CSV."""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.0006, n) + 0.00002
    close = 1.1000 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[close[0]], close[:-1]])
    span = np.abs(rng.normal(0, 0.0004, n))
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

DISCLAIMER = (
    "=" * 66 + "\n"
    "Educational / research use only. NOT financial advice.\n"
    "Binary options: ~54% wins needed at 85% payout just to break even;\n"
    "~59% at 70% payout. Most retail traders lose. No indicator combo is a\n"
    "'sure shot.' Backtests do not guarantee future results. Demo only.\n"
    + "=" * 66
)


def main():
    ap = argparse.ArgumentParser(
        description="1-minute confluence analyzer + backtester (educational).")
    ap.add_argument("csv", nargs="?", help="OHLC CSV (time,open,high,low,close)")
    ap.add_argument("--demo", action="store_true",
                    help="Run on synthetic data (no CSV required)")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--payout", type=float, default=0.85)
    ap.add_argument("--stake", type=float, default=0.05)
    ap.add_argument("--balance", type=float, default=50)
    ap.add_argument("--brief", action="store_true",
                    help="Print only the one-line status (bot-friendly)")
    a = ap.parse_args()

    # --- Load data --------------------------------------------------------
    if a.demo or not a.csv:
        if not a.csv and not a.demo:
            if not a.brief:
                print("[info] No CSV given; falling back to --demo data.\n")
        df = make_demo_df()
    else:
        df = pd.read_csv(a.csv)
        df.columns = [x.lower().strip() for x in df.columns]

    required = {"open", "high", "low", "close"}
    if not required.issubset(df.columns):
        raise SystemExit(f"CSV must contain columns {sorted(required)}; "
                         f"found {list(df.columns)}")
    if len(df) < 50:
        raise SystemExit(f"Need at least ~50 candles for stable indicators "
                         f"(got {len(df)}).")

    votes = build_votes(df)
    latest = explain_latest(votes, a.min_score)

    # --- Brief / bot-friendly output --------------------------------------
    if a.brief:
        s = latest["score"]
        if latest["signal"] == "NO TRADE":
            line = (f"No setup | score {s:+d} (need ±{a.min_score}) "
                    f"| votes: {latest['votes']}")
        else:
            line = (f"{latest['signal']} | score {s:+d} "
                    f"| confidence {latest['confidence']:.0%} "
                    f"| votes: {latest['votes']}")
        print(line)
        return

    # --- Full output ------------------------------------------------------
    print(DISCLAIMER + "\n")

    print(f"Candles: {len(df)}  |  payout {a.payout:.0%}  |  "
          f"break-even win rate {1 / (1 + a.payout):.1%}\n")

    print("Backtest by confluence threshold:")
    print(f"{'min':>3} {'trades':>7} {'win%':>7} {'end bal':>10} {'maxDD':>7}")
    for ms in range(2, min(7, len(VOTE_COLS) + 1)):
        r = backtest(df, votes["score"], ms, a.payout, a.stake, a.balance)
        print(f"{ms:>3} {r['trades']:>7} {r['win_rate']:>7.1%} "
              f"{r['final_balance']:>10.2f} {r['max_drawdown']:>7.1%}")
    print("\nIf win% sits below break-even, the strategy loses money.")
    print("Backtests on past data do not guarantee future results.\n")

    # --- Latest candle: ALWAYS print a status ----------------------------
    s = latest["score"]
    status = format_status(latest, a.min_score)
    warm = format_warm(latest, a.min_score)

    print("Latest candle votes:",
          {k: v for k, v in latest["votes"].items()})
    print(f"Score {s:+d}  ->  {status}{warm}")

    if latest["call_reasons"]:
        print("  CALL votes:")
        for r in latest["call_reasons"]:
            print(f"    • {r}")
    if latest["put_reasons"]:
        print("  PUT votes:")
        for r in latest["put_reasons"]:
            print(f"    • {r}")

    print(f"  Threshold: |score| >= {a.min_score} required.")
    if latest["signal"] == "NO TRADE":
        print("  No trade is intentional and often better than forcing an entry.")
    print("\nReminder: educational only. Demo account. Strict risk management.")


if __name__ == "__main__":
    main()