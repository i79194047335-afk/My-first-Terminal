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

# UTC clock times that reshape liquidity. FXCM rolls its trading day at 21:00
# UTC (see the H4/D1 grid note in CLAUDE.md), which is also the thinnest book.
SESSION_EVENTS_UTC = [
    (21, 0, "rollover / Sydney open"),
    (0, 0, "Tokyo open"),
    (7, 0, "London open"),
    (12, 30, "US data slot"),
    (13, 30, "NY open"),
    (20, 0, "NY close"),
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
            Label of the session event, or None.
        """
        dt = datetime.fromtimestamp(ts, timezone.utc)
        for hh, mm, label in SESSION_EVENTS_UTC:
            event = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
            # Check neighbouring days so windows spanning midnight still match.
            for shifted in (event - timedelta(days=1), event, event + timedelta(days=1)):
                if abs((dt - shifted).total_seconds()) <= SESSION_WINDOW_MIN * 60:
                    return label
        return None

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

        session = self.session_window(ts)
        if session:
            return {
                "kind": "session",
                "label": session,
                "until_ts": None,
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
