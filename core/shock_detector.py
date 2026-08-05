"""
Range-shock detector for the four FXCM pairs.

A shock is an M1 candle whose range (high-low) is a large outlier against the
same pair's own trailing 30 candles.

The leader pair EUR/USD CLASSIFIES the event rather than blocking it. When the
leader stayed calm the move belongs to this pair alone — "solo". When the leader
moved too, the pair was carried by a broad dollar impulse — "in_stream". Both
are reported; the distinction is what the reader needs, not a reason for
silence. (Until 2026-08-04 a noisy leader suppressed the shock outright, which
hid events like USD/JPY 11:56 on that day: 6.3 sigma, 91% of it inside 10
seconds, dropped purely because EUR/USD read 2.18 sigma.)

Thresholds are per-pair because the pairs' range distributions differ. Sigma
measures rarity within a pair, not move size: 6 sigma on USD/JPY and 4.5 on
USD/CAD are comparable statements about each instrument.

Current values (owner's choice on 2026-08-04, from the grid in
hypothesis/tune_sigma_solo.py measured over 2026-07-21..2026-08-03):

    USD/JPY  6.0  ->  4.3 events/day, 2.7 of them bursts
    AUD/USD  4.5  ->  3.9 events/day, 1.5 of them bursts
    USD/CAD  4.5  ->  3.0 events/day, 0.4 of them bursts

These are deliberately lower than the p99.9 values used before (8.0/5.0/4.5):
with the leader no longer blocking, the thresholds carry the whole load, and
6.0 on USD/JPY is what it takes to report the 11:56 candle above.

Signals fire on the FORMING candle, not the closed one: waiting for the close
costs up to a minute, and the point is to see the move while it happens. A
forming candle's range only grows, so the first tick that pushes it past the
threshold is the signal, and that minute then stays silent — otherwise a single
burst would emit a signal on every subsequent tick.

Shape: a wide minute built from evenly spread movement is a different animal
from one made by a single jump. Every shock is therefore classified by how much
of its range happened inside one BURST_SECONDS window — a SLIDING window, not
fixed slots. Measured over 2026-07-21..08-03, fixed 10s slots split real bursts
across a boundary and mislabelled 4 of the 6 sharpest events (e.g. 2026-07-21
01:29 read 48% fixed vs 83% sliding), so the sliding definition is the honest
one.

Both kinds are reported, but only a "burst" makes a sound: with the leader gate
removed the raw event rate roughly tripled, and burst is the filter that keeps
the audible stream near five a day instead of fourteen. "spread" events go to
the panel silently.

The consequence, accepted by the owner: a candle that spikes and then retraces
leaves a long wick rather than a wide body. That is still range, and the signal
still stands.

IMPORTANT: back-tests found no edge in what happens *after* a shock — the
next-minute reversal rate was 42.9% against a 52.0% baseline, i.e. noise
(hypothesis/uj_shock_reversal.py). This detector marks unusual candles; it does
not predict direction.

Python 3.7 compatible: lives in core/ and must parse on the feed interpreter.
"""
import csv
import os
import statistics
import time
from datetime import datetime

# Per-pair range z-score thresholds. See module docstring for provenance.
SHOCK_SIGMA = {
    "USD/JPY": 6.0,
    "AUD/USD": 4.5,
    "USD/CAD": 4.5,
}

# Below this z-score the leader counts as calm, and the shock is labelled
# "solo". At or above it the shock is "in_stream". This no longer suppresses
# anything — it only sets the label.
LEADER_SYMBOL = "EUR/USD"
LEADER_CALM_SIGMA = 2.0

# Trailing candles used for the rolling range mean/stdev.
LOOKBACK = 30

# How far back warmup_from_ticks() rebuilds. Slightly more than LOOKBACK so a
# gap or two in the archive still leaves a full window.
WARMUP_MINUTES = 40

# Burst shape. A shock is "burst" when at least BURST_MIN_COVERAGE of the
# minute's range happened inside one BURST_SECONDS sliding window.
#
# 0.70 is the owner's setting. For reference, over 2026-07-21..08-03 the
# audible shocks split like this (hypothesis/burst_concentration.py):
#     >= 60%: 14    >= 70%: 9    >= 80%: 6    >= 90%: 2
BURST_SECONDS = 10
BURST_MIN_COVERAGE = 0.70


# Two candle dialects coexist in this project and BOTH reach this module:
# CandleBuilder emits open/high/low/close, while core/db and the archive
# scripts use o/h/l/c. Reading only the short keys silently yielded a zero
# range for every live candle, so the hub could never fire a signal — that was
# the state of production between 2026-08-04 and 2026-08-05.
def _field(candle, short, long_name):
    """Read one OHLC field, accepting either candle dialect.

    Args:
        candle: Candle mapping in either dialect.
        short: Short key, e.g. "h".
        long_name: Long key, e.g. "high".

    Returns:
        Field value as float, or None when absent/unreadable.
    """
    if not isinstance(candle, dict):
        return None
    value = candle.get(short)
    if value is None:
        value = candle.get(long_name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _range_of(candle):
    """Return a candle's high-low range.

    Args:
        candle: Mapping with h/l or high/low keys.

    Returns:
        Range as a float, 0.0 when the keys are missing.
    """
    high = _field(candle, "h", "high")
    low = _field(candle, "l", "low")
    if high is None or low is None:
        return 0.0
    return high - low


def range_zscore(candle, history):
    """Score a candle's range against its trailing history.

    Args:
        candle: The candle under test (mapping with "h"/"l").
        history: Preceding candles, oldest first, at least 5 entries.

    Returns:
        Z-score of the candle's range, or None when history is too short
        or degenerate (zero variance).
    """
    if len(history) < 5:
        return None
    ranges = [_range_of(c) for c in history]
    mean = statistics.mean(ranges)
    try:
        stdev = statistics.stdev(ranges)
    except statistics.StatisticsError:
        return None
    if stdev <= 0:
        return None
    return (_range_of(candle) - mean) / stdev


def burst_coverage(ticks, minute_range, window_seconds=BURST_SECONDS):
    """Largest share of a minute's range covered by one sliding time window.

    Walks a window over the tick series and takes the widest high-low found
    inside any span of `window_seconds`. Sliding rather than fixed slots: a
    burst that straddles a slot boundary would otherwise read as two halves.

    Args:
        ticks: List of (offset_seconds_within_minute, price), time-sorted.
        minute_range: High-low of the whole minute.
        window_seconds: Burst window length.

    Returns:
        Tuple (coverage, start_offset): coverage in 0..1 and the offset the
        best window starts at; (0.0, None) when it cannot be measured.
    """
    if minute_range <= 0 or len(ticks) < 2:
        return 0.0, None

    best = 0.0
    best_start = None
    # Two pointers: `j` runs ahead while it stays inside the window. Prices are
    # rescanned per window, which is fine at forex tick rates (~150/min).
    for i in range(len(ticks)):
        start = ticks[i][0]
        hi = lo = ticks[i][1]
        for j in range(i + 1, len(ticks)):
            if ticks[j][0] - start > window_seconds:
                break
            price = ticks[j][1]
            if price > hi:
                hi = price
            elif price < lo:
                lo = price
        cover = (hi - lo) / minute_range
        if cover > best:
            best = cover
            best_start = start
    return best, best_start


def direction(candle):
    """Classify a candle's colour.

    Args:
        candle: Mapping with o/c or open/close keys.

    Returns:
        1 for up, -1 for down, 0 for doji or unreadable.
    """
    o = _field(candle, "o", "open")
    c = _field(candle, "c", "close")
    if o is None or c is None:
        return 0
    if c > o:
        return 1
    if c < o:
        return -1
    return 0


def warmup_from_ticks(detector, data_dir, symbols, now_ts=None,
                      minutes=WARMUP_MINUTES):
    """Prime a detector from the tick archive so it works from the first tick.

    The rolling statistics need LOOKBACK finished minutes before anything can
    be scored. Started cold, the hub is therefore blind for half an hour — and
    the archive already holds those minutes, so there is no reason to wait for
    them to happen again.

    Ticks are read (not candles) because burst shape is measurable only from
    ticks; a minute rebuilt from OHLC alone could never be classified.

    Args:
        detector: ShockDetector to fill.
        data_dir: Directory holding <SYMBOL>_<YYYYMMDD>.csv tick files.
        symbols: Iterable of provider-form symbols, e.g. ["EUR/USD", ...].
        now_ts: Reference time, unix seconds; defaults to the current time.
        minutes: How many minutes back to rebuild.

    Returns:
        Dict {symbol: minutes_restored} — how many finished minutes each
        symbol contributed.
    """
    if now_ts is None:
        now_ts = time.time()
    start_ts = int(now_ts) - minutes * 60
    restored = {}

    for symbol in symbols:
        prefix = symbol.replace("/", "")
        # Ticks may straddle midnight, so read yesterday's file too.
        days = sorted({
            datetime.utcfromtimestamp(start_ts).strftime("%Y%m%d"),
            datetime.utcfromtimestamp(now_ts).strftime("%Y%m%d"),
        })

        buckets = {}
        for day in days:
            path = os.path.join(data_dir, "%s_%s.csv" % (prefix, day))
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r") as fh:
                    for row in csv.DictReader(fh):
                        try:
                            ts = float(row["timestamp_utc"])
                            mid = float(row["mid"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if ts < start_ts or ts > now_ts:
                            continue
                        bucket = int(ts // 60) * 60
                        candle = buckets.get(bucket)
                        if candle is None:
                            buckets[bucket] = {"time": bucket, "o": mid, "h": mid,
                                               "l": mid, "c": mid,
                                               "ticks": [(ts - bucket, mid)]}
                        else:
                            if mid > candle["h"]:
                                candle["h"] = mid
                            if mid < candle["l"]:
                                candle["l"] = mid
                            candle["c"] = mid
                            candle["ticks"].append((ts - bucket, mid))
            except (IOError, OSError):
                continue

        if not buckets:
            restored[symbol] = 0
            continue

        # The most recent bucket is still forming in the live feed; leaving it
        # in history would let a partial minute into the statistics.
        ordered = [buckets[b] for b in sorted(buckets)][:-1]
        for candle in ordered:
            detector.push(symbol, {k: candle[k] for k in ("time", "o", "h", "l", "c")})
        restored[symbol] = len(ordered)

    return restored


class ShockDetector(object):
    """Evaluates finished M1 candles and reports shocks."""

    def __init__(self, thresholds=None, lookback=LOOKBACK,
                 leader_symbol=LEADER_SYMBOL, leader_calm=LEADER_CALM_SIGMA):
        """Configure the detector.

        Args:
            thresholds: Optional {symbol: sigma} override for SHOCK_SIGMA.
            lookback: Trailing candle count for rolling stats.
            leader_symbol: Pair that must stay calm, normally EUR/USD.
            leader_calm: Leader z-score ceiling.
        """
        self.thresholds = dict(SHOCK_SIGMA if thresholds is None else thresholds)
        self.lookback = lookback
        self.leader_symbol = leader_symbol
        self.leader_calm = leader_calm
        # symbol -> list of recent FINISHED candles, oldest first. The forming
        # candle is deliberately kept out: its range is still growing and would
        # contaminate the very statistics it is being scored against.
        self._history = {}
        # symbol -> candle time already signalled, so one burst fires once.
        self._fired_at = {}
        # Live leader candle, needed to judge calm before the minute closes:
        # symbol -> candle.
        self._live = {}
        # Ticks of the minute currently forming, per symbol, for burst shape:
        # symbol -> {"time": bucket, "ticks": [(offset, price), ...]}.
        # Only the current minute is kept — the buffer resets on rollover.
        self._tick_buf = {}

    def push(self, symbol, candle):
        """Append a finished candle to a symbol's rolling history.

        Args:
            symbol: Pair in provider form, e.g. "USD/JPY".
            candle: Mapping with "time"/"o"/"h"/"l"/"c".
        """
        hist = self._history.setdefault(symbol, [])
        if hist and hist[-1].get("time") == candle.get("time"):
            hist[-1] = candle
            return
        hist.append(candle)
        # Keep a little more than the lookback so gap checks have room.
        if len(hist) > self.lookback + 5:
            del hist[0:len(hist) - (self.lookback + 5)]

    def history(self, symbol):
        """Return the stored candles for a symbol.

        Args:
            symbol: Pair in provider form.

        Returns:
            List of candles, oldest first (may be empty).
        """
        return self._history.get(symbol, [])

    def _contiguous_lookback(self, symbol, candle_time):
        """Fetch an unbroken lookback window ending just before a candle.

        A gap (weekend, feed outage) would make the rolling stats span
        unrelated market conditions, so such windows are rejected.

        Args:
            symbol: Pair in provider form.
            candle_time: Start time of the candle under test.

        Returns:
            List of `lookback` candles, or None if unavailable or broken.
        """
        hist = [c for c in self._history.get(symbol, []) if c.get("time", 0) < candle_time]
        if len(hist) < self.lookback:
            return None
        window = hist[-self.lookback:]
        if window[-1].get("time") != candle_time - 60:
            return None
        if candle_time - window[0].get("time", 0) != self.lookback * 60:
            return None
        return window

    def push_live(self, symbol, candle):
        """Record the currently forming candle for a symbol.

        Kept separate from push(): the forming candle must be visible for
        scoring but must never enter the rolling history.

        Args:
            symbol: Pair in provider form.
            candle: The forming candle (mapping with time/o/h/l/c).
        """
        self._live[symbol] = candle

    def push_tick(self, symbol, ts, price):
        """Record a tick for burst-shape measurement of the current minute.

        Args:
            symbol: Pair in provider form.
            ts: Tick timestamp, unix seconds (fractional allowed).
            price: Tick price.
        """
        bucket = int(ts // 60) * 60
        buf = self._tick_buf.get(symbol)
        if buf is None or buf["time"] != bucket:
            # New minute: the previous minute's ticks are of no further use.
            buf = {"time": bucket, "ticks": []}
            self._tick_buf[symbol] = buf
        buf["ticks"].append((ts - bucket, price))

    def shape_of(self, symbol, candle_time, minute_range):
        """Classify how concentrated the minute's movement was.

        Args:
            symbol: Pair in provider form.
            candle_time: Minute-bucket start time.
            minute_range: High-low of the minute.

        Returns:
            Dict {shape, coverage, burst_start} — shape is "burst", "spread",
            or "unknown" when no ticks were recorded for that minute (e.g. a
            back-test driven by candles alone).
        """
        buf = self._tick_buf.get(symbol)
        if buf is None or buf["time"] != candle_time or len(buf["ticks"]) < 2:
            return {"shape": "unknown", "coverage": None, "burst_start": None}

        coverage, start = burst_coverage(buf["ticks"], minute_range)
        return {
            "shape": "burst" if coverage >= BURST_MIN_COVERAGE else "spread",
            "coverage": round(coverage, 3),
            "burst_start": start,
        }

    def leader_z(self, candle_time):
        """Score the leader pair's range for a given minute.

        Prefers the leader's live candle for that minute, falling back to the
        finished one — while a follower is still forming, the leader normally
        has not closed either.

        Args:
            candle_time: Minute-bucket start time, unix seconds.

        Returns:
            The leader's range z-score, or None when it cannot be computed.
        """
        leader_candle = None
        live = self._live.get(self.leader_symbol)
        if live is not None and live.get("time") == candle_time:
            leader_candle = live
        else:
            for c in self._history.get(self.leader_symbol, []):
                if c.get("time") == candle_time:
                    leader_candle = c
                    break
        if leader_candle is None:
            return None
        window = self._contiguous_lookback(self.leader_symbol, candle_time)
        if window is None:
            return None
        return range_zscore(leader_candle, window)

    def evaluate_live(self, symbol, candle):
        """Test the forming candle for a shock, at most once per minute.

        Args:
            symbol: Pair in provider form.
            candle: The forming candle (mapping with time/o/h/l/c).

        Returns:
            Event dict as evaluate() returns, plus "live": True, or None when
            the candle is not (yet) a shock or this minute already fired.
        """
        if symbol == self.leader_symbol:
            return None
        candle_time = candle.get("time")
        if candle_time is None:
            return None

        self.push_live(symbol, candle)

        # One signal per candle: range only grows, so without this every
        # subsequent tick of the same burst would fire again.
        if self._fired_at.get(symbol) == candle_time:
            return None

        event = self._score(symbol, candle, candle_time)
        if event is None:
            return None

        self._fired_at[symbol] = candle_time
        event["live"] = True
        return event

    def _score(self, symbol, candle, candle_time):
        """Score a candle against its own history and the leader's calm.

        Shared by evaluate() and evaluate_live() so both paths apply identical
        thresholds and guards.

        Args:
            symbol: Pair in provider form.
            candle: Candle being scored (forming or finished).
            candle_time: The candle's minute-bucket start time.

        Returns:
            Event dict, or None when this is not a shock.
        """
        threshold = self.thresholds.get(symbol)
        if threshold is None:
            return None

        window = self._contiguous_lookback(symbol, candle_time)
        if window is None:
            return None

        sigma = range_zscore(candle, window)
        if sigma is None or sigma < threshold:
            return None

        # The leader labels the event, it no longer blocks it. Calm leader ->
        # this pair moved alone; noisy leader -> it rode a dollar-wide move.
        # Without leader data the origin is unknown, which is stated rather
        # than guessed.
        leader_sigma = self.leader_z(candle_time)
        if leader_sigma is None:
            origin = "unknown"
        elif leader_sigma < self.leader_calm:
            origin = "solo"
        else:
            origin = "in_stream"

        # Shape does NOT gate the signal either — both kinds are reported, the
        # front end distinguishes them. A wide-but-even minute is still
        # unusual, it is simply a different event from a single jump.
        shape = self.shape_of(symbol, candle_time, _range_of(candle))

        return {
            "symbol": symbol,
            "time": candle_time,
            "sigma": round(sigma, 2),
            "threshold": threshold,
            "leader_sigma": None if leader_sigma is None else round(leader_sigma, 2),
            "origin": origin,
            "direction": direction(candle),
            "range": _range_of(candle),
            "price": _field(candle, "c", "close"),
            "high": _field(candle, "h", "high"),
            "low": _field(candle, "l", "low"),
            "shape": shape["shape"],
            "coverage": shape["coverage"],
            "burst_start": shape["burst_start"],
        }

    def evaluate(self, symbol, candle):
        """Test one finished candle for a shock.

        Kept for back-tests and for the close-time top-up: a candle whose range
        only crosses the threshold on its very last ticks would otherwise be
        missed by the live path.

        Args:
            symbol: Pair in provider form.
            candle: The finished candle to test.

        Returns:
            Dict describing the shock, or None when it is not a shock or the
            minute already fired live.
        """
        if symbol == self.leader_symbol:
            return None
        candle_time = candle.get("time")
        if candle_time is None:
            return None
        if self._fired_at.get(symbol) == candle_time:
            return None

        event = self._score(symbol, candle, candle_time)
        if event is None:
            return None

        self._fired_at[symbol] = candle_time
        event["live"] = False
        return event
