"""Channel styling: captions + charts (matplotlib, headless). Change styles here only."""
from __future__ import annotations
import io, re
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import pandas as pd

BRAND_FOOTER = "🌍 DunyaBazaar"
CONTACT = "CONTACT : @DBSdesk"

def display_name(symbol: str) -> str:
    """'USDPHP_otc' -> 'USDPHP'; 'BRLUSD_otc' -> 'BRLUSD' (shows the instrument actually traded)."""
    return re.sub(r"[-_ ]?otc$", "", symbol, flags=re.I).replace("/", "")

def result_caption(res: str, symbol: str, direction: str, entry: float | None = None, close: float | None = None) -> str:
    icon = {"WIN": "✅", "LOSS": "❌", "TIE": "⚪"}.get(res, "•")
    lines = [f"{icon} <b>{display_name(symbol)}</b>", f"Result: <b>{res}</b>"]
    if entry is not None and close is not None: lines.append(f"Entry: {entry:.5f} | Close: {close:.5f}")
    lines += ["—", BRAND_FOOTER]
    return "\n".join(lines)

def signal_caption(symbol: str, direction: str, entry_at, expiry_min: int, playbook: tuple | None, precheck_second: int,
                   wait_line: str | None = None) -> str:
    arrow = "🟢 CALL ⬆️" if direction == "CALL" else "🔴 PUT ⬇️"
    lines = ["🚀 Signal ready", "------ { DUNYABAZAAR } ------", f"<b>{display_name(symbol)}</b>  {arrow}",
             f"⏰ Entry {entry_at:%H:%M}:00 UTC · ⌛ {expiry_min} min"]
    if playbook:
        lines += [f"▶️ {playbook[0]}", playbook[1], wait_line or f"Wait for ✅ CONFIRMED at :{precheck_second}"]
    lines += ["------------------------------", CONTACT]
    return "\n".join(lines)

def render_chart(df: pd.DataFrame, symbol: str, entry_at=None, expiry_bars: int = 1, n: int = 30) -> bytes:
    """Black background, plain green/red candles, small 'PAIR-OTC' label top-left, subtle entry marker."""
    end = entry_at + pd.Timedelta(minutes=expiry_bars - 1) if entry_at is not None else df.index[-1]
    d = df[df.index <= end].iloc[-n:]
    bg, up, dn = "#000000", "#22c55e", "#ef4444"
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110); fig.patch.set_facecolor(bg); ax.set_facecolor(bg)
    rng = (d.high.max() - d.low.min()) or 1e-9; w = 0.62
    for k, (t, r) in enumerate(d.iterrows()):
        col = up if r.close >= r.open else dn
        ax.vlines(k, r.low, r.high, color=col, lw=1)
        ax.add_patch(Rectangle((k - w/2, min(r.open, r.close)), w, max(abs(r.close - r.open), rng*0.002), color=col))
        if entry_at is not None and t == entry_at:
            ax.hlines(r.open, k - 3, k + expiry_bars - 0.5, color="#9ca3af", lw=0.8, ls=(0, (3, 3)))
            ax.vlines(k - 0.5, d.low.min(), d.high.max(), color="#4b5563", lw=0.6, ls=(0, (2, 3)))
    ax.text(0.01, 0.97, f"{display_name(symbol)}-OTC", transform=ax.transAxes, color="#d1d5db", fontsize=9, va="top")
    ax.set_xlim(-1, len(d)); ax.set_xticks([]); ax.yaxis.tick_right()
    ax.tick_params(colors="#6b7280", labelsize=7, length=0)
    for s in ax.spines.values(): s.set_visible(False)
    fig.tight_layout(); buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor=bg); plt.close(fig)
    return buf.getvalue()

def render_result_chart(df, entry_at, direction, symbol, res, expiry_bars=1, n=30):
    return render_chart(df, symbol, entry_at, expiry_bars, n)
