from collections import deque
import csv
import os
import time
from datetime import datetime, timezone


# ============================================================
# VELOCITY LOGGER
# ============================================================

VEL_LOG_DIR = "vel_log"
os.makedirs(VEL_LOG_DIR, exist_ok=True)

_vel_writers = {}  # date_str -> {file, writer}


def _get_session(hour_utc):
    if 0 <= hour_utc < 8:
        return "asia"
    elif 8 <= hour_utc < 16:
        return "europe"
    else:
        return "america"


def _vel_log(symbol, velocity_raw, dt_val, price, ts):
    dt_utc   = datetime.utcfromtimestamp(ts)
    date_str = dt_utc.strftime("%Y%m%d")
    session  = _get_session(dt_utc.hour)

    if date_str not in _vel_writers:
        # закрываем старые
        for v in _vel_writers.values():
            v["file"].close()
        _vel_writers.clear()

        path     = os.path.join(VEL_LOG_DIR, f"velocity_{date_str}.csv")
        is_new   = not os.path.exists(path)
        f        = open(path, "a", newline="", buffering=1)
        w        = csv.writer(f)
        if is_new:
            w.writerow(["timestamp", "symbol", "session", "velocity_raw", "dt", "price"])
        _vel_writers[date_str] = {"file": f, "writer": w}

    _vel_writers[date_str]["writer"].writerow([
        round(ts, 3),
        symbol,
        session,
        round(velocity_raw, 8),
        round(dt_val, 3),
        round(price, 5)
    ])


# ============================================================


class MarketEngine:

    # Временное окно для расчёта velocity (секунды).
    # Одинаково для FXCM и Dukascopy — результат сопоставим
    # независимо от частоты тиков источника.
    VELOCITY_WINDOW = 3.0

    def __init__(self):

        self.ticks      = deque(maxlen=1000)
        self.tick_dirs  = deque(maxlen=200)
        self.last_price = None
        self.prev_velocity = 0
        self.last_state = None

    # ----------------------------------------------------------
    def update_tick(self, symbol, price, ts, state, tf_data):

        self.ticks.append({"price": price, "ts": ts})

        # --- фильтр микродвижений ---
        MIN_MOVE = 0.00005

        if self.last_price is not None:
            if abs(price - self.last_price) < MIN_MOVE:
                return None

        if self.last_price is None:
            self.last_price = price
            return None

        direction = 1 if price > self.last_price else -1
        self.tick_dirs.append(direction)
        self.last_price = price

        if len(self.ticks) < 50:
            return None

        # ---------------- PRESSURE ----------------

        buy  = sum(1 for d in self.tick_dirs if d == 1)
        sell = sum(1 for d in self.tick_dirs if d == -1)
        total = buy + sell

        pressure       = buy / total if total else 0.5
        tick_imbalance = pressure

        # ---------------- VELOCITY ----------------
        # Считаем по фиксированному временному окну VELOCITY_WINDOW секунд.
        # Ищем самый старый тик внутри окна и берём движение от него.
        # Это даёт одинаковый физический смысл на любом источнике данных:
        # FXCM (1-2 тика/сек) и Dukascopy (10-20 тиков/сек) дадут
        # сопоставимые значения velocity.

        velocity_raw = 0
        dt_used      = 0

        current_ts  = self.ticks[-1]["ts"]
        current_p   = self.ticks[-1]["price"]
        window_start = current_ts - self.VELOCITY_WINDOW

        # Ищем самый старый тик в окне (идём с конца)
        ref_tick = None
        for tick in reversed(self.ticks):
            if tick["ts"] <= window_start:
                ref_tick = tick
                break

        if ref_tick is not None:
            dt_used      = current_ts - ref_tick["ts"]
            if dt_used > 0:
                velocity_raw = (current_p - ref_tick["price"]) / dt_used
                _vel_log(symbol, velocity_raw, dt_used, price, ts)
        else:
            # Окно ещё не накоплено — берём самый старый тик из буфера
            oldest = self.ticks[0]
            dt_used = current_ts - oldest["ts"]
            if dt_used > 0:
                velocity_raw = (current_p - oldest["price"]) / dt_used
                _vel_log(symbol, velocity_raw, dt_used, price, ts)

        SMOOTH_ALPHA = 0.2
        velocity = (
            SMOOTH_ALPHA * velocity_raw +
            (1 - SMOOTH_ALPHA) * self.prev_velocity
        )

        # ---------------- ACCELERATION ----------------

        acceleration       = velocity - self.prev_velocity
        self.prev_velocity = velocity

        # ---------------- TICK RATE ----------------
        # сколько тиков в секунду за последние 50 тиков

        t1 = self.ticks[-1]["ts"]
        t2 = self.ticks[-50]["ts"]
        dt = t1 - t2
        if dt <= 0:
            return None
        tick_rate = 50 / dt        # тиков/сек

        # ---------------- MICRO TREND ----------------

        micro = sum(self.tick_dirs)

        if micro > 5:
            micro_trend = "up"
        elif micro < -5:
            micro_trend = "down"
        else:
            micro_trend = "flat"

        # ---------------- HTF (M5 последние 30 свечей = 2.5ч) ----------------

        m5 = tf_data.get("M5", [])

        if len(m5) < 30:
            return None

        closes = [c["close"] for c in m5[-30:]]

        if closes[-1] > closes[0]:
            htf = "trend_up"
        elif closes[-1] < closes[0]:
            htf = "trend_down"
        else:
            htf = "range"

        # ---------------- RANGE POSITION ----------------

        high       = max(closes)
        low        = min(closes)
        range_size = high - low

        if high != low:
            range_pos = (price - low) / (high - low)
            range_pos = max(0.0, min(1.0, range_pos))
        else:
            range_pos = 0.5

        # ---------------- VOLATILITY ----------------
        # сравниваем последнюю M5 свечу со средней

        ranges = [c["high"] - c["low"] for c in m5[-20:]]

        if len(ranges) >= 2:
            avg_range = sum(ranges[:-1]) / (len(ranges) - 1)
            cur_range = ranges[-1]
            vol_ratio = cur_range / avg_range if avg_range > 0 else 1.0

            if vol_ratio > 1.5:
                volatility = "high"
            elif vol_ratio < 0.6:
                volatility = "low"
            else:
                volatility = "normal"
        else:
            volatility = "normal"
            vol_ratio  = 1.0

        # ---------------- STATE ----------------

        new_state = {
            "htf":            htf,
            "pressure":       round(pressure,       3),
            "tick_imbalance": round(tick_imbalance, 3),
            "velocity":       round(velocity,       6),
            "acceleration":   round(acceleration,   6),
            "tick_rate":      round(tick_rate,      3),
            "range_pos":      round(range_pos,      3),
            "range_size":     round(range_size,     6),
            "micro_trend":    micro_trend,
            "volatility":     volatility,
            "vol_ratio":      round(vol_ratio,      3),
        }

        # ---------------- STATE CHANGE FILTER ----------------

        if self.last_state:
            dist = sum(
                abs(new_state[k] - self.last_state[k])
                for k in ["pressure", "velocity", "acceleration", "range_pos"]
            )
            if dist < 0.05:
                return None

        self.last_state = new_state
        return new_state

