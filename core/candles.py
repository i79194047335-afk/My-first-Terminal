"""
Tick-to-candle aggregation shared across all feeds (FXCM, Lighter, …).

Provides:
  - CandleBuilder: per-symbol state machine that turns a tick stream into
    OHLC candles for multiple timeframes simultaneously.
  - aggregate_higher_tf: build higher-TF candles from an M1 source list
    (used for backfill bootstrap).

Единственное место в проекте, где считается граница бакета. Сетка провайдера
учитывается смещением (tf_offset): FXCM отдаёт H4 по 01/05/09/13/17/21 UTC,
D1 — по 21:00 UTC (торговый день), а не по UTC-полуночи.
"""


class CandleBuilder:
    """Per-symbol stateful candle builder — call .ingest() on every tick.

    On each tick the builder updates in-progress candles for every timeframe.
    When a bucket rolls over, the closed candle is returned so the caller can
    persist it (SQLite) and append it to the in-memory history.

    Usage::

        builder = CandleBuilder({"M1": 60, "M5": 300})
        # …или со смещением сетки провайдера:
        builder = CandleBuilder({"M1": 60, "H4": 14400}, {"H4": 3600})
        for tick in feed:
            closed = builder.ingest(tick.price, tick.ts)
            for tf, candle in closed.items():
                db.upsert_candle("fxcm", "EUR/USD", tf, candle)
    """

    def __init__(self, tf_defs: dict, tf_offsets: dict = None):
        """Create a builder for one symbol.

        Args:
            tf_defs:    Dict mapping timeframe name → seconds, e.g.
                        {"M1": 60, "M5": 300}. All TFs are updated on
                        every tick (sub-second TFs included).
            tf_offsets: Optional dict {tf: offset_seconds} — the provider's
                        grid offset from UTC midnight. Missing TF → 0.

        Returns:
            None.
        """
        self._tf_defs    = tf_defs
        self._tf_offsets = {} if tf_offsets is None else dict(tf_offsets)
        self._buckets    = {tf: None for tf in tf_defs}
        self._candles    = {tf: None for tf in tf_defs}
        self._history    = {tf: []  for tf in tf_defs}

    # ── bucket ──────────────────────────────────────────────────────────

    def _bucket_of(self, tf: str, ts: float) -> int:
        """Bucket start time for a tick, honouring the provider's grid offset.

        Смещение нулевое для всех ТФ, выровненных по UTC-полуночи, — тогда это
        обычное `ts // sec * sec`. Для брокерских сеток (FXCM H4/D1) оно не
        нулевое, и нарезка по полуночи породила бы вторую сетку поверх
        брокерской (баг, чинённый в server.py, коммит a1434d3).

        Args:
            tf: Timeframe name.
            ts: Tick timestamp, unix seconds (float допустим — time.time()).

        Returns:
            Unix seconds (int) of the bucket start this tick belongs to.
        """
        sec    = self._tf_defs[tf]
        offset = self._tf_offsets.get(tf, 0)
        return ((int(ts) - offset) // sec) * sec + offset

    def set_offsets(self, tf_offsets: dict) -> None:
        """Merge grid offsets into the builder (later calls win).

        Смещение узнаётся из истории уже ПОСЛЕ создания билдера, поэтому
        отдельный сеттер, а не только аргумент конструктора.

        Args:
            tf_offsets: Dict {tf: offset_seconds} to merge in.

        Returns:
            None.
        """
        self._tf_offsets.update(tf_offsets)

    @property
    def offsets(self) -> dict:
        """Current grid offsets.

        Args:
            None.

        Returns:
            Dict {tf: offset_seconds} (copy).
        """
        return dict(self._tf_offsets)

    @staticmethod
    def detect_offsets(bars_by_tf: dict, tf_defs: dict) -> dict:
        """Learn each TF's grid offset from the last closed bar of the provider.

        Считается из ДАННЫХ, а не константой: у FXCM смещение D1 плавает
        (21:00 летом, 22:00 зимой — переходы видны в market.db), и хардкод
        сгнил бы дважды в год.

        Args:
            bars_by_tf: Dict {tf: [closed bars, oldest first]} — бары провайдера.
            tf_defs:    Dict {tf: seconds}.

        Returns:
            Dict {tf: offset_seconds} — только для ТФ с непустым списком баров.
        """
        offsets = {}
        for tf, bars in bars_by_tf.items():
            if not bars:
                continue
            offsets[tf] = bars[-1]["time"] % tf_defs[tf]
        return offsets

    # ── public API ──────────────────────────────────────────────────────

    def ingest(self, price: float, ts: float, size=None, side=None):
        """Process a single tick.

        Args:
            price: Mid-price (or last trade price) of this tick.
            ts:    Unix timestamp in seconds (float).
            size:  Trade size in base units, or None if the provider has no
                   volume (FXCM). None keeps candles in their old OHLC shape.
            side:  Aggressor side ("buy"/"sell") or None. Feeds the delta.

        Returns:
            Dict {tf: candle} for every timeframe whose bucket just closed.
            The caller should persist each returned candle.
            If no bucket rolled over, returns an empty dict.
        """
        closed = {}

        for tf, sec in self._tf_defs.items():
            bucket = self._bucket_of(tf, ts)

            if self._buckets[tf] is None:
                # Very first tick for this TF
                self._buckets[tf] = bucket
                self._candles[tf] = _new_candle(bucket, price, size, side)
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
                    fresh = {
                        "time":  bucket,
                        "open":  prev_close,
                        "high":  max(prev_close, price),
                        "low":   min(prev_close, price),
                        "close": price,
                    }
                    # Объём принадлежит СЛЕДУЮЩЕЙ свече целиком: тик, закрывший
                    # предыдущую, торговался уже в новом бакете.
                    if size is not None:
                        _add_volume(fresh, price, size, side)
                    self._candles[tf] = fresh
                else:
                    # Секундные ТФ открываются от цены тика — гэп там норма.
                    self._candles[tf] = _new_candle(bucket, price, size, side)
                continue

            # Same bucket — extend the in-progress candle
            if self._candles[tf] is None:
                self._candles[tf] = _new_candle(bucket, price, size, side)
                continue

            c = self._candles[tf]
            c["high"]  = max(c["high"], price)
            c["low"]   = min(c["low"],  price)
            c["close"] = price
            if size is not None:
                _add_volume(c, price, size, side)

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

        Returns:
            None.
        """
        self._history[tf] = list(candles)

    def seed_current(self, tf: str, candle: dict) -> None:
        """Restore the in-progress candle of the current bucket after a restart.

        Без этого первый живой тик открыл бы свечу заново от своей цены — тот
        самый разрыв после рестарта (server.py:seed_current_candles, 50e63ac).

        Args:
            tf:     Timeframe name.
            candle: Candle dict (time/open/high/low/close); time = bucket start.

        Returns:
            None.
        """
        self._candles[tf] = dict(candle)
        self._buckets[tf] = candle["time"]

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

def _new_candle(bucket: int, price: float, size=None, side=None) -> dict:
    """Create a fresh OHLC candle dict from the first tick in a bucket.

    Args:
        bucket: Bucket start time (unix seconds).
        price:  Price of the first tick.
        size:   Trade size in base units, or None if the provider has no
                volume (FXCM). When None, no volume keys are added at all.
        side:   Aggressor side ("buy"/"sell") or None.

    Returns:
        Candle dict. Volume keys (vol_base/vol_quote/delta) are present only
        for providers that supply size — FXCM candles keep their old shape.
    """
    candle = {
        "time":  bucket,
        "open":  price,
        "high":  price,
        "low":   price,
        "close": price,
    }
    if size is not None:
        _add_volume(candle, price, size, side)
    return candle


def _add_volume(candle: dict, price: float, size: float, side) -> None:
    """Accumulate one trade into a candle's volume counters.

    Creates the counters on first use, so a candle opened by a provider
    without volume never grows these keys.

    vol_quote is accumulated per trade (size * price), not derived afterwards
    from vol_base * close: a candle's trades happen at different prices, so
    the product of totals would not equal the true turnover.

    Delta is the aggressor imbalance — buys minus sells in base units. It
    shows who pushed the price inside the bar, which OHLC alone cannot say.

    Args:
        candle: Candle dict to update in place.
        price:  Trade price.
        size:   Trade size in base units.
        side:   Aggressor side ("buy"/"sell"); None leaves delta unchanged.

    Returns:
        None.
    """
    candle["vol_base"]  = candle.get("vol_base", 0.0) + size
    candle["vol_quote"] = candle.get("vol_quote", 0.0) + size * price
    if side is not None:
        delta = candle.get("delta", 0.0)
        candle["delta"] = delta + (size if side == "buy" else -size)


def _sum_volume(target: dict, source: dict) -> None:
    """Add one source candle's volume counters into an aggregate candle.

    Only keys with an actual numeric value are touched: a provider without
    volume (FXCM) must not grow zero-valued volume keys on aggregation.

    The check is ``is not None``, not ``key in source``: candles read back
    from SQLite always carry every column, and for FXCM rows delta/vol_base
    come back as NULL → None. Testing for mere presence let None through into
    ``0.0 + None`` and killed every bus message that aggregated a higher TF.

    Args:
        target: Aggregate candle being built, updated in place.
        source: Lower-TF candle contributing to it.

    Returns:
        None.
    """
    for key in ("vol_base", "vol_quote", "delta"):
        if source.get(key) is not None:
            target[key] = target.get(key, 0.0) + source[key]


def aggregate_higher_tf(source: list, sec: int, offset: int = 0) -> list:
    """Build higher-TF candles from a sorted M1 source.

    Groups M1 candles into ``sec``-second buckets. Each bucket produces one
    candle: open from the first M1, high/low from extremes, close from the
    last M1.

    Volume keys (vol_base/vol_quote/delta) are summed when the source has
    them and omitted entirely when it does not — M5/M15 derived from Lighter
    M1 keep their turnover, while FXCM output stays pure OHLC.

    Args:
        source: List of M1 candle dicts (time, open, high, low, close,
                optionally vol_base/vol_quote/delta), sorted by time ascending.
        sec:    Target timeframe in seconds (e.g. 300 for M5).
        offset: Grid offset in seconds (0 = UTC midnight, как было раньше).

    Returns:
        List of aggregated candle dicts, sorted by time.
    """
    if not source:
        return []

    result = []
    bucket = None
    candle = None

    for c in source:
        b = ((c["time"] - offset) // sec) * sec + offset
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
            _sum_volume(candle, c)
        else:
            candle["high"]  = max(candle["high"], c["high"])
            candle["low"]   = min(candle["low"],  c["low"])
            candle["close"] = c["close"]
            _sum_volume(candle, c)

    if candle:
        result.append(candle)

    return result
