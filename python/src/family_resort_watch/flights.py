"""Round-trip fares from the user's home airports to each resort's airport, via Google Flights.

fast-flights rebuilds Google Flights' protobuf `tfs` parameter, so one call
is one real Google search: no key, no quota, and a response shape Google can
change without notice. Two lessons learned the hard way:

* Google sends two result lists and fast-flights parses only the second, so
  the cheapest fares go missing. `_fetch_items` reads both.
* With a child in the party Google defers results to a client-side request
  and the payload comes back empty for most international routes (measured
  here on 2026-10-03: CUN, PVR and MBJ empty, PUJ 1 result vs 19). So fares
  are searched as `proxy_adults` adults. International child fares are
  usually the adult fare; the links still open with the real party.

Searches ask Google for at most `max_stops` stops (0 = nonstop only), so the
whole result list is eligible rather than a few nonstops picked out of a
connection-heavy list.

Searches run from each of `origins`: home airports, or a Google metro code
such as NYC (JFK, LGA, EWR) or CHI (ORD, MDW) that covers several in one
search. `options` keeps only `departure_airports` (the home airports the user
will actually use), for outbound departures and return arrivals alike. Every eligible itinerary is kept (not just the
cheapest) so the report can filter by airport, airline and departure time,
and each outbound gets its paired returns from a follow-up search
(`returns_for`) with that outbound selected.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field, replace

from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html
from fast_flights.pb.flights_pb2 import FlightData
from selectolax.lexbor import LexborHTMLParser

from .config import Party

log = logging.getLogger(__name__)

KINDS = ("any", "nonstop", "one_stop")


@dataclass(frozen=True)
class FlightSettings:
    origins: tuple[str, ...]
    departure_airports: tuple[str, ...]  # the home airports kept, both ways
    home_label: str
    max_stops: int | None
    proxy_adults: int
    exclude_basic_economy: bool
    carry_on_bags: int
    checked_bags: int
    request_delay_seconds: float
    retries: int
    every_hours: int
    flex_every_hours: int
    return_lookups_per_route: int = 20


@dataclass(frozen=True)
class Quote:
    kind: str  # any | nonstop | one_stop
    price: int  # round trip, whole party
    airlines: str
    stops: int
    depart_airport: str
    depart_time: str  # outbound, local, "HH:MM"
    arrive_time: str  # outbound, local, "HH:MM"; "+1" when next day


class FlightError(RuntimeError):
    pass


def _query(s: FlightSettings, dest: str, depart: str, ret: str, passengers: Passengers, origin: str | None = None):
    origin = origin or s.origins[0]
    return create_query(
        flights=[
            FlightQuery(date=depart, from_airport=origin, to_airport=dest),
            FlightQuery(date=ret, from_airport=dest, to_airport=origin),
        ],
        trip="round-trip",
        seat="economy",
        passengers=passengers,
        max_stops=s.max_stops,
        # Google prices these bags in per passenger; they are not a filter.
        carry_on_bags=s.carry_on_bags,
        checked_bags=s.checked_bags,
        # Basic Economy has no carry-on, no seat choice and no changes.
        exclude_basic_economy=s.exclude_basic_economy,
        # Self-transfer means collecting bags and re-checking with a child in tow.
        hide_separate_and_self_transfer=True,
        currency="USD",
        language="en-US",
    )


def google_flights_url(s: FlightSettings, party: Party, dest: str, depart: str, ret: str) -> str:
    """Link for a person: the real party, which Google's own page handles fine."""
    children = sum(1 for a in party.child_ages if a >= 2)
    infants = len(party.child_ages) - children
    pax = Passengers(adults=party.adults, children=children, infants_on_lap=infants)
    return _query(s, dest, depart, ret, pax).url()


@dataclass(frozen=True)
class Option:
    """One itinerary from a search: an outbound, or a return paired with one.

    A round-trip search lists outbound flights, each priced as its cheapest
    round trip. `returns_for` selects one outbound and gets the returns Google
    pairs with it, each priced as that exact round trip.
    """

    price: int  # round trip, whole party
    airlines: str
    flight_numbers: str  # "DM 5311", or "UA 1514, UA 220" with a connection
    stops: int
    depart_airport: str
    depart_time: str  # local "HH:MM"
    arrive_time: str  # local "HH:MM", "+1" when the next day
    duration_minutes: int | None  # time in the air, layovers excluded
    aircraft: str
    legroom: str
    arrive_airport: str = ""
    # (from, date, to, airline code, flight number) per leg: what selecting
    # this flight in a follow-up search needs. Not stored.
    segments: tuple = field(default=(), compare=False, repr=False)
    # Which of the configured origins found it. Not stored.
    origin: str = field(default="", compare=False, repr=False)


def _fetch_items(query) -> list:
    """Raw itineraries from both of Google's result lists (see flight-watch)."""
    html = fetch_flights_html(query)
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise FlightError("no result payload in response")
    payload = json.loads(script.text().split("data:", 1)[1].rsplit(",", 1)[0])

    def items(slot):
        entry = payload[slot] if len(payload) > slot else None
        return (entry[0] if entry else None) or []

    return items(2) + items(3)


def _hhmm(t) -> str:
    # Google sends [hour, minute], dropping trailing zeros: [12] is 12:00, [None, 30] is 00:30.
    if not t:
        return ""
    return f"{t[0] or 0:02d}:{(t[1] if len(t) > 1 and t[1] else 0):02d}"


def _at(seq, i, default=None):
    return seq[i] if isinstance(seq, list) and len(seq) > i else default


def parse_option(item) -> Option | None:
    """One raw itinerary, read at the indexes fast-flights' own parser uses.

    fast-flights drops flight numbers (leg[22]) and legroom (leg[14]), so this
    reads the row itself. Anything malformed is skipped, not raised.
    """
    try:
        flight, price = item[0], item[1][0][1]
        legs = flight[2]
        if not isinstance(price, int) or price <= 0 or not legs:
            return None
        first, last = legs[0], legs[-1]

        def number(leg):
            info = _at(leg, 22) or []
            return f"{info[0]} {info[1]}" if len(info) > 1 and info[0] and info[1] else ""

        durations = [leg[11] for leg in legs if isinstance(_at(leg, 11), int)]

        def segment(leg):
            info, day = _at(leg, 22) or [], _at(leg, 20) or []
            if len(info) < 2 or len(day) < 3:
                return None
            return (leg[3], "%04d-%02d-%02d" % tuple(day[:3]), leg[6], info[0], info[1])

        next_day = _at(last, 21) is not None and list(_at(last, 21)) != list(_at(first, 20) or [])
        return Option(
            price=price,
            airlines=", ".join(flight[1] or []),
            flight_numbers=", ".join(n for n in (number(leg) for leg in legs) if n),
            stops=len(legs) - 1,
            depart_airport=first[3],
            depart_time=_hhmm(_at(first, 8)),
            arrive_time=_hhmm(_at(last, 10)) + ("+1" if next_day else ""),
            duration_minutes=sum(durations) if durations else None,
            aircraft=_at(first, 17) or "",
            legroom=_at(first, 14) or "",
            arrive_airport=last[6],
            segments=tuple(seg for seg in (segment(leg) for leg in legs) if seg),
        )
    except (IndexError, TypeError, KeyError):
        return None


def options(items: list, home_airports: tuple[str, ...], returning: bool = False) -> list[Option]:
    """Every priced itinerary using an allowed home airport, cheapest first, deduplicated.

    Outbounds must leave from one; returns (`returning`) must land at one.
    """
    seen, out = set(), []
    for item in items:
        o = parse_option(item)
        if o is None or (o.arrive_airport if returning else o.depart_airport) not in home_airports:
            continue
        key = (o.flight_numbers, o.depart_time, o.price)
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    # Ties go to the earlier departure, then the airline, so the order does
    # not depend on Google's.
    return sorted(out, key=lambda o: (o.price, o.depart_time, o.airlines, o.flight_numbers))


def pick(opts: list[Option]) -> dict[str, Quote]:
    """Cheapest overall, nonstop and with one stop, from sorted options."""
    best: dict[str, Quote] = {}
    for o in opts:
        kinds = ["any"] + (["nonstop"] if o.stops == 0 else ["one_stop"] if o.stops == 1 else [])
        for kind in kinds:
            best.setdefault(
                kind,
                Quote(
                    kind=kind, price=o.price, airlines=o.airlines, stops=o.stops,
                    depart_airport=o.depart_airport, depart_time=o.depart_time, arrive_time=o.arrive_time,
                ),
            )
    return best


def _items_with_retries(s: FlightSettings, query, what: str) -> list:
    last: Exception | None = None
    for attempt in range(s.retries + 1):
        try:
            return _fetch_items(query)
        except Exception as exc:  # noqa: BLE001 - a scraper; anything can surface
            last = exc
            if attempt < s.retries:
                time.sleep(2 ** (attempt + 1) + random.uniform(0, 1))
    raise FlightError(f"{what}: {type(last).__name__}: {last}")


def search(s: FlightSettings, dest: str, depart: str, ret: str) -> tuple[int, list[Option]]:
    """Search from every origin; returns (raw itinerary count, eligible outbounds cheapest first).

    Each option remembers the origin it was found from, so its return lookup
    asks the same question.
    """
    count, found = 0, []
    for i, origin in enumerate(s.origins):
        if i:
            pause(s)
        query = _query(s, dest, depart, ret, Passengers(adults=s.proxy_adults), origin)
        items = _items_with_retries(s, query, f"{origin}-{dest} {depart}")
        count += len(items)
        found += [replace(o, origin=origin) for o in options(items, s.departure_airports)]
    seen, merged = set(), []
    for o in sorted(found, key=lambda o: (o.price, o.depart_time, o.airlines, o.flight_numbers)):
        key = (o.flight_numbers, o.depart_time)
        if key not in seen:
            seen.add(key)
            merged.append(o)
    return count, merged


# Google's `tfs` carries a chosen flight as field 4 of a leg's FlightData:
# {1: from, 2: date, 3: to, 5: airline code, 6: flight number}, one entry per
# segment. fast-flights' schema has no such field, so it is appended as raw
# protobuf; protobuf keeps fields it does not know when re-serializing.
def _varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def selected_flight_bytes(segments: tuple) -> bytes:
    out = b""
    for frm, day, to, airline, number in segments:
        out += _field(4, b"".join(_field(n, v.encode()) for n, v in ((1, frm), (2, day), (3, to), (5, airline), (6, number))))
    return out


def returns_for(s: FlightSettings, dest: str, depart: str, ret: str, outbound: Option) -> list[Option]:
    """Returns Google pairs with one outbound, each priced as that round trip, cheapest first."""
    if not outbound.segments:
        raise FlightError(f"{dest} {depart}: {outbound.flight_numbers or 'outbound'} has no flight numbers to select")
    query = _query(s, dest, depart, ret, Passengers(adults=s.proxy_adults), outbound.origin or None)
    query.flight_data[0] = FlightData.FromString(
        query.flight_data[0].SerializeToString() + selected_flight_bytes(outbound.segments)
    )
    items = _items_with_retries(s, query, f"{dest} {depart} return for {outbound.flight_numbers}")
    return options(items, s.departure_airports, returning=True)


def pause(s: FlightSettings) -> None:
    """Jittered gap between searches so the pattern is not perfectly periodic."""
    time.sleep(random.uniform(s.request_delay_seconds * 0.5, s.request_delay_seconds * 1.5))
