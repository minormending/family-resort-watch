"""Turn the database into the documents the report page reads.

Everything here is a pure function of the database and the config files, so
the same database always yields byte-identical output (apart from
`generated_at`). The hourly session adds judgement on top; it never computes
prices.

Outputs, all under report/:

* db/latest.json        -> artifact db doc sbw/latest   (current state, events)
* db/hist-<stay>.json   -> artifact db doc sbw/hist-... (price change points)
* db/flights.json       -> artifact db doc sbw/flights (current fares per route)
* db/flights-hist.json  -> artifact db doc sbw/flights-hist (fare change points)
* db/flights-options.json -> artifact db doc sbw/flights-options (every option, for filters)
* db/reviews.json       -> artifact db doc sbw/reviews (one-time family-fit research)
* db/trends.json        -> artifact db doc sbw/trends (each stay over time, all resorts together)
* changes.md            what the session reads to write its analysis
* publish_plan.json     which db docs changed since the last publish
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bookcore, config, reviews, trends
from .config import Config, Window

# Change points kept per resort and stay; older ones fall off.
HISTORY_POINTS = 150
# An offer with this many rooms or fewer left counts as scarce.
SCARCE_INVENTORY = 3
DOC_COLLECTION = "sbw"


def _epoch(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp())


def _money(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def _brand(name: str, brands: dict) -> tuple[str, dict]:
    match = max((b for b in brands if name.startswith(b)), key=len, default="")
    return match, brands.get(match, {})


def _stay_doc_id(w: Window) -> str:
    return f"hist-{w.checkin}_{w.checkout}"


def _series(conn: sqlite3.Connection) -> dict[tuple[str, str, str], list[dict]]:
    """Every non-error fetch, oldest first, with its cheapest member prices."""
    rows = conn.execute(
        """
        SELECT f.hotel_code, f.checkin, f.checkout, f.scan_id, f.fetched_at, f.status,
               MIN(o.member_total) AS best,
               MIN(CASE WHEN o.refundable = 1 THEN o.member_total END) AS best_refundable,
               MIN(CASE WHEN o.two_beds = 1 THEN o.member_total END) AS best_two_beds
        FROM fetches f
        LEFT JOIN offers o
          ON o.scan_id = f.scan_id AND o.hotel_code = f.hotel_code
         AND o.checkin = f.checkin AND o.checkout = f.checkout
        WHERE f.status != 'error'
        GROUP BY f.scan_id, f.hotel_code, f.checkin, f.checkout
        ORDER BY f.scan_id
        """
    ).fetchall()
    out: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        out[(r["hotel_code"], r["checkin"], r["checkout"])].append(dict(r))
    return out


def _cheapest(
    conn: sqlite3.Connection, scan_id: int, code: str, w: Window, where: str = "", full: bool = True
) -> dict | None:
    r = conn.execute(
        f"""
        SELECT room_name, rate_name, refundable, free_cancel_until, member_total, public_total,
               rack_total, inventory, two_beds
        FROM offers
        WHERE scan_id = ? AND hotel_code = ? AND checkin = ? AND checkout = ? {where}
        ORDER BY member_total, refundable DESC, room_code
        LIMIT 1
        """,
        (scan_id, code, w.checkin, w.checkout),
    ).fetchone()
    if r is None:
        return None
    if not full:
        # The alternatives (refundable, two beds) only need enough to compare.
        return {
            "room": r["room_name"],
            "member_total": _money(r["member_total"]),
            "free_cancel_until": r["free_cancel_until"],
        }
    return {
        "room": r["room_name"],
        "rate": r["rate_name"],
        "refundable": bool(r["refundable"]),
        "free_cancel_until": r["free_cancel_until"],
        "member_total": _money(r["member_total"]),
        "public_total": _money(r["public_total"]),
        "rack_total": _money(r["rack_total"]),
        "inventory": r["inventory"],
        "two_beds": bool(r["two_beds"]),
    }


def _stay(conn, code: str, w: Window, fetches: list[dict], now: datetime) -> dict | None:
    if not fetches:
        return None
    last = fetches[-1]
    priced = [f for f in fetches if f["best"] is not None]
    prev = fetches[-2] if len(fetches) > 1 else None
    day_ago = now - timedelta(hours=24)
    older = [f for f in fetches[:-1] if datetime.fromisoformat(f["fetched_at"]) <= day_ago]
    ref_24h = older[-1] if older else None
    low = min(priced, key=lambda f: (f["best"], f["scan_id"])) if priced else None

    def delta(other):
        if other is None or other["best"] is None or last["best"] is None:
            return None
        return _money(last["best"] - other["best"])

    return {
        "status": last["status"],
        "fetched_at": last["fetched_at"],
        "observations": len(fetches),
        "cheapest": _cheapest(conn, last["scan_id"], code, w),
        "cheapest_refundable": _cheapest(conn, last["scan_id"], code, w, "AND refundable = 1", full=False),
        "cheapest_two_beds": _cheapest(conn, last["scan_id"], code, w, "AND two_beds = 1", full=False),
        "previous": _money(prev["best"]) if prev else None,
        "delta": delta(prev),
        "delta_24h": delta(ref_24h),
        "low": {"member_total": _money(low["best"]), "at": low["fetched_at"]} if low else None,
    }


def _points(category: str, award: dict, party, nights: int, cash: float | None) -> dict | None:
    chart = award.get("chart", {})
    if category not in chart:
        return None
    lowest, top = chart[category][0], chart[category][-1]
    extra = max(0, party.adults + sum(1 for a in party.child_ages if a >= 3) - 2)
    factor = 1 + award.get("extra_guest_factor", 0.5) * extra
    stay = [round(lowest * factor * nights), round(top * factor * nights)]
    return {
        "category": category,
        "stay_points": stay,
        # cents per point at each end of the range, against the cash price
        "cpp": [round(cash * 100 / stay[1], 2), round(cash * 100 / stay[0], 2)] if cash else None,
    }


FLIGHT_HISTORY_POINTS = 100


def family_fare(quotes: dict[str, dict], max_stops: int | None) -> dict | None:
    """The fare a trip total uses: fewest stops first, never more than `max_stops`."""
    allowed = {0: ("nonstop",), 1: ("nonstop", "one_stop")}.get(max_stops, ("nonstop", "one_stop", "any"))
    for kind in allowed:
        if kind in quotes:
            return {"price": quotes[kind]["price"], "kind": kind}
    return None


def _flight_series(conn: sqlite3.Connection, max_stops: int | None) -> dict[tuple[str, str, str], list[dict]]:
    """Every non-error flight search, oldest first, with its picked fares."""
    quotes: dict[int, dict[str, dict]] = defaultdict(dict)
    for q in conn.execute("SELECT * FROM flight_quotes"):
        quotes[q["fetch_id"]][q["kind"]] = {
            "price": q["price"], "airlines": q["airlines"], "stops": q["stops"],
            "depart_airport": q["depart_airport"], "depart_time": q["depart_time"], "arrive_time": q["arrive_time"],
        }
    fares: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in conn.execute("SELECT * FROM flight_fares"):
        fares[r["fetch_id"]][r["airline"]] = {
            "tier": r["tier"], "list_tier": r["list_tier"], "extra": r["price"] - r["list_price"],
            "basis": r["basis"], "source": r["source"], "looked_at": r["looked_at"],
        }
    out: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for f in conn.execute("SELECT * FROM flight_fetches WHERE status != 'error' ORDER BY id"):
        q = quotes.get(f["id"], {})
        fam = family_fare(q, max_stops)
        out[(f["dest"], f["depart"], f["ret"])].append(
            {
                "id": f["id"], "fetched_at": f["fetched_at"], "status": f["status"], "passengers": f["passengers"],
                "quotes": q, "family": fam["price"] if fam else None, "any": q.get("any", {}).get("price"),
                "fare_basis": f["fare_basis"], "fares": fares.get(f["id"], {}),
            }
        )
    return out


def _since_tier_fares(fseries: dict) -> tuple[dict, str | None]:
    """Each route's searches from its first one priced at seat-selection tiers.

    Earlier searches priced the airline's bottom tier, so comparing across the
    switch would read as a fare rise and keep a low nobody can book. Returns
    (series, when the switch first happened).
    """
    out, first = {}, None
    for key, fetches in fseries.items():
        i = next((i for i, f in enumerate(fetches) if f["fare_basis"] == "tier"), None)
        out[key] = fetches if i is None else fetches[i:]
        if i is not None and (first is None or fetches[i]["fetched_at"] < first):
            first = fetches[i]["fetched_at"]
    return out, first


# Columns of each row in sbw/flights-options, kept as arrays to stay small.
# An option row ends with its returns: null when not looked up (or the lookup
# failed), otherwise a list of RETURN_COLUMNS rows, cheapest first.
OPTION_COLUMNS = ["price", "airlines", "flight_numbers", "depart_airport", "depart_time",
                  "arrive_time", "duration_minutes", "aircraft", "legroom"]
RETURN_COLUMNS = ["price", "airlines", "flight_numbers", "depart_time", "arrive_time",
                  "arrive_airport", "duration_minutes", "aircraft", "legroom"]
OPTIONS_PER_ROUTE = 25
RETURNS_PER_OPTION = 6


def _options_doc(conn: sqlite3.Connection, cfg: Config, fseries, dests: list[str], now: datetime) -> dict:
    """Every option from the latest search of each route, for the page's filters."""
    routes: dict[str, dict] = {}
    for dest in dests:
        for w in cfg.windows:
            fetches = fseries.get((dest, w.checkin, w.checkout), [])
            if not fetches:
                continue
            fetch_id = fetches[-1]["id"]
            rows = conn.execute(
                f"SELECT rank, {', '.join(OPTION_COLUMNS)} FROM flight_options WHERE fetch_id = ? ORDER BY rank LIMIT ?",
                (fetch_id, OPTIONS_PER_ROUTE),
            ).fetchall()
            looked = {
                r["outbound_rank"]: r["status"]
                for r in conn.execute("SELECT * FROM flight_return_lookups WHERE fetch_id = ?", (fetch_id,))
            }
            rets: dict[int, list] = defaultdict(list)
            for r in conn.execute(
                f"SELECT outbound_rank, {', '.join(RETURN_COLUMNS)} FROM flight_returns WHERE fetch_id = ?"
                " ORDER BY outbound_rank, rank",
                (fetch_id,),
            ):
                if len(rets[r["outbound_rank"]]) < RETURNS_PER_OPTION:
                    rets[r["outbound_rank"]].append(list(r)[1:])
            routes.setdefault(dest, {})[w.key] = [
                [*list(r)[1:], rets[r["rank"]] if looked.get(r["rank"]) in ("ok", "none") else None] for r in rows
            ]
    return {"schema": 2, "generated_at": now.replace(microsecond=0).isoformat(),
            "columns": OPTION_COLUMNS, "return_columns": RETURN_COLUMNS, "routes": routes}


def _cheapest_by_airline(options_doc: dict, dest: str, stay: str) -> list[dict]:
    """Each airline's cheapest option, with the return it is priced with."""
    cols, rcols = options_doc["columns"], options_doc.get("return_columns", [])
    best: dict[str, dict] = {}
    for row in (options_doc.get("routes", {}).get(dest) or {}).get(stay, []):
        r = dict(zip(cols, row))
        returns = row[len(cols)] if len(row) > len(cols) else None
        r["back"] = dict(zip(rcols, returns[0])) if returns else None
        best.setdefault(r["airlines"], r)
    return sorted(best.values(), key=lambda r: (r["price"], r["airlines"]))


def _flights_doc(cfg: Config, fseries, dests: list[str], now: datetime) -> tuple[dict, dict]:
    from . import flights

    s = cfg.flights
    routes: dict[str, dict] = {}
    hist: dict[str, list] = {}
    day_ago = now - timedelta(hours=24)
    for dest in dests:
        for w in cfg.windows:
            fetches = fseries.get((dest, w.checkin, w.checkout), [])
            if not fetches:
                continue
            last = fetches[-1]
            prev = fetches[-2] if len(fetches) > 1 else None
            older = [f for f in fetches[:-1] if datetime.fromisoformat(f["fetched_at"]) <= day_ago]
            priced = [f for f in fetches if f["family"] is not None]
            low = min(priced, key=lambda f: (f["family"], f["id"])) if priced else None

            def delta(other):
                if other is None or other["family"] is None or last["family"] is None:
                    return None
                return last["family"] - other["family"]

            routes.setdefault(dest, {})[w.key] = {
                "status": last["status"],
                "fetched_at": last["fetched_at"],
                "searched_as": last["passengers"],
                **{k: last["quotes"].get(k) for k in ("any", "nonstop", "one_stop")},
                "family": family_fare(last["quotes"], s.max_stops),
                "previous": prev["family"] if prev else None,
                "delta": delta(prev),
                "delta_24h": delta(older[-1] if older else None),
                "low": {"price": low["family"], "at": low["fetched_at"]} if low else None,
                "url": flights.google_flights_url(s, cfg.party, dest, w.checkin, w.checkout),
                # Per airline: the tier priced and what it adds to Google's price.
                "fares": last["fares"],
            }
            pts, seen = [], None
            for f in fetches:
                value = (f["family"], f["any"])
                if value != seen:
                    pts.append([_epoch(f["fetched_at"]), *value])
                    seen = value
            hist[f"{dest}|{w.key}"] = pts[-FLIGHT_HISTORY_POINTS:]
    doc = {
        "schema": 1,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "origins": list(s.origins),
        "departure_airports": list(s.departure_airports),
        "home_label": s.home_label,
        "searched_as": f"{s.proxy_adults} adults",
        "max_stops": s.max_stops,
        "fare_rule": (
            f"Nonstop only: a route with no nonstop from {s.home_label} has no fare."
            if s.max_stops == 0
            else "Fewest stops first, at most %s." % s.max_stops
        ),
        "fare_basis": (
            "Each airline's own price for its cheapest fare with free seat selection and a carry-on,"
            " from Google Flights' booking page; not third-party sites."
            if s.fare_tiers
            else "Each airline's own cheapest fare with a carry-on, as Google Flights lists it."
        ),
        "routes": routes,
    }
    return doc, {"schema": 1, "points": hist}


def _best_trip(resort: dict, routes: dict, windows: tuple[Window, ...]) -> dict | None:
    """Cheapest hotel + flight total over the given stays and the resort's airports."""
    best = None
    for w in windows:
        stay = resort["stays"].get(w.key) or {}
        hotel = (stay.get("cheapest") or {}).get("member_total")
        if hotel is None:
            continue
        for ap in resort["airports"]:
            route = (routes.get(ap) or {}).get(w.key) or {}
            fare = route.get("family")
            if not fare:
                continue
            total = round(hotel + fare["price"], 2)
            cand = {"stay": w.key, "airport": ap, "hotel": hotel, "flight": fare["price"],
                    "flight_kind": fare["kind"], "total": total}
            if best is None or (total, w.checkin, ap) < (best["total"], best["stay"], best["airport"]):
                best = cand
    return best


def _flight_events(fseries, dests: list[str], windows: tuple[Window, ...], since: str, home: str = "home") -> list[dict]:
    """Fare moves from searches made since `since` (flights run every few hours)."""
    events = []
    for dest in dests:
        for w in windows:
            fetches = fseries.get((dest, w.checkin, w.checkout), [])
            if len(fetches) < 2 or fetches[-1]["fetched_at"] < since:
                continue
            prev, last = fetches[-2], fetches[-1]
            base = {"code": dest, "name": f"Flights {home}–{dest}", "stay": w.key, "tier": w.tier}
            if prev["family"] is None or last["family"] is None or prev["family"] == last["family"]:
                continue
            d = last["family"] - prev["family"]
            events.append({**base, "type": "flight_drop" if d < 0 else "flight_rise", "from": prev["family"],
                           "to": last["family"], "delta": d, "pct": round(100 * d / prev["family"], 1)})
            earlier = [f["family"] for f in fetches[:-1] if f["family"] is not None]
            if len(earlier) >= 2 and last["family"] < min(earlier):
                events.append({**base, "type": "flight_new_low", "to": last["family"], "old_low": min(earlier)})
    return events


def _events(conn, resorts: list[dict], series, windows: tuple[Window, ...], scan_id: int) -> list[dict]:
    events = []
    for r in resorts:
        for w in windows:
            fetches = series.get((r["code"], w.checkin, w.checkout), [])
            if len(fetches) < 2 or fetches[-1]["scan_id"] != scan_id:
                continue
            prev, last = fetches[-2], fetches[-1]
            base = {"code": r["code"], "name": r["name"], "stay": w.key, "tier": w.tier}
            if prev["best"] is not None and last["best"] is None:
                events.append({**base, "type": "sold_out", "from": _money(prev["best"])})
                continue
            if prev["best"] is None and last["best"] is not None:
                events.append({**base, "type": "back", "to": _money(last["best"])})
                continue
            if last["best"] is None:
                continue
            if last["best"] != prev["best"]:
                d = last["best"] - prev["best"]
                events.append(
                    {
                        **base,
                        "type": "drop" if d < 0 else "rise",
                        "from": _money(prev["best"]),
                        "to": _money(last["best"]),
                        "delta": _money(d),
                        "pct": round(100 * d / prev["best"], 1),
                    }
                )
            earlier = [f["best"] for f in fetches[:-1] if f["best"] is not None]
            if len(earlier) >= 2 and last["best"] < min(earlier):
                events.append({**base, "type": "new_low", "to": _money(last["best"]), "old_low": _money(min(earlier))})
            # Scarcity is reported when it starts, not on every scan it lasts.
            inv = (r["stays"].get(w.key) or {}).get("cheapest") or {}
            if inv.get("inventory") is not None and inv["inventory"] <= SCARCE_INVENTORY:
                before = _cheapest(conn, prev["scan_id"], r["code"], w) or {}
                was_scarce = (
                    before.get("room") == inv["room"]
                    and before.get("inventory") is not None
                    and before["inventory"] <= SCARCE_INVENTORY
                )
                if not was_scarce:
                    events.append({**base, "type": "scarce", "inventory": inv["inventory"], "room": inv["room"]})
    return events


EVENT_ORDER = {"new_low": 0, "flight_new_low": 0, "drop": 1, "flight_drop": 1, "back": 2,
               "sold_out": 3, "rise": 4, "flight_rise": 4, "scarce": 5}


def _sort_events(events: list[dict]) -> list[dict]:
    order = EVENT_ORDER
    return sorted(events, key=lambda e: (order[e["type"]], e["tier"] != "primary", e.get("to") or 0, e["code"], e["stay"]))


def snapshot(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> tuple[dict, dict]:
    """(latest, history). latest["_flights"] / ["_flights_hist"] carry the flight docs; build() splits them off."""
    now = now or datetime.now(timezone.utc)
    notes = config.load_notes()
    scan = conn.execute("SELECT * FROM scans WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if scan is None:
        raise RuntimeError("no finished scan yet; run `scan` first")
    placeholders = ",".join("?" * len(cfg.countries))
    hotels = conn.execute(
        f"SELECT * FROM hotels WHERE country IN ({placeholders}) ORDER BY country, area, name",
        tuple(sorted(cfg.countries)),
    ).fetchall()
    series = _series(conn)
    health = conn.execute(
        "SELECT COUNT(*) AS n, SUM(status = 'error') AS errors FROM fetches WHERE scan_id = ?", (scan["id"],)
    ).fetchone()

    resort_reviews, _ = reviews.load_resorts()
    resorts, adults_only, unknown = [], [], []
    for h in hotels:
        if h["takes_party"] == 0:
            adults_only.append({"code": h["code"], "name": h["name"], "area": h["area"]})
            continue
        brand, club = _brand(h["name"], notes["brand"])
        extra = notes["resort"].get(h["code"], {})
        stays = {}
        for w in cfg.windows:
            s = _stay(conn, h["code"], w, series.get((h["code"], w.checkin, w.checkout), []), now)
            if s:
                stays[w.key] = s
        priced = [
            (stays[w.key]["cheapest"]["member_total"], w)
            for w in cfg.primary
            if w.key in stays and stays[w.key]["cheapest"]
        ]
        best = min(priced, key=lambda p: (p[0], p[1].checkin)) if priced else None
        flex_priced = [
            (s["cheapest"]["member_total"], k)
            for k, s in stays.items()
            if s["cheapest"] and k not in {w.key for w in cfg.primary}
        ]
        flex_best = min(flex_priced) if flex_priced else None
        entry = {
            "code": h["code"],
            "name": h["name"],
            "brand": brand,
            "country": h["country"],
            "area": h["area"],
            "airports": list(cfg.airports.get(h["area"], ())),
            "airport": (cfg.airports.get(h["area"]) or ("",))[0],
            "kids_club": club.get("kids_club"),
            "kids_ages": club.get("ages"),
            "note": extra.get("note", ""),
            "fit": (resort_reviews.get(h["code"]) or {}).get("fit"),
            "best_primary": {"stay": best[1].key, "member_total": best[0], "nights": best[1].nights} if best else None,
            "best_flex": {"stay": flex_best[1], "member_total": flex_best[0]} if flex_best else None,
            "points": _points(
                extra.get("category", ""), notes["award"], cfg.party,
                best[1].nights if best else cfg.primary[0].nights, best[0] if best else None,
            ),
            "stays": stays,
        }
        if h["takes_party"] is None and all(s["status"] != "ok" for s in stays.values()):
            unknown.append({"code": h["code"], "name": h["name"], "area": h["area"], "note": h["classify_note"]})
            continue
        resorts.append(entry)

    all_fseries = _flight_series(conn, cfg.flights.max_stops if cfg.flights else None)
    fseries, tier_switch = _since_tier_fares(all_fseries)
    dests = sorted({ap for r in resorts for ap in r["airports"]})
    flights_doc, flights_hist = (
        _flights_doc(cfg, fseries, dests, now) if cfg.flights else ({"routes": {}}, {"points": {}})
    )
    options_doc = _options_doc(conn, cfg, fseries, dests, now) if cfg.flights else {"columns": [], "routes": {}}
    for r in resorts:
        r["best_trip"] = _best_trip(r, flights_doc["routes"], cfg.primary)
    scan_times = {row["id"]: row["started_at"] for row in conn.execute("SELECT id, started_at FROM scans")}
    # Trends keep their history across the switch to tier fares, marked.
    trends_doc = trends.stay_trends(series, all_fseries, resorts, cfg.windows, scan_times, now)
    if tier_switch and any(f["fare_basis"] is None for fs in all_fseries.values() for f in fs):
        trends_doc["annotations"] = sorted(
            trends_doc["annotations"] + [{"t": _epoch(tier_switch), "label": "Fares include seat selection"}],
            key=lambda a: a["t"],
        )

    resorts.sort(key=lambda r: (r["best_primary"] is None, (r["best_primary"] or {}).get("member_total", 0), r["name"]))
    for rank, r in enumerate(resorts, 1):
        r["rank"] = rank

    latest = {
        "schema": 1,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "scan": {
            "id": scan["id"],
            "started_at": scan["started_at"],
            "finished_at": scan["finished_at"],
            "requests": health["n"],
            "errors": health["errors"] or 0,
        },
        "party": {
            "adults": cfg.party.adults,
            "child_ages": list(cfg.party.child_ages),
            "code": cfg.party.code,
        },
        "windows": [
            {
                "key": w.key,
                "checkin": w.checkin,
                "checkout": w.checkout,
                "nights": w.nights,
                "tier": w.tier,
                "doc": _stay_doc_id(w),
            }
            for w in cfg.windows
        ],
        "award": notes["award"],
        # The page fills in {code}, {checkin} and {checkout}; the format lives in bookcore.
        "booking_url_template": bookcore.booking_url("{code}", "{checkin}", "{checkout}", cfg.party),
        "resorts": resorts,
        "adults_only": adults_only,
        "unknown": unknown,
        "events": _sort_events(
            _events(conn, resorts, series, cfg.windows, scan["id"])
            + _flight_events(fseries, dests, cfg.windows, scan["started_at"], cfg.flights.home_label if cfg.flights else "home")
        ),
        "flights_searched_as": flights_doc.get("searched_as"),
        "trip": {
            "name": config.TRIP,
            **cfg.trip,
            # Shown on the page until the user's own research replaces every shipped review.
            **({"review_note": reviews.SHIPPED_NOTE} if reviews.uses_shipped(r["code"] for r in resorts) else {}),
        },
        "_flights": flights_doc,
        "_flights_hist": flights_hist,
        "_flights_options": options_doc,
        "_trends": trends_doc,
        "_trend_summary": trends.summary(trends_doc, cfg.windows, now),
    }

    history = {}
    for w in cfg.windows:
        points = {}
        for r in resorts:
            pts, last = [], None
            for f in series.get((r["code"], w.checkin, w.checkout), []):
                value = (_money(f["best"]), _money(f["best_refundable"]))
                if value != last:
                    pts.append([_epoch(f["fetched_at"]), *value])
                    last = value
            if pts:
                points[r["code"]] = pts[-HISTORY_POINTS:]
        history[_stay_doc_id(w)] = {"schema": 1, "stay": w.key, "points": points}
    return latest, history


def _changes_md(
    latest: dict, flights_doc: dict | None = None, options_doc: dict | None = None, trend_summary: list | None = None
) -> str:
    def usd(v):
        return "—" if v is None else f"${v:,.0f}"

    lines = [
        f"# {latest.get('trip', {}).get('title', 'Resort watch')} — scan {latest['scan']['id']}",
        "",
        f"Finished {latest['scan']['finished_at']} · {latest['scan']['requests']} requests · "
        f"{latest['scan']['errors']} errors · party {latest['party']['code']} (adults-children-babies)",
        "",
        "Prices are World of Hyatt member totals for the whole stay and party, taxes included.",
        "",
        "## Events since the previous scan",
        "",
    ]
    if not latest["events"]:
        lines.append("None. No price, availability or inventory change.")
    for e in latest["events"]:
        what = {
            "flight_drop": f"family fare dropped {usd(e.get('from'))} → {usd(e.get('to'))} ({e.get('pct')}%)",
            "flight_rise": f"family fare rose {usd(e.get('from'))} → {usd(e.get('to'))} (+{e.get('pct')}%)",
            "flight_new_low": f"family fare at a new low {usd(e.get('to'))} (was {usd(e.get('old_low'))})",
            "drop": f"dropped {usd(e.get('from'))} → {usd(e.get('to'))} ({e.get('pct')}%)",
            "rise": f"rose {usd(e.get('from'))} → {usd(e.get('to'))} (+{e.get('pct')}%)",
            "new_low": f"new all-time low {usd(e.get('to'))} (was {usd(e.get('old_low'))})",
            "sold_out": f"no longer bookable for the party (was {usd(e.get('from'))})",
            "back": f"bookable again at {usd(e.get('to'))}",
            "scarce": f"only {e.get('inventory')} left of the cheapest room ({e.get('room')})",
        }[e["type"]]
        lines.append(f"- [{e['tier']}] {e['name']} ({e['code']}), {e['stay']}: {what}")
    if trend_summary:
        def signed(v):
            return "—" if v is None else ("+" if v > 0 else "−" if v < 0 else "±") + f"${abs(v):,.0f}"

        lines += [
            "",
            "## Stay trends (all kids-club resorts together, cheapest stay first)",
            "",
            "Median trip = the middle resort's hotel + nonstop total for that stay; it moves with the whole stay, "
            "not one resort. Cheapest trip can jump when the leader changes.",
            "",
            "| Stay | Tier | Median trip | Δ 24h | Δ 7d | Cheapest trip | Δ 7d | Since first |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for t in trend_summary:
            lines.append(
                f"| {t['stay']} | {t['tier']} | {usd(t['median_trip'])} | {signed(t['median_trip_24h'])} | "
                f"{signed(t['median_trip_7d'])} | {usd(t['cheapest_trip'])} ({t['cheapest_code']}) | "
                f"{signed(t['cheapest_trip_7d'])} | {signed(t['median_trip_since'])} since {t['since']} |"
            )
    trips = sorted((r for r in latest["resorts"] if r.get("best_trip")), key=lambda r: r["best_trip"]["total"])
    kind_label = {"nonstop": "nonstop", "one_stop": "1 stop", "any": "2+ stops"}
    if trips:
        lines += [
            "",
            "## Ranking by trip total (hotel + flights, best preferred stay)",
            "",
            f"Flights from {', '.join((flights_doc or {}).get('departure_airports', []))}, searched as "
            f"{(flights_doc or {}).get('searched_as', '?')}. {(flights_doc or {}).get('fare_rule', '')} "
            f"{(flights_doc or {}).get('fare_basis', '')}",
            "",
            "| # | Resort | Fit /10 | Kids club | Stay | Hotel | Flights | Airport | Trip total |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(trips[:20], 1):
            t = r["best_trip"]
            lines.append(
                f"| {i} | {r['name']} | {r['fit'] if r.get('fit') is not None else '—'} | {r['kids_club'] or 'none'} | {t['stay']} | {usd(t['hotel'])} | "
                f"{usd(t['flight'])} ({kind_label[t['flight_kind']]}) | {t['airport']} | {usd(t['total'])} |"
            )
        no_fare = sorted({r["name"] for r in latest["resorts"] if r.get("best_primary") and not r.get("best_trip")})
        if no_fare:
            lines += ["", "No qualifying flights for the preferred stays: " + "; ".join(no_fare)]
    routes = (flights_doc or {}).get("routes", {})
    if routes:
        prim = [w["key"] for w in latest["windows"] if w["tier"] == "primary"]
        lines += [
            "",
            "## Fares by airport (preferred stays)",
            "",
            "| Airport | " + " | ".join(prim) + " | Low seen |",
            "|---|" + "---|" * len(prim) + "---|",
        ]
        for ap in sorted(routes):
            cells, lows = [], []
            for k in prim:
                rt = routes[ap].get(k)
                if not rt or not rt.get("family"):
                    cells.append("none" if rt else "—")
                    continue
                fam = rt["family"]
                d = f", Δ {usd(rt['delta'])}" if rt.get("delta") else ""
                cells.append(f"{usd(fam['price'])} {kind_label[fam['kind']]}{d}")
                if rt.get("low"):
                    lows.append(rt["low"]["price"])
            lines.append(f"| {ap} | " + " | ".join(cells) + f" | {usd(min(lows)) if lows else '—'} |")
        # Timing reads were written for one trip's schedules; others leave them out.
        route_research = (
            reviews.load_airlines().get("routes", {}) if latest.get("trip", {}).get("route_timing", True) else {}
        )
        if options_doc and options_doc.get("routes"):
            lines += [
                "",
                "Cheapest nonstop per airline (preferred stays), with the outbound departure and the paired "
                "return (local times). The page can filter by airline, airport and departure time, "
                "so a cheap fare on one airline is worth naming:",
                "",
            ]
            for ap in sorted(routes):
                for k in prim:
                    by_airline = _cheapest_by_airline(options_doc, ap, k)
                    timing = {
                        name: ((route_research.get(f"{name}|{ap}") or {}).get("timing") or {}).get("rating")
                        for name in (r["airlines"] for r in by_airline)
                    }
                    tiers = (routes.get(ap, {}).get(k) or {}).get("fares") or {}

                    def fare_name(name):
                        """'Delta Main Classic (+$270 over Delta Main Basic)', or the airline alone."""
                        f = tiers.get(name) or {}
                        if f.get("basis") == "unknown":
                            return f"{name} (Google's price)"
                        if not f.get("tier"):
                            return name
                        label = f["tier"] if f["tier"].lower().startswith(name.lower()) else f"{name} {f['tier']}"
                        over = f" (+{usd(f['extra'])} over {f['list_tier']})" if f.get("extra") and f.get("list_tier") else ""
                        return label + over

                    if by_airline:
                        lines.append(f"- {ap} {k}: " + " · ".join(
                            f"{fare_name(r['airlines'])} {usd(r['price'])} (out {r['depart_airport']} {r['depart_time']}"
                            + (f", back {r['back']['depart_time']}→{r['back']['arrive_airport']} {r['back']['arrive_time']}"
                               if r["back"] else ", return not checked")
                            + (f"; timing {timing[r['airlines']]}" if timing.get(r["airlines"]) else "") + ")"
                            for r in by_airline
                        ))
    lines += [
        "",
        "## Ranking (best primary stay)",
        "",
        "| # | Resort | Area | Kids club | Best stay | Member total | Δ prev | Δ 24h | Low | Refundable | 2 beds |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in latest["resorts"]:
        b = r["best_primary"]
        s = r["stays"].get(b["stay"]) if b else None
        lines.append(
            "| {rank} | {name} | {area} ({ap}) | {club} | {stay} | {total} | {d} | {d24} | {low} | {ref} | {two} |".format(
                rank=r["rank"],
                name=r["name"],
                area=r["area"],
                ap=r["airport"],
                club=r["kids_club"] or "none",
                stay=b["stay"] if b else "—",
                total=usd(b["member_total"]) if b else "unavailable",
                d=usd(s["delta"]) if s and s["delta"] else "0",
                d24=usd(s["delta_24h"]) if s and s["delta_24h"] else "0",
                low=usd(s["low"]["member_total"]) if s and s["low"] else "—",
                ref=usd((s["cheapest_refundable"] or {}).get("member_total")) if s else "—",
                two=usd((s["cheapest_two_beds"] or {}).get("member_total")) if s else "—",
            )
        )
    if latest["unknown"]:
        lines += ["", "## No rooms returned (closed or sold out)", ""]
        lines += [f"- {u['name']} ({u['code']})" for u in latest["unknown"]]
    return "\n".join(lines) + "\n"


# The artifact db refuses documents over 256 KiB; leave headroom.
MAX_DOC_BYTES = 240 * 1024


def _write_json(path: Path, doc: dict) -> str:
    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(body.encode()) > MAX_DOC_BYTES:
        raise ValueError(f"{path.name} is {len(body.encode())} bytes, over the {MAX_DOC_BYTES}-byte db limit")
    path.write_text(body)
    # The hash ignores generated_at so an unchanged history doc is not re-sent.
    stable = {k: v for k, v in doc.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def build(conn: sqlite3.Connection, cfg: Config) -> list[Path]:
    latest, history = snapshot(conn, cfg)
    flights_doc, flights_hist = latest.pop("_flights"), latest.pop("_flights_hist")
    options_doc = latest.pop("_flights_options")
    trends_doc, trend_summary = latest.pop("_trends"), latest.pop("_trend_summary")
    db_dir = config.REPORT_DIR / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    hashes = {"latest": _write_json(db_dir / "latest.json", latest)}
    if cfg.flights:
        hashes["flights"] = _write_json(db_dir / "flights.json", flights_doc)
        hashes["flights-hist"] = _write_json(db_dir / "flights-hist.json", flights_hist)
        hashes["flights-options"] = _write_json(db_dir / "flights-options.json", options_doc)
    for doc_id, doc in history.items():
        hashes[doc_id] = _write_json(db_dir / f"{doc_id}.json", doc)
    hashes["trends"] = _write_json(db_dir / "trends.json", trends_doc)
    reviews_doc = reviews_document({r["code"] for r in latest["resorts"]}, cfg.trip.get("route_timing", True))
    if reviews_doc:
        hashes["reviews"] = _write_json(db_dir / "reviews.json", reviews_doc)
    published = json.loads(config.PUBLISHED_PATH.read_text()) if config.PUBLISHED_PATH.exists() else {}
    plan = [
        {"collection": DOC_COLLECTION, "doc_id": doc_id, "file_path": str(db_dir / f"{doc_id}.json"), "sha": sha}
        for doc_id, sha in hashes.items()
        # latest always goes: its generated_at is the page's "last checked".
        if doc_id == "latest" or published.get(doc_id) != sha
    ]
    plan_path = config.REPORT_DIR / "publish_plan.json"
    plan_path.write_text(json.dumps(plan, indent=1))
    changes = config.REPORT_DIR / "changes.md"
    changes.write_text(_changes_md(latest, flights_doc, options_doc, trend_summary))
    return [changes, plan_path, db_dir]


def reviews_document(codes: set[str], route_timing: bool = True) -> dict | None:
    """sbw/reviews: research for resorts still tracked, plus airline notes.

    Route notes and timing reads were written against one trip's schedules;
    a trip on other dates leaves them out (`route_timing = false`).
    """
    resort_reviews, _ = reviews.load_resorts()
    airlines = reviews.load_airlines()
    if not resort_reviews and not airlines:
        return None
    return {
        "schema": 1,
        "weights": reviews.WEIGHTS,
        "resorts": {code: r for code, r in sorted(resort_reviews.items()) if code in codes},
        "airlines": airlines.get("airlines", {}),
        "routes": airlines.get("routes", {}) if route_timing else {},
        "airlines_researched_on": airlines.get("researched_on"),
    }


def mark_published() -> list[str]:
    """Record the plan's hashes as live, after the session's db batch succeeded."""
    plan = json.loads((config.REPORT_DIR / "publish_plan.json").read_text())
    published = json.loads(config.PUBLISHED_PATH.read_text()) if config.PUBLISHED_PATH.exists() else {}
    for item in plan:
        published[item["doc_id"]] = item["sha"]
    config.PUBLISHED_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PUBLISHED_PATH.write_text(json.dumps(published, indent=1, sort_keys=True))
    return [item["doc_id"] for item in plan]
