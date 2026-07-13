"""
Tick-to-candle aggregation shared across all feeds (FXCM, Lighter, …).

Provides:
  - CandleBuilder: per-symbol state machine that turns a tick stream into
    OHLC candles for multiple timeframes simultaneously.
  - aggregate_higher_tf: build higher-TF candles from an M1 source list
    (used for backfill bootstrap).
"""


class CandleBuilder:
    """Per-symbol stateful candle builder — call .ingest() on every tick.

    On each tick the builder updates in-progress candles for every timeframe.
    When a bucket rolls over, the closed candle is returned so the caller can
    persist it (SQLite) and append it to the in-memory history.

    Usage::

        builder = CandleBuilder({"M1": 60, "M5": 300})
        for tick in feed:
            closed = builder.ingest(tick.price, tick.ts)
            for tf, candle in closed.items():
                db.upsert_candle("fxcm", "EUR/USD", tf, candle)
    """

    def __init__(self, tf_defs: dict):
        """Create a builder for one symbol.

        Args:
            tf_defs: Dict mapping timeframe name → seconds, e.g.
                     {"M1": 60, "M5": 300}. All TFs are updated on
                     every tick (sub-second TFs included).
        """
        self._tf_defs = tf_defs
        self._buckets = {tf: None for tf in tf_defs}
        self._candles = {tf: None for tf in tf_defs}
        self._history = {tf: []  for tf in tf_defs}

    # ── public API ──────────────────────────────────────────────────────

    def ingest(self, price: float, ts: float):
        """Process a single tick.

        Args:
            price: Mid-price (or last trade price) of this tick.
            ts:    Unix timestamp in seconds (float).

        Returns:
            Dict {tf: candle} for every timeframe whose bucket just closed.
            The caller should persist each returned candle.
            If no bucket rolled over, returns an empty dict.
        """
        closed = {}

        for tf, sec in self._tf_defs.items():
            bucket = int(ts // sec) * sec

            if self._buckets[tf] is None:
                # Very first tick for this TF
                self._buckets[tf] = bucket
                self._candles[tf] = _new_candle(bucket, price)
                continue

            if bucket != self._buckets[tf]:
                # Bucket rolled — close the previous candle
                prev = self._candles[tf]
                if prev is not None:
                    self._history[tf].append(prev)
                    closed[tf] = prev

                prev_close = prev["close"] if prev else None
                self._buckets[tf] = bucket

                if sec >= 60 and prev_close is not None:
                    # M1+ TFs: open = previous close (no gaps)
                    self._candles[tf] = {
                        "time":  bucket,
                        "open":  prev_close,
                        "high":  max(prev_close, price),
                        "low":   min(prev_close, price),
                        "close": price,
                    }
                else:
                    self._candles[tf] = _new_candle(bucket, price)
                continue

            # Same bucket — extend the in-progress candle
            if self._candles[tf] is None:
                self._candles[tf] = _new_candle(bucket, price)
                continue

            c = self._candles[tf]
            c["high"]  = max(c["high"], price)
            c["low"]   = min(c["low"],  price)
            c["close"] = price

        return closed

    def current(self, tf: str) -> dict:
        """Return the in-progress (open) candle for a timeframe, or None."""
        return self._candles.get(tf)

    def history(self, tf: str) -> list:
        """Return the list of closed candles for a timeframe (oldest first)."""
        return self._history.get(tf, [])

    def seed_history(self, tf: str, candles: list) -> None:
        """Pre-populate history for a timeframe (e.g. from SQLite backfill).

        The last candle in the list is assumed to be the most recent *closed*
        candle; the builder will open the next candle on the first tick.

        Args:
            tf:      Timeframe name.
            candles: List of closed candle dicts, oldest first.
        """
        self._history[tf] = list(candles)

    def close_all(self):
        """Close all in-progress candles and return them.

        Use at shutdown to persist the final partial candles.

        Returns:
            Dict {tf: candle} of the just-closed in-progress candles.
        """
        closed = {}
        for tf, candle in self._candles.items():
            if candle is not None:
                self._history[tf].append(candle)
                closed[tf] = candle
                self._candles[tf] = None
        return closed


# ── helpers ────────────────────────────────────────────────────────────

def _new_candle(bucket: int, price: float) -> dict:
    """Create a fresh OHLC candle dict from the first tick in a bucket."""
    return {
        "time":  bucket,
        "open":  price,
        "high":  price,
        "low":   price,
        "close": price,
    }


def aggregate_higher_tf(source: list, sec: int) -> list:
    """Build higher-TF candles from a sorted M1 source.

    Groups M1 candles into ``sec``-second buckets. Each bucket produces one
    candle: open from the first M1, high/low from extremes, close from the
    last M1.

    Args:
        source: List of M1 candle dicts (time, open, high, low, close),
                sorted by time ascending.
        sec:    Target timeframe in seconds (e.g. 300 for M5).

    Returns:
        List of aggregated candle dicts, sorted by time.
    """
    if not source:
        return []

    result = []
    bucket = None
    candle = None

    for c in source:
        b = (c["time"] // sec) * sec
        if b != bucket:
            if candle:
                result.append(candle)
            bucket = b
            candle = {
                "time":  b,
                "open":  c["open"],
                "high":  c["high"],
                "low":   c["low"],
                "close": c["close"],
            }
        else:
            candle["high"]  = max(candle["high"], c["high"])
            candle["low"]   = min(candle["low"],  c["low"])
            candle["close"] = c["close"]

    if candle:
        result.append(candle)

    return result
