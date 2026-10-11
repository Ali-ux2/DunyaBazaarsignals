import io
from PIL import Image, ImageDraw

def generate_candle_chart(bars, pair_name):
    if not bars:
        return None
    width, height = 600, 300
    img = Image.new('RGB', (width, height), color='#1e1e1e')
    draw = ImageDraw.Draw(img)

    opens = [bar.open for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    closes = [bar.close for bar in bars]

    min_price = min(lows)
    max_price = max(highs)
    price_range = max_price - min_price if max_price > min_price else 1

    padding = 20
    chart_width = width - padding * 2
    chart_height = height - padding * 2
    candle_width = chart_width / len(bars)

    for i in range(len(bars)):
        color = '#00ff00' if closes[i] >= opens[i] else '#ff0000'
        x_center = padding + (i * candle_width) + (candle_width / 2)
        y_high = padding + chart_height - ((highs[i] - min_price) / price_range) * chart_height
        y_low = padding + chart_height - ((lows[i] - min_price) / price_range) * chart_height
        y_open = padding + chart_height - ((opens[i] - min_price) / price_range) * chart_height
        y_close = padding + chart_height - ((closes[i] - min_price) / price_range) * chart_height

        draw.line([(x_center, y_high), (x_center, y_low)], fill=color, width=2)
        body_width = candle_width * 0.6
        x0 = x_center - body_width / 2
        x1 = x_center + body_width / 2
        y_top = min(y_open, y_close)
        y_bottom = max(y_open, y_close)
        if y_top == y_bottom:
            draw.line([(x0, y_top), (x1, y_top)], fill=color, width=2)
        else:
            draw.rectangle([x0, y_top, x1, y_bottom], fill=color, outline=color)

    display_name = f"{pair_name.upper().replace('_OTC', '')}-OTC"
    draw.text((15, 15), display_name, fill='white')

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf