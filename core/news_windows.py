"""
News and session exclusion windows for the shock indicator.

Two independent kinds of window suppress a shock signal:

  * session windows  — +-30 min around opens/rollover, fixed clock times;
  * news windows     — +-15 min around a high-impact release, read from
                       data_loaders/news_calendar.csv (kept fresh by
                       data_loaders/sync_calendar.py).

A news event only masks the pairs whose currencies it touches: US CPI silences
EUR/USD, USD/JPY, AUD/USD and USD/CAD, while a BOC rate decision only silences
USD/CAD. Session windows apply to every pair.

The calendar is re-read when its mtime changes, so a cron refresh is picked up
without restarting the hub.

Python 3.7 compatible: this module lives in core/ and must parse on the feed's
interpreter as well as the hub's.
"""
import csv
import os
from datetime import datetime, timedelta, timezone

CALENDAR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data_loaders", "news_calendar.csv")

# Owner's spec: 30 minutes each side of a session event, 15 around news.
SESSION_WINDOW_MIN = 30
NEWS_WINDOW_MIN = 15

# UTC clock times that reshape liquidity: (hour, minute, label). Each is a
# point event; the ±SESSION_WINDOW_MIN window is applied around it.
#
# Note: NY close (20:00) and a point-event rollover (21:00) used to sit here
# too, but together their ±30-min windows blanketed 19:30–21:30 in silence.
# The owner wants the broker-downtime block to start at 21:00 sharp, so the
# rollover is now a RANGE (see SESSION_RANGES_UTC) and NY close is dropped.
SESSION_EVENTS_UTC = [
    (0, 0, "Tokyo open"),
    (7, 0, "London open"),
    (12, 30, "US data slot"),
    (13, 30, "NY open"),
]

# Continuous silence windows: (start_hour, start_minute, end_hour, end_minute,
# label), UTC, inclusive of start and exclusive of end. Unlike SESSION_EVENTS
# these are exact spans, not a point ± a margin. The broker is down 21:00–23:00
# UTC (see CLAUDE.md «Downtime window» / bot payout.is_broker_down): no ticks
# arrive, so a shock there would be an artefact of the gap, not a real move.
SESSION_RANGES_UTC = [
    (21, 0, 23, 0, "broker downtime / rollover"),
]

# Which currencies each tradable symbol is exposed to.
SYMBOL_CURRENCIES = {
    "EUR/USD": ("EUR", "USD"),
    "USD/JPY": ("USD", "JPY"),
    "AUD/USD": ("AUD", "USD"),
    "USD/CAD": ("USD", "CAD"),
}


class NewsWindows(object):
    """Answers whether a timestamp falls inside a suppression window."""

    def __init__(self, calendar_path=CALENDAR_PATH):
        """Prepare a calendar reader.

        Args:
            calendar_path: Path to the news CSV; missing file is tolerated.
        """
        self._path = calendar_path
        self._events = []
        self._mtime = None
        self.reload()

    def reload(self):
        """Re-read the calendar CSV if it changed on disk.

        Returns:
            True if events were reloaded, False if the file was unchanged
            or unreadable.
        """
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            self._events = []
            self._mtime = None
            return False

        if self._mtime is not None and mtime == self._mtime:
            return False

        events = []
        try:
            with open(self._path, "r") as fh:
                for row in csv.DictReader(fh):
                    ts_raw = (row.get("ts_utc") or "").strip()
                    if not ts_raw.isdigit():
                        continue
                    events.append({
                        "ts": int(ts_raw),
                        "currency": (row.get("currency") or "").strip().upper(),
                        "event": (row.get("event") or "").strip(),
                    })
        except (IOError, OSError):
            return False

        events.sort(key=lambda e: e["ts"])
        self._events = events
        self._mtime = mtime
        return True

    def session_window(self, ts):
        """Check whether a timestamp sits in a session-open window.

        Args:
            ts: Unix seconds, UTC.

        Returns:
            Tuple (label, until_ts): the window's label and the unix time it
            ends (exact for ranges, ±SESSION_WINDOW_MIN edge for point events),
            or (None, None) when no window is active.
        """
        dt = datetime.fromtimestamp(ts, timezone.utc)

        # Continuous ranges: exact spans, checked minute-of-day. All current
        # ranges live inside one UTC day (21:00–23:00), so no midnight wrap.
        minute_of_day = dt.hour * 60 + dt.minute
        for sh, sm, eh, em, label in SESSION_RANGES_UTC:
            start = sh * 60 + sm
            end = eh * 60 + em
            if start <= minute_of_day < end:
                end_dt = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
                return label, int(end_dt.timestamp())

        # Point events: ±SESSION_WINDOW_MIN around a fixed clock time.
        for hh, mm, label in SESSION_EVENTS_UTC:
            event = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
            # Check neighbouring days so windows spanning midnight still match.
            for shifted in (event - timedelta(days=1), event, event + timedelta(days=1)):
                if abs((dt - shifted).total_seconds()) <= SESSION_WINDOW_MIN * 60:
                    until = shifted + timedelta(minutes=SESSION_WINDOW_MIN)
                    return label, int(until.timestamp())
        return None, None

    def news_window(self, ts, symbol):
        """Check whether a timestamp sits in a news window for this symbol.

        Args:
            ts: Unix seconds, UTC.
            symbol: Pair in provider form, e.g. "USD/JPY".

        Returns:
            Dict with the matching event (ts, currency, event, minutes_to),
            or None when the pair is unaffected.
        """
        self.reload()
        currencies = SYMBOL_CURRENCIES.get(symbol)
        if not currencies:
            return None

        span = NEWS_WINDOW_MIN * 60
        for ev in self._events:
            if abs(ev["ts"] - ts) > span:
                continue
            if ev["currency"] not in currencies:
                continue
            return {
                "ts": ev["ts"],
                "currency": ev["currency"],
                "event": ev["event"],
                "minutes_to": int(round((ev["ts"] - ts) / 60.0)),
            }
        return None

    def blocked(self, ts, symbol):
        """Report any active suppression for a symbol at a timestamp.

        Args:
            ts: Unix seconds, UTC.
            symbol: Pair in provider form, e.g. "USD/JPY".

        Returns:
            Dict {kind, label, until_ts, ...} describing the window, or None
            when nothing suppresses the signal. `kind` is "session" or "news".
        """
        news = self.news_window(ts, symbol)
        if news:
            return {
                "kind": "news",
                "label": "{} {}".format(news["currency"], news["event"]),
                "until_ts": news["ts"] + NEWS_WINDOW_MIN * 60,
                "minutes_to": news["minutes_to"],
            }

        session_label, session_until = self.session_window(ts)
        if session_label:
            return {
                "kind": "session",
                "label": session_label,
                "until_ts": session_until,
                "minutes_to": None,
            }
        return None

    def upcoming(self, ts, symbol, horizon_hours=12):
        """List news events ahead of a timestamp, for the front-end badge.

        Args:
            ts: Unix seconds, UTC.
            symbol: Pair in provider form.
            horizon_hours: How far ahead to look.

        Returns:
            List of dicts {ts, currency, event, minutes_to}, soonest first.
        """
        self.reload()
        currencies = SYMBOL_CURRENCIES.get(symbol)
        if not currencies:
            return []

        horizon = ts + horizon_hours * 3600
        out = []
        for ev in self._events:
            if not (ts <= ev["ts"] <= horizon):
                continue
            if ev["currency"] not in currencies:
                continue
            out.append({
                "ts": ev["ts"],
                "currency": ev["currency"],
                "event": ev["event"],
                "minutes_to": int(round((ev["ts"] - ts) / 60.0)),
            })
        return out
