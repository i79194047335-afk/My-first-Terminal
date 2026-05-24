from collections import deque


class StructureEngine:
    def __init__(self):
        self.prices = deque(maxlen=300)
        self.times = deque(maxlen=300)

    def update(self, price, ts):

        self.prices.append(price)
        self.times.append(ts)

        if len(self.prices) < 50:
            return None

        high = max(self.prices)
        low = min(self.prices)
        width = high - low

        if width == 0:
            return None

        # позиция внутри диапазона (0..1)
        pos = (price - low) / width

        # расстояние до границ
        dist_high = (high - price) / width
        dist_low = (price - low) / width

        # === КЛЮЧ ===
        NEAR = 0.15  # 15% диапазона

        near_high = dist_high < NEAR
        near_low = dist_low < NEAR

        return {
            "range_high": high,
            "range_low": low,
            "range_width": width,
            "range_pos_local": round(pos, 3),
            "near_high": near_high,
            "near_low": near_low
        }


# ------------------------------------------------------
# событие: подход к границе
# ------------------------------------------------------
def detect_event(structure):

    if not structure:
        return None

    if structure["near_high"]:
        return "approach_high"

    if structure["near_low"]:
        return "approach_low"

    return None
