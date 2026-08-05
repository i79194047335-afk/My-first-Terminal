"""
Persist the ForexFactory economic calendar into a rolling CSV.

Why this exists
---------------
briefing/sources.py:fetch_calendar() already talks to ForexFactory, but it only
returns the next `window_hours` of events to the briefing prompt and never
stores them. The old data_loaders fetcher that *did* write the CSV points at
ff_calendar_nextweek.json, which ForexFactory removed — it has been logging
HTTP 404 ever since, leaving data_loaders/news_calendar.csv frozen on
2026-07-14..15.

Only ff_calendar_thisweek.json still exists, so a single fetch can never cover
more than the current week. This script therefore MERGES each run into the CSV
instead of overwriting it: run it on cron and the file accumulates history
week over week, which is what a news-exclusion window needs.

Dedup key is (ts_utc, currency, event) — re-running mid-week is harmless.

Run: python3.10 data_loaders/sync_calendar.py [--dry-run]
Cron: 0 */6 * * * cd /root/projects/terminal && python3.10 data_loaders/sync_calendar.py >> data_loaders/fetch_news.log 2>&1
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# Only thisweek exists; nextweek/thismonth return 404 on this host.
FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Currencies of the four FXCM pairs we trade.
CALENDAR_CURRENCIES = {"USD", "JPY", "EUR", "AUD", "CAD"}

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_calendar.csv")
FIELDS = ["ts_utc", "datetime_utc", "currency", "impact", "event"]


def log(msg):
    """Print a timestamped line for the cron log.

    Args:
        msg: Message body.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print("[calendar {}] {}".format(stamp, msg))


# ForexFactory rate-limits this endpoint hard: two fetches a minute apart still
# return 429, and it stayed limited for ~5 minutes when measured on 2026-08-04.
# Cron runs unattended, so retry slowly rather than skipping a whole slot.
RETRY_DELAYS = [60, 120, 180, 300]


def _download():
    """Fetch the raw calendar JSON, retrying through rate limits.

    Returns:
        Tuple (data, error): parsed JSON list, or (None, reason) on failure.
    """
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                FF_CALENDAR_URL, headers={"User-Agent": "Mozilla/5.0 (terminal-calendar)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except Exception as err:
            is_last = attempt == attempts - 1
            if is_last:
                return None, repr(err)
            delay = RETRY_DELAYS[attempt]
            log("attempt {}/{} failed ({}), retrying in {}s".format(
                attempt + 1, attempts, err, delay))
            time.sleep(delay)
    return None, "unreachable"


def fetch_events():
    """Download this week's high-impact events for our currencies.

    Returns:
        Tuple (events, error): events is a list of row dicts using FIELDS;
        error is None on success or a string describing the failure.
    """
    data, error = _download()
    if error:
        return [], error

    rows = []
    for entry in data:
        if str(entry.get("impact", "")).strip().lower() != "high":
            continue
        currency = str(entry.get("country", "")).strip().upper()
        if currency not in CALENDAR_CURRENCIES:
            continue
        date_str = entry.get("date")
        if not date_str:
            continue
        try:
            # Feed dates carry an offset, e.g. 2026-08-02T05:15:00-04:00.
            ts = int(datetime.fromisoformat(date_str).timestamp())
        except (TypeError, ValueError):
            continue
        rows.append({
            "ts_utc": ts,
            "datetime_utc": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "currency": currency,
            "impact": "High",
            "event": str(entry.get("title", "")).strip(),
        })
    return rows, None


def read_existing(path):
    """Load already-stored rows, tolerating the legacy 4-column layout.

    Args:
        path: CSV file path; may be missing.

    Returns:
        List of row dicts normalised to FIELDS.
    """
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r") as fh:
        for row in csv.DictReader(fh):
            ts_raw = (row.get("ts_utc") or "").strip()
            if not ts_raw.isdigit():
                continue
            ts = int(ts_raw)
            out.append({
                "ts_utc": ts,
                # The old file had no datetime_utc column — derive it.
                "datetime_utc": (row.get("datetime_utc")
                                 or datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")),
                "currency": (row.get("currency") or "").strip().upper(),
                "impact": (row.get("impact") or "High").strip().capitalize(),
                "event": (row.get("event") or "").strip(),
            })
    return out


def merge(existing, fresh):
    """Merge fetched rows into stored ones, de-duplicating.

    Args:
        existing: Rows already in the CSV.
        fresh: Rows from this fetch.

    Returns:
        Tuple (merged_rows_sorted, added_count).
    """
    seen = {(r["ts_utc"], r["currency"], r["event"]): r for r in existing}
    added = 0
    for row in fresh:
        key = (row["ts_utc"], row["currency"], row["event"])
        if key not in seen:
            seen[key] = row
            added += 1
    merged = sorted(seen.values(), key=lambda r: (r["ts_utc"], r["currency"], r["event"]))
    return merged, added


def write_csv(path, rows):
    """Write rows atomically so a crashed run cannot truncate the calendar.

    Args:
        path: Destination CSV path.
        rows: Row dicts using FIELDS.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, path)


def main():
    """Fetch this week's calendar and merge it into the rolling CSV."""
    dry_run = "--dry-run" in sys.argv

    fresh, error = fetch_events()
    if error:
        log("fetch FAILED: {} (calendar left untouched)".format(error))
        return 1
    log("fetched {} high-impact events for {}".format(
        len(fresh), "/".join(sorted(CALENDAR_CURRENCIES))))

    existing = read_existing(CSV_PATH)
    merged, added = merge(existing, fresh)

    if dry_run:
        log("dry run: {} stored + {} new = {} total (nothing written)".format(
            len(existing), added, len(merged)))
        return 0

    write_csv(CSV_PATH, merged)
    span = ""
    if merged:
        span = " spanning {} .. {}".format(merged[0]["datetime_utc"], merged[-1]["datetime_utc"])
    log("wrote {} rows (+{} new){}".format(len(merged), added, span))
    return 0


if __name__ == "__main__":
    sys.exit(main())
