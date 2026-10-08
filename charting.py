import matplotlib
matplotlib.use('Agg')  # Required for running on Railway without a display
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
from io import BytesIO
from datetime import datetime

def generate_candle_chart(bars, pair_name):
    """
    Generates a dark-themed candlestick chart and returns a BytesIO object.
    """
    if not bars:
        return None

    # Extract data
    times = [bar.time for bar in bars]
    opens = [bar.open for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    closes = [bar.close for bar in bars]

    # Create figure and axis
    fig, ax = plt.subplots(figsize=(6, 3), facecolor='#1e1e1e')
    ax.set_facecolor('#1e1e1e')

    # Plot wicks and bodies
    for i in range(len(bars)):
        color = '#00ff00' if closes[i] >= opens[i] else '#ff0000'
        
        # Draw wick
        ax.plot([i, i], [lows[i], highs[i]], color=color, linewidth=1)
        
        # Draw body
        rect = patches.Rectangle(
            (i - 0.3, min(opens[i], closes[i])), 
            0.6, 
            abs(closes[i] - opens[i]), 
            facecolor=color, 
            edgecolor=color
        )
        ax.add_patch(rect)

    # Set axis limits
    ax.set_xlim(-1, len(bars))
    ax.set_ylim(min(lows) - (max(highs)-min(lows))*0.1, max(highs) + (max(highs)-min(lows))*0.1)

    # Remove grid and ticks for a clean look
    ax.grid(False)
    ax.set_xticks([])
    ax.set_yticks([])
    
    # Remove borders
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Add pair name text on the chart
    ax.text(0.01, 0.95, f"{pair_name.upper().replace('_OTC', '')}-OTC", 
            transform=ax.transAxes, color='white', fontsize=12, fontweight='bold')

    # Save to BytesIO
    buf = BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png', facecolor=fig.get_facecolor(), bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    
    return buf
