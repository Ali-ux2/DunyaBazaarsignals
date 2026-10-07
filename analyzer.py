from collections import deque
from config import PAIRS, SHORT_WINDOW, LONG_WINDOW

class S3Analyzer:
    def __init__(self):
        # Initialize price history and signal tracking for each pair
        self.price_history = {pair: deque(maxlen=LONG_WINDOW) for pair in PAIRS}
        self.last_signal = {pair: None for pair in PAIRS}

    def analyze(self, pair: str, price: float):
        """
        S3 Strategy: Simple Moving Average Crossover.
        Returns (direction, strength) where direction is 'CALL', 'PUT', or None.
        Strength is the percentage distance between the MAs.
        """
        history = self.price_history[pair]
        history.append(price)

        if len(history) < LONG_WINDOW:
            return None, 0.0

        short_ma = sum(list(history)[-SHORT_WINDOW:]) / SHORT_WINDOW
        long_ma = sum(history) / LONG_WINDOW

        prev_short = sum(list(history)[-SHORT_WINDOW-1:-1]) / SHORT_WINDOW if len(history) > SHORT_WINDOW else short_ma
        prev_long = sum(list(history)[-LONG_WINDOW-1:-1]) / LONG_WINDOW if len(history) > LONG_WINDOW else long_ma

        # Calculate strength as the percentage difference between MAs
        strength = abs(short_ma - long_ma) / long_ma if long_ma != 0 else 0

        # Detect crossover
        if prev_short <= prev_long and short_ma > long_ma:
            return "CALL", strength
        elif prev_short >= prev_long and short_ma < long_ma:
            return "PUT", strength
            
        return None, strength
