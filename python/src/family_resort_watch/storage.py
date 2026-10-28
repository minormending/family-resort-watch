from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .bookcore import Availability, Hotel
from .flights import Option, Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS hotels (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    area TEXT NOT NULL,
    -- 1 = prices the party, 0 = rooms but none for the party (adults-only),
    -- NULL = not yet known (never checked, or sold out when checked)
    takes_party INTEGER,
    classified_at TEXT,
    classify_note TEXT,
    listed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    party TEXT NOT NULL
);

-- One row per (scan, hotel, stay) request, whatever came back.
CREATE TABLE IF NOT EXISTS fetches (
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    hotel_code TEXT NOT NULL,
    checkin TEXT NOT NULL,
    checkout TEXT NOT NULL,
    status TEXT NOT NULL,            -- ok | no_party_rooms | no_rooms | error
    message TEXT,
    room_types INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (scan_id, hotel_code, checkin, checkout)
);

CREATE TABLE IF NOT EXISTS offers (
    scan_id INTEGER NOT NULL,
    hotel_code TEXT NOT NULL,
    checkin TEXT NOT NULL,
    checkout TEXT NOT NULL,
    room_code TEXT NOT NULL,
    room_name TEXT NOT NULL,
    rate_code TEXT NOT NULL,
    rate_name TEXT NOT NULL,
    board TEXT NOT NULL,
    refundable INTEGER NOT NULL,
    free_cancel_until TEXT,
    public_total REAL NOT NULL,
    member_total REAL NOT NULL,
    rack_total REAL NOT NULL,
    inventory INTEGER,
    two_beds INTEGER NOT NULL,
    PRIMARY KEY (scan_id, hotel_code, checkin, checkout, room_code, rate_code, board)
);

-- One row per Google Flights search (one destination, one stay).
CREATE TABLE IF NOT EXISTS flight_fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dest TEXT NOT NULL,
    depart TEXT NOT NULL,
    ret TEXT NOT NULL,
    passengers TEXT NOT NULL,        -- what was searched, e.g. "3 adults"
    status TEXT NOT NULL,            -- ok | none | error
    results INTEGER,                 -- raw itineraries Google returned
    error TEXT,
    fetched_at TEXT NOT NULL
);

-- The fares picked from one search: cheapest overall, nonstop, one stop.
CREATE TABLE IF NOT EXISTS flight_quotes (
    fetch_id INTEGER NOT NULL REFERENCES flight_fetches(id),
    kind TEXT NOT NULL,              -- any | nonstop | one_stop
    price INTEGER NOT NULL,          -- round trip, whole party, USD
    airlines TEXT NOT NULL,
    stops INTEGER NOT NULL,
    depart_airport TEXT NOT NULL,
    depart_time TEXT,
    arrive_time TEXT,
    PRIMARY KEY (fetch_id, kind)
);

-- Every eligible itinerary a search returned, cheapest first (rank 0), so
-- the page can filter by airport, airline and departure time.
CREATE TABLE IF NOT EXISTS flight_options (
    fetch_id INTEGER NOT NULL REFERENCES flight_fetches(id),
    rank INTEGER NOT NULL,
    price INTEGER NOT NULL,
    airlines TEXT NOT NULL,
    flight_numbers TEXT NOT NULL,
    stops INTEGER NOT NULL,
    depart_airport TEXT NOT NULL,
    depart_time TEXT,
    arrive_time TEXT,
    duration_minutes INTEGER,
    aircraft TEXT,
    legroom TEXT,
    arrive_airport TEXT,
    PRIMARY KEY (fetch_id, rank)
);

-- Whether each outbound's paired returns were looked up, and how it went.
-- The fare tier priced for each airline in one search (fares.py). `price` is
-- what the airline charges for its cheapest tier with free seat selection on
-- the looked-up pairing; `list_price` is Google's result price for the same
-- pairing. The gap was added to every option of that airline in the search.
-- source: lookup (done now), cached (an earlier lookup, same stay), route
-- (an earlier lookup on another stay of the route), none (no tier known).
CREATE TABLE IF NOT EXISTS flight_fares (
    fetch_id INTEGER NOT NULL,
    airline TEXT NOT NULL,
    list_price INTEGER NOT NULL,
    price INTEGER NOT NULL,
    tier TEXT,
    list_tier TEXT,
    basis TEXT NOT NULL,             -- seat | not_basic | airline | third_party | unknown
    source TEXT NOT NULL,
    looked_at TEXT,
    PRIMARY KEY (fetch_id, airline)
);

CREATE TABLE IF NOT EXISTS flight_return_lookups (
    fetch_id INTEGER NOT NULL,
    outbound_rank INTEGER NOT NULL,
    status TEXT NOT NULL,            -- ok | none | error
    error TEXT,
    PRIMARY KEY (fetch_id, outbound_rank)
);

-- Returns Google pairs with one outbound, each priced as that round trip.
CREATE TABLE IF NOT EXISTS flight_returns (
    fetch_id INTEGER NOT NULL,
    outbound_rank INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    price INTEGER NOT NULL,
    airlines TEXT NOT NULL,
    flight_numbers TEXT NOT NULL,
    depart_airport TEXT NOT NULL,
    arrive_airport TEXT NOT NULL,
    depart_time TEXT,
    arrive_time TEXT,
    duration_minutes INTEGER,
    aircraft TEXT,
    legroom TEXT,
    PRIMARY KEY (fetch_id, outbound_rank, rank)
);

CREATE INDEX IF NOT EXISTS flight_fetches_by_route ON flight_fetches (dest, depart, ret, id);
CREATE INDEX IF NOT EXISTS offers_by_stay ON offers (hotel_code, checkin, checkout, scan_id);
CREATE INDEX IF NOT EXISTS fetches_by_stay ON fetches (hotel_code, checkin, checkout, scan_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # The hourly run and a manual flight sweep can overlap: WAL lets readers
    # carry on during a write, and the long timeout makes a second writer
    # wait its turn instead of failing with "database is locked".
    conn = sqlite3.connect(path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Columns added after a table first shipped; CREATE IF NOT EXISTS won't add them."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(flight_options)")}
    if "arrive_airport" not in have:
        conn.execute("ALTER TABLE flight_options ADD COLUMN arrive_airport TEXT")
    # "tier" once fares are priced at the airline's seat-selection tier; older
    # searches (NULL) priced the airline's bottom tier.
    have = {r[1] for r in conn.execute("PRAGMA table_info(flight_fetches)")}
    if "fare_basis" not in have:
        conn.execute("ALTER TABLE flight_fetches ADD COLUMN fare_basis TEXT")


def upsert_hotels(conn: sqlite3.Connection, hotels: list[Hotel]) -> None:
    ts = now()
    conn.executemany(
        """
        INSERT INTO hotels (code, name, country, area, listed_at) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name = excluded.name, country = excluded.country, area = excluded.area,
            listed_at = excluded.listed_at
        """,
        [(h.code, h.name, h.country, h.area, ts) for h in hotels],
    )


def set_classification(conn: sqlite3.Connection, code: str, takes_party: bool | None, note: str) -> None:
    conn.execute(
        "UPDATE hotels SET takes_party = ?, classified_at = ?, classify_note = ? WHERE code = ?",
        (None if takes_party is None else int(takes_party), now(), note, code),
    )


def start_scan(conn: sqlite3.Connection, party_code: str) -> int:
    cur = conn.execute("INSERT INTO scans (started_at, party) VALUES (?, ?)", (now(), party_code))
    return int(cur.lastrowid)


def finish_scan(conn: sqlite3.Connection, scan_id: int) -> None:
    conn.execute("UPDATE scans SET finished_at = ? WHERE id = ?", (now(), scan_id))


def record(
    conn: sqlite3.Connection,
    scan_id: int,
    hotel_code: str,
    checkin: str,
    checkout: str,
    result: Availability | None,
    error: str | None = None,
) -> None:
    if result is None:
        conn.execute(
            "INSERT OR REPLACE INTO fetches VALUES (?, ?, ?, ?, 'error', ?, NULL, ?)",
            (scan_id, hotel_code, checkin, checkout, error, now()),
        )
        return
    conn.execute(
        "INSERT OR REPLACE INTO fetches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (scan_id, hotel_code, checkin, checkout, result.status, result.message, result.room_types, now()),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO offers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                scan_id, hotel_code, checkin, checkout, o.room_code, o.room_name, o.rate_code,
                o.rate_name, o.board, int(o.refundable), o.free_cancel_until, o.public_total,
                o.member_total, o.rack_total, o.inventory, int(o.two_beds),
            )
            for o in result.offers
        ],
    )


def last_fetched_at(conn: sqlite3.Connection, checkin: str, checkout: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM fetches WHERE checkin = ? AND checkout = ? AND status != 'error'",
        (checkin, checkout),
    ).fetchone()
    return row[0]


def prune(conn: sqlite3.Connection, keep_days: int = 14) -> int:
    """Thin offers from scans older than `keep_days`.

    Each old fetch keeps only the rows the history needs: its cheapest offer
    overall, cheapest refundable and cheapest two-bed. The series the report
    draws stays exactly the same; per-room detail survives only for recent
    scans.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    # Flight options and returns only feed the page's filters, which read the latest search.
    for table in ("flight_options", "flight_returns", "flight_return_lookups"):
        conn.execute(
            f"DELETE FROM {table} WHERE fetch_id IN (SELECT id FROM flight_fetches WHERE fetched_at < ?)",
            (cutoff,),
        )
    cur = conn.execute(
        """
        DELETE FROM offers
        WHERE scan_id IN (SELECT id FROM scans WHERE started_at < ?)
          AND rowid NOT IN (
            SELECT rowid FROM (
              SELECT rowid,
                ROW_NUMBER() OVER w_all AS r_all,
                ROW_NUMBER() OVER w_ref AS r_ref,
                ROW_NUMBER() OVER w_two AS r_two,
                refundable, two_beds
              FROM offers
              WHERE scan_id IN (SELECT id FROM scans WHERE started_at < ?)
              WINDOW
                w_all AS (PARTITION BY scan_id, hotel_code, checkin, checkout ORDER BY member_total, room_code, rate_code),
                w_ref AS (PARTITION BY scan_id, hotel_code, checkin, checkout, refundable ORDER BY member_total, room_code, rate_code),
                w_two AS (PARTITION BY scan_id, hotel_code, checkin, checkout, two_beds ORDER BY member_total, room_code, rate_code)
            )
            WHERE r_all = 1 OR (refundable = 1 AND r_ref = 1) OR (two_beds = 1 AND r_two = 1)
          )
        """,
        (cutoff, cutoff),
    )
    return cur.rowcount


def record_flights(
    conn: sqlite3.Connection,
    dest: str,
    depart: str,
    ret: str,
    passengers: str,
    results: int | None,
    quotes: dict[str, Quote],
    error: str | None = None,
    options: list[Option] | None = None,
    fare_basis: str | None = None,
) -> int:
    status = "error" if error else ("ok" if quotes else "none")
    cur = conn.execute(
        "INSERT INTO flight_fetches (dest, depart, ret, passengers, status, results, error, fetched_at, fare_basis)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (dest, depart, ret, passengers, status, results, error, now(), fare_basis),
    )
    fetch_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO flight_quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (fetch_id, q.kind, q.price, q.airlines, q.stops, q.depart_airport, q.depart_time, q.arrive_time)
            for q in quotes.values()
        ],
    )
    conn.executemany(
        "INSERT INTO flight_options (fetch_id, rank, price, airlines, flight_numbers, stops, depart_airport,"
        " depart_time, arrive_time, duration_minutes, aircraft, legroom, arrive_airport)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (fetch_id, rank, o.price, o.airlines, o.flight_numbers, o.stops, o.depart_airport,
             o.depart_time, o.arrive_time, o.duration_minutes, o.aircraft, o.legroom, o.arrive_airport)
            for rank, o in enumerate(options or [])
        ],
    )
    return fetch_id


def record_fares(conn: sqlite3.Connection, fetch_id: int, rows: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO flight_fares (fetch_id, airline, list_price, price, tier, list_tier, basis, source, looked_at)"
        " VALUES (:fetch_id, :airline, :list_price, :price, :tier, :list_tier, :basis, :source, :looked_at)",
        [{**r, "fetch_id": fetch_id} for r in rows],
    )


def last_fare(conn: sqlite3.Connection, dest: str, depart: str | None, ret: str | None, airline: str):
    """The latest looked-up tier for an airline on a route (and stay, when given)."""
    sql = (
        "SELECT ff.* FROM flight_fares ff JOIN flight_fetches f ON f.id = ff.fetch_id"
        " WHERE f.dest = ? AND ff.airline = ? AND ff.looked_at IS NOT NULL AND ff.basis != 'unknown'"
    )
    args: list = [dest, airline]
    if depart is not None:
        sql += " AND f.depart = ? AND f.ret = ?"
        args += [depart, ret]
    return conn.execute(sql + " ORDER BY ff.looked_at DESC LIMIT 1", args).fetchone()


def record_returns(
    conn: sqlite3.Connection, fetch_id: int, outbound_rank: int, returns: list[Option] | None, error: str | None = None
) -> None:
    status = "error" if error else ("ok" if returns else "none")
    conn.execute(
        "INSERT OR REPLACE INTO flight_return_lookups VALUES (?, ?, ?, ?)", (fetch_id, outbound_rank, status, error)
    )
    conn.executemany(
        "INSERT OR REPLACE INTO flight_returns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (fetch_id, outbound_rank, rank, r.price, r.airlines, r.flight_numbers, r.depart_airport,
             r.arrive_airport, r.depart_time, r.arrive_time, r.duration_minutes, r.aircraft, r.legroom)
            for rank, r in enumerate(returns or [])
        ],
    )


def last_flight_fetch(conn: sqlite3.Connection, dest: str, depart: str, ret: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM flight_fetches WHERE dest = ? AND depart = ? AND ret = ? AND status != 'error'",
        (dest, depart, ret),
    ).fetchone()
    return row[0]
