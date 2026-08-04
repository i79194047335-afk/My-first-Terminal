"""
Range-shock detector for the four FXCM pairs.

A shock is an M1 candle whose range (high-low) is a large outlier against the
same pair's own trailing 30 candles, fired only while the leader pair EUR/USD
stays calm. The leader test is what separates a pair-specific move from a broad
dollar impulse: when EUR/USD spikes too, every pair spikes and the event says
nothing about the follower.

Thresholds are per-pair because the pairs' range distributions differ. Measured
over 2026-07-21..2026-08-03 (hypothesis/calibrate_sigmas.py), the 99.9th
percentile of the range z-score was:

    USD/JPY  8.0      AUD/USD  4.8      USD/CAD  4.7   (EUR/USD is the leader)

USD/JPY is pinned at 8.0 by the owner. The others take their own p99.9, so all
pairs fire at a comparable rate (~0.5-0.8/day) instead of USD/CAD never firing —
its observed maximum over those two weeks was 6.6, below USD/JPY's threshold.

Sigma measures rarity within a pair, not move size: 8 sigma on USD/JPY and 4.5
on USD/CAD are both "a once-in-two-weeks candle for this instrument".

Signals fire on the FORMING candle, not the closed one: waiting for the close
costs up to a minute, and the point is to see the move while it happens. A
forming candle's range only grows, so the first tick that pushes it past the
threshold is the signal, and that minute then stays silent — otherwise a single
burst would emit a signal on every subsequent tick.

The consequence, accepted by the owner: a candle that spikes and then retraces
leaves a long wick rather than a wide body. That is still range, and the signal
still stands.

IMPORTANT: back-tests found no edge in what happens *after* a shock — the
next-minute reversal rate was 42.9% against a 52.0% baseline, i.e. noise
(hypothesis/uj_shock_reversal.py). This detector marks unusual candles; it does
not predict direction.

Python 3.7 compatible: lives in core/ and must parse on the feed interpreter.
"""
import statistics

# Per-pair range z-score thresholds. See module docstring for provenance.
SHOCK_SIGMA = {
    "USD/JPY": 8.0,
    "AUD/USD": 5.0,
    "USD/CAD": 4.5,
}

# The leader must stay below this z-score for a follower's shock to count.
LEADER_SYMBOL = "EUR/USD"
LEADER_CALM_SIGMA = 2.0

# Trailing candles used for the rolling range mean/stdev.
LOOKBACK = 30


def _range_of(candle):
    """Return a candle's high-low range.

    Args:
        candle: Mapping with "h" and "l" keys.

    Returns:
        Range as a float, 0.0 when the keys are missing.
    """
    try:
        return float(candle["h"]) - float(candle["l"])
    except (KeyError, TypeError, ValueError):
        return 0.0


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


def direction(candle):
    """Classify a candle's colour.

    Args:
        candle: Mapping with "o" and "c" keys.

    Returns:
        1 for up, -1 for down, 0 for doji or unreadable.
    """
    try:
        o, c = float(candle["o"]), float(candle["c"])
    except (KeyError, TypeError, ValueError):
        return 0
    if c > o:
        return 1
    if c < o:
        return -1
    return 0


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

        # The leader must be present and calm; without its data we cannot tell
        # a pair-specific move from a dollar-wide one, so stay silent.
        leader_sigma = self.leader_z(candle_time)
        if leader_sigma is None or leader_sigma >= self.leader_calm:
            return None

        return {
            "symbol": symbol,
            "time": candle_time,
            "sigma": round(sigma, 2),
            "threshold": threshold,
            "leader_sigma": round(leader_sigma, 2),
            "direction": direction(candle),
            "range": _range_of(candle),
            "price": candle.get("c"),
            "high": candle.get("h"),
            "low": candle.get("l"),
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
