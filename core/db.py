"""
SQLite storage layer for candle history.

Schema:
  - candles (WITHOUT ROWID): (provider, symbol, tf, time) as PK, OHLC, volumes.
  - instruments: per-symbol metadata (decimals, min_base, has_volume, raw meta).

All times are Unix SECONDS. Volumes are nullable (FXCM doesn't provide them).
"""

import sqlite3
import os

KEEP_BARS = 2000

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS candles (
    provider  TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    tf        TEXT NOT NULL,
    time      INTEGER NOT NULL,   -- unix seconds
    o         REAL NOT NULL,
    h         REAL NOT NULL,
    l         REAL NOT NULL,
    c         REAL NOT NULL,
    vol_base  REAL,
    vol_quote REAL,
    PRIMARY KEY (provider, symbol, tf, time)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS instruments (
    provider       TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    price_decimals INTEGER,
    size_decimals  INTEGER,
    min_base       REAL,
    has_volume     INTEGER,       -- 0/1
    meta           TEXT,           -- raw JSON from provider
    updated        INTEGER,       -- unix seconds
    PRIMARY KEY (provider, symbol)
);
"""


def init_db(path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite database and ensure schema exists.

    Args:
        path: Filesystem path to the .db file.

    Returns:
        A sqlite3.Connection in WAL mode with candles + instruments tables ready.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def upsert_candle(conn: sqlite3.Connection, provider: str, symbol: str,
                  tf: str, candle: dict) -> None:
    """Insert or replace a single candle.

    Idempotent — re-running the same (provider, symbol, tf, time) overwrites
    the row rather than creating a duplicate.

    Args:
        conn:     Open SQLite connection.
        provider: Feed identifier (e.g. "fxcm", "lighter").
        symbol:   Trading pair (e.g. "EUR/USD", "BTC").
        tf:       Timeframe string (e.g. "M1", "H1").
        candle:   Dict with keys time, open, high, low, close and optional
                  vol_base, vol_quote.
    """
    conn.execute(
        """INSERT OR REPLACE INTO candles
           (provider, symbol, tf, time, o, h, l, c, vol_base, vol_quote)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            provider, symbol, tf,
            candle["time"],
            candle["open"],
            candle["high"],
            candle["low"],
            candle["close"],
            candle.get("vol_base"),
            candle.get("vol_quote"),
        ),
    )


def upsert_candles_batch(conn: sqlite3.Connection, provider: str, symbol: str,
                         tf: str, candles: list) -> None:
    """Insert or replace a list of candles in a single transaction.

    Args:
        conn:     Open SQLite connection.
        provider: Feed identifier.
        symbol:   Trading pair.
        tf:       Timeframe string.
        candles:  List of candle dicts (see upsert_candle).
    """
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO candles
               (provider, symbol, tf, time, o, h, l, c, vol_base, vol_quote)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    provider, symbol, tf,
                    c["time"], c["open"], c["high"], c["low"], c["close"],
                    c.get("vol_base"), c.get("vol_quote"),
                )
                for c in candles
            ],
        )


def load_history(conn: sqlite3.Connection, provider: str, symbol: str,
                 tf: str) -> list:
    """Read all persisted candles for a given pair+timeframe, oldest first.

    Args:
        conn:     Open SQLite connection.
        provider: Feed identifier.
        symbol:   Trading pair.
        tf:       Timeframe string.

    Returns:
        List of candle dicts with keys: time, open, high, low, close,
        vol_base, vol_quote.
    """
    rows = conn.execute(
        """SELECT time, o, h, l, c, vol_base, vol_quote
           FROM candles
           WHERE provider=? AND symbol=? AND tf=?
           ORDER BY time ASC""",
        (provider, symbol, tf),
    ).fetchall()

    return [
        {
            "time":  row[0],
            "open":  row[1],
            "high":  row[2],
            "low":   row[3],
            "close": row[4],
            "vol_base":  row[5],
            "vol_quote": row[6],
        }
        for row in rows
    ]


def get_candle_count(conn: sqlite3.Connection, provider: str, symbol: str,
                     tf: str) -> int:
    """Return the number of stored candles for a pair+timeframe.

    Args:
        conn:     Open SQLite connection.
        provider: Feed identifier.
        symbol:   Trading pair.
        tf:       Timeframe string.

    Returns:
        Integer count of rows.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE provider=? AND symbol=? AND tf=?",
        (provider, symbol, tf),
    ).fetchone()
    return row[0] if row else 0


def trim_window(conn: sqlite3.Connection, provider: str, symbol: str,
                tf: str, keep_bars: int = KEEP_BARS) -> int:
    """Delete candles beyond the sliding window, keeping the most recent N bars.

    Should be called after a CLOSED candle is written (not on every tick).

    Args:
        conn:      Open SQLite connection.
        provider:  Feed identifier.
        symbol:    Trading pair.
        tf:        Timeframe string.
        keep_bars: Number of most-recent bars to retain (default KEEP_BARS).

    Returns:
        Number of rows deleted.
    """
    if keep_bars < 1:
        raise ValueError(f"keep_bars must be >= 1, got {keep_bars}")

    # Find the timestamp of the keep_bars-th most recent candle.
    # This is the oldest candle we want to KEEP.
    cutoff_row = conn.execute(
        """SELECT time FROM candles
           WHERE provider=? AND symbol=? AND tf=?
           ORDER BY time DESC LIMIT 1 OFFSET ?""",
        (provider, symbol, tf, keep_bars - 1),
    ).fetchone()

    if cutoff_row is None:
        return 0  # window not full yet

    cutoff_time = cutoff_row[0]
    cursor = conn.execute(
        """DELETE FROM candles
           WHERE provider=? AND symbol=? AND tf=? AND time < ?""",
        (provider, symbol, tf, cutoff_time),
    )
    return cursor.rowcount


def vacuum(conn: sqlite3.Connection) -> None:
    """Reclaim disk space after large deletions. Call sparingly (weekly cron).

    Args:
        conn: Open SQLite connection.
    """
    conn.commit()          # VACUUM cannot run inside a transaction
    conn.execute("VACUUM")


def upsert_instrument(conn: sqlite3.Connection, provider: str, symbol: str,
                      price_decimals: int = None, size_decimals: int = None,
                      min_base: float = None, has_volume: bool = False,
                      meta: dict = None, updated: int = None) -> None:
    """Insert or update instrument metadata.

    Args:
        conn:           Open SQLite connection.
        provider:       Feed identifier.
        symbol:         Trading pair.
        price_decimals: Decimal places for price display.
        size_decimals:  Decimal places for size display.
        min_base:       Minimum order size in base units.
        has_volume:     Whether the feed provides trade volume.
        meta:           Arbitrary provider metadata (stored as JSON).
        updated:        Unix timestamp of last update.
    """
    import json
    conn.execute(
        """INSERT OR REPLACE INTO instruments
           (provider, symbol, price_decimals, size_decimals,
            min_base, has_volume, meta, updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            provider, symbol,
            price_decimals, size_decimals,
            min_base, 1 if has_volume else 0,
            json.dumps(meta) if meta else None,
            updated,
        ),
    )


def get_instrument(conn: sqlite3.Connection, provider: str,
                   symbol: str) -> dict:
    """Read instrument metadata.

    Args:
        conn:     Open SQLite connection.
        provider: Feed identifier.
        symbol:   Trading pair.

    Returns:
        Dict with keys matching the instruments table, or None if not found.
    """
    import json
    row = conn.execute(
        """SELECT provider, symbol, price_decimals, size_decimals,
                  min_base, has_volume, meta, updated
           FROM instruments WHERE provider=? AND symbol=?""",
        (provider, symbol),
    ).fetchone()

    if row is None:
        return None

    return {
        "provider":       row[0],
        "symbol":         row[1],
        "price_decimals": row[2],
        "size_decimals":  row[3],
        "min_base":       row[4],
        "has_volume":     bool(row[5]),
        "meta":           json.loads(row[6]) if row[6] else None,
        "updated":        row[7],
    }
