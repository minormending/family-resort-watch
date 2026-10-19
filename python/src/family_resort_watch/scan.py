from __future__ import annotations

import gzip
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from . import bookcore, config, storage
from .config import Config, Window

log = logging.getLogger(__name__)

# A flex window counts as due slightly early so an hourly cadence with a few
# minutes of drift does not skip a whole cycle.
DUE_SLACK = timedelta(minutes=10)
# One lock for every trip, so two trips' sweeps never hit Google at once.


def _fetch(cfg: Config, code: str, window: Window):
    try:
        raw = bookcore.fetch_availability_raw(
            code, window.checkin, window.checkout, cfg.party, cfg.timeout_seconds, cfg.retries
        )
    except bookcore.FetchError as exc:
        return code, window, None, str(exc)
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RAW_DIR / f"{code}_{window.checkin}_{window.checkout}.json.gz"
    path.write_bytes(gzip.compress(json.dumps(raw).encode()))
    return code, window, bookcore.parse_availability(raw, cfg.party), None


def classify(conn: sqlite3.Connection, cfg: Config, force: bool = False) -> dict[str, int]:
    """Refresh the hotel list and work out which in-scope resorts price a child."""
    storage.upsert_hotels(conn, bookcore.fetch_hotel_list(cfg.timeout_seconds, cfg.retries))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg.reclassify_days)).isoformat()
    placeholders = ",".join("?" * len(cfg.countries))
    rows = conn.execute(
        f"""
        SELECT code FROM hotels WHERE country IN ({placeholders})
          AND (? OR classified_at IS NULL OR classified_at < ?)
        ORDER BY code
        """,
        (*sorted(cfg.countries), int(force), cutoff),
    ).fetchall()
    codes = [r["code"] for r in rows]
    counts = {"family": 0, "adults_only": 0, "unknown": 0}
    if not codes:
        return counts
    log.info("classifying %d resorts", len(codes))
    probe = cfg.primary[0]
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        results = list(pool.map(lambda c: _fetch(cfg, c, probe), codes))
    for code, _, result, error in results:
        if result is None:
            storage.set_classification(conn, code, None, f"probe failed: {error}")
            counts["unknown"] += 1
        elif result.status == "ok":
            storage.set_classification(conn, code, True, f"{len(result.offers)} offers for party")
            counts["family"] += 1
        elif result.status == "no_party_rooms":
            storage.set_classification(conn, code, False, result.message)
            counts["adults_only"] += 1
        else:
            storage.set_classification(conn, code, None, result.message or "no rooms returned")
            counts["unknown"] += 1
    return counts


def due_windows(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> list[Window]:
    now = now or datetime.now(timezone.utc)
    due = []
    for w in cfg.windows:
        if w.tier == "primary":
            due.append(w)
            continue
        last = storage.last_fetched_at(conn, w.checkin, w.checkout)
        if last is None or datetime.fromisoformat(last) + timedelta(hours=w.every_hours) - DUE_SLACK <= now:
            due.append(w)
    return due


def scan(conn: sqlite3.Connection, cfg: Config, only_windows: list[Window] | None = None) -> int:
    windows = only_windows or due_windows(conn, cfg)
    placeholders = ",".join("?" * len(cfg.countries))
    codes = [
        r["code"]
        for r in conn.execute(
            f"""SELECT code FROM hotels WHERE country IN ({placeholders})
                AND (takes_party IS NULL OR takes_party = 1) ORDER BY code""",
            tuple(sorted(cfg.countries)),
        )
    ]
    if not codes:
        raise RuntimeError("no resorts to scan; run `classify` first")
    scan_id = storage.start_scan(conn, cfg.party.code)
    jobs = [(c, w) for w in windows for c in codes]
    log.info("scan %d: %d resorts x %d stays = %d requests", scan_id, len(codes), len(windows), len(jobs))
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        for i, (code, window, result, error) in enumerate(pool.map(lambda j: _fetch(cfg, *j), jobs), 1):
            storage.record(conn, scan_id, code, window.checkin, window.checkout, result, error)
            # Commit as we go so a concurrent flight sweep isn't locked out for the whole scan.
            if i % 25 == 0:
                conn.commit()
            if result is not None and result.status in ("ok", "no_party_rooms"):
                known = conn.execute("SELECT takes_party FROM hotels WHERE code = ?", (code,)).fetchone()
                if known["takes_party"] is None:
                    storage.set_classification(
                        conn, code, result.status == "ok", f"learned during scan {scan_id}"
                    )
    storage.finish_scan(conn, scan_id)
    conn.commit()
    return scan_id


def flight_destinations(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    """Every airport serving an in-scope resort that takes the party."""
    placeholders = ",".join("?" * len(cfg.countries))
    areas = {
        r["area"]
        for r in conn.execute(
            f"SELECT DISTINCT area FROM hotels WHERE country IN ({placeholders}) AND takes_party = 1",
            tuple(sorted(cfg.countries)),
        )
    }
    return sorted({code for area in areas for code in cfg.airports.get(area, ())})


def due_flights(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> list[tuple[str, Window]]:
    now = now or datetime.now(timezone.utc)
    s = cfg.flights
    due = []
    for w in cfg.windows:
        every = timedelta(hours=s.every_hours if w.tier == "primary" else s.flex_every_hours)
        for dest in flight_destinations(conn, cfg):
            last = storage.last_flight_fetch(conn, dest, w.checkin, w.checkout)
            if last is None or datetime.fromisoformat(last) + every - DUE_SLACK <= now:
                due.append((dest, w))
    return due


def _with_returns(s, flights, dest: str, w: Window, opts: list, counts: dict) -> tuple[list, dict]:
    """Look up each outbound's paired returns and price it by its cheapest eligible one.

    An outbound whose returns all land at an airport we don't use (not a home airport) is
    dropped. One past the lookup cap, or whose lookup failed, keeps Google's
    listed price. Returns (re-sorted outbounds, {outbound: (returns, error)}).
    """
    found: dict = {}
    priced = []
    for i, o in enumerate(opts):
        if i >= s.return_lookups_per_route:
            priced.append(o)
            continue
        flights.pause(s)
        counts["return_lookups"] += 1
        try:
            rets = flights.returns_for(s, dest, w.checkin, w.checkout, o)
        except flights.FlightError as exc:
            log.warning("%s", exc)
            counts["return_errors"] += 1
            priced.append(o)
            found[o] = (None, str(exc))
            continue
        if not rets:
            log.info("%s %s: no return to a home airport pairs with %s", dest, w.checkin, o.flight_numbers)
            continue
        o = replace(o, price=rets[0].price)
        priced.append(o)
        found[o] = (rets, None)
    priced.sort(key=lambda o: (o.price, o.depart_time, o.airlines, o.flight_numbers))
    return priced, found


def scan_flights(conn: sqlite3.Connection, cfg: Config, force: bool = False) -> dict[str, int]:
    """Search every due route one at a time, with a jittered pause between.

    A full sweep takes ~10 minutes, so an hourly run can start while one is
    going; the lock makes the later one skip flights instead of doubling up.
    """
    import fcntl

    from . import flights

    s = cfg.flights
    if s is None:
        return {}
    # One lock per home, so several trips' hourly runs never sweep at once.
    lock_path = config.HOME / "flights.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("flights: another flight scan is running; skipping")
        lock.close()
        return {"skipped": 1}
    try:
        return _scan_flights(conn, cfg, s, force, flights)
    finally:
        lock.close()


def _scan_flights(conn, cfg: Config, s, force: bool, flights) -> dict[str, int]:
    jobs = (
        [(d, w) for w in cfg.windows for d in flight_destinations(conn, cfg)]
        if force
        else due_flights(conn, cfg)
    )
    counts = {"ok": 0, "none": 0, "error": 0, "return_lookups": 0, "return_errors": 0}
    if not jobs:
        return counts
    log.info("flights: %d searches", len(jobs))
    passengers = f"{s.proxy_adults} adults"
    for i, (dest, w) in enumerate(jobs):
        if i:
            flights.pause(s)
        try:
            results, opts = flights.search(s, dest, w.checkin, w.checkout)
            opts, returns = _with_returns(s, flights, dest, w, opts, counts)
            quotes = flights.pick(opts)
            fetch_id = storage.record_flights(conn, dest, w.checkin, w.checkout, passengers, results, quotes, options=opts)
            for rank, o in enumerate(opts):
                if o in returns:
                    storage.record_returns(conn, fetch_id, rank, *returns[o])
            counts["ok" if quotes else "none"] += 1
        except flights.FlightError as exc:
            log.warning("%s", exc)
            storage.record_flights(conn, dest, w.checkin, w.checkout, passengers, None, {}, str(exc))
            counts["error"] += 1
        conn.commit()
    return counts
