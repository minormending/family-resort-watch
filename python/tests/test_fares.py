import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from family_resort_watch import fares, storage
from family_resort_watch.config import Window
from family_resort_watch.flights import Option

STAY = Window("2027-04-24", "2027-04-29", "primary")


def option(price, seller, direct=True, tier=None, basic=False, feats=()):
    """One booking option, filled at the indexes parse_booking reads (see the 2026-10-28 captures)."""
    row = [None] * 22
    row[0], row[1], row[7] = 0, [[seller[:2].upper(), seller, None, direct]], [[None, price], "token"]
    if tier:
        row[21] = [["XX", tier.upper()], [list(f) for f in feats], basic or None, tier]
    return row


def response(*options):
    """A GetBookingResults body: a header chunk, then one with the options."""
    head = json.dumps([[None, None, 0], [None] * 5])
    body = json.dumps([[None, None, 1], [list(options)]])
    lines = [json.dumps([["wrb.fr", None, head]]), json.dumps([["wrb.fr", None, body]]), json.dumps([["di", 44]])]
    return ")]}'\n\n" + "\n".join(f"{len(x)}\n{x}" for x in lines)


DELTA = response(
    option(3043, "Delta", tier="Delta Main Basic", basic=True, feats=[(8, 1), (4, 4), (1, 2)]),
    option(3313, "Delta", tier="Delta Main Classic", feats=[(8, 1), (4, 1), (1, 1)]),
    option(3763, "Delta", tier="Delta Comfort Classic", feats=[(8, 1), (4, 1), (11, 1)]),
)


def test_parse_booking_reads_tiers_seats_and_seller():
    got = fares.parse_booking(DELTA)
    assert [(f.tier, f.price, f.basic, f.seat, f.carry_on) for f in got] == [
        ("Delta Main Basic", 3043, True, 4, 1),
        ("Delta Main Classic", 3313, False, 1, 1),
        ("Delta Comfort Classic", 3763, False, 1, 1),
    ]
    assert all(f.airline_direct and f.seller == "Delta" for f in got)
    assert fares.parse_booking(")]}'\n\n12\n[[\"e\",4]]") == []
    assert fares.parse_booking(response(["garbage"], option(0, "Delta"))) == []


def test_choose_takes_the_cheapest_airline_tier_with_free_seats():
    c = fares.choose("Delta", 3043, fares.parse_booking(DELTA))
    assert (c.price, c.tier, c.basis, c.uplift, c.list_tier) == (3313, "Delta Main Classic", "seat", 270, "Delta Main Basic")


def test_choose_skips_tiers_without_a_carry_on_and_keeps_a_matching_list_price():
    united = fares.parse_booking(response(
        option(2992, "United", tier="Basic Economy", basic=True, feats=[(8, 4), (4, 2)]),
        option(3292, "United", tier="Economy", feats=[(8, 1), (4, 1)]),
    ))
    c = fares.choose("United", 3292, united)
    assert (c.price, c.tier, c.uplift) == (3292, "Economy", 0)


def test_choose_falls_back_to_not_basic_then_the_airline_then_third_parties():
    no_free_seats = fares.parse_booking(response(
        option(900, "Spirit", tier="Go", basic=True, feats=[(8, 1), (4, 2)]),
        option(1100, "Spirit", tier="Go Comfy", feats=[(8, 1), (4, 2)]),
    ))
    c = fares.choose("Spirit", 900, no_free_seats)
    assert (c.price, c.tier, c.basis) == (1100, "Go Comfy", "not_basic")
    collapsed = fares.parse_booking(response(option(2107, "Arajet")))  # tiers not sent
    c = fares.choose("Arajet", 2107, collapsed)
    assert (c.price, c.tier, c.basis) == (2107, None, "airline")
    only_agencies = fares.parse_booking(response(option(2027, "Booking.com", direct=False)))
    c = fares.choose("Arajet", 2107, only_agencies)
    assert (c.price, c.basis) == (2107, "third_party")


@pytest.mark.parametrize("hhmm, label", [("14:05", "2:05 PM"), ("00:30", "12:30 AM"), ("12:00", "12:00 PM"),
                                         ("09:10", "9:10 AM")])
def test_clock_matches_the_results_list(hhmm, label):
    assert fares.clock(hhmm) == label


def opt(price, airline, number, depart="10:00"):
    return Option(price=price, airlines=airline, flight_numbers=number, stops=0, depart_airport="JFK",
                  depart_time=depart, arrive_time="14:00", duration_minutes=240, aircraft="", legroom="",
                  arrive_airport="PUJ", segments=(("JFK", STAY.checkin, "PUJ", number[:2], number[3:]),))


class FakeChrome:
    def __init__(self, choices, usable=True):
        self.fares, self.choices, self.usable, self.calls = fares, choices, usable, []

    def lookup(self, s, flights, dest, w, outbound, back):
        self.calls.append(outbound.airlines)
        c = self.choices[outbound.airlines]
        if isinstance(c, Exception):
            raise c
        return c


SETTINGS = SimpleNamespace(fare_lookup_every_hours=48, fare_lookups_per_sweep=20)
FLIGHTS = SimpleNamespace(pause=lambda s: None)


def counts():
    return {"fare_lookups": 0, "fare_errors": 0}


def test_with_fares_adds_each_airlines_gap_to_all_its_flights(tmp_path):
    from family_resort_watch.scan import _with_fares

    d1, d2, b6 = opt(3043, "Delta", "DL 1"), opt(3200, "Delta", "DL 2"), opt(3127, "JetBlue", "B6 3")
    back = {d1: ([opt(3043, "Delta", "DL 9", "14:05")], None), b6: ([opt(3127, "JetBlue", "B6 8")], None)}
    chrome = FakeChrome({
        "Delta": fares.Choice("Delta", 3043, 3313, "Delta Main Classic", "seat", "Delta Main Basic"),
        "JetBlue": fares.Choice("JetBlue", 3127, 3127, "Main", "seat", "Main"),
    })
    with storage.connect(tmp_path / "t.db") as conn:
        opts, returns, rows = _with_fares(conn, SETTINGS, FLIGHTS, chrome, "PUJ", STAY, [d1, b6, d2], back, counts())
    # Delta gets +270 everywhere, JetBlue nothing, and the order follows.
    assert [(o.flight_numbers, o.price) for o in opts] == [("B6 3", 3127), ("DL 1", 3313), ("DL 2", 3470)]
    assert returns[opts[1]][0][0].price == 3313
    assert {r["airline"]: (r["source"], r["price"] - r["list_price"]) for r in rows} == {
        "Delta": ("lookup", 270), "JetBlue": ("lookup", 0)}


def test_with_fares_reuses_fresh_lookups_and_falls_back_when_one_fails(tmp_path):
    from family_resort_watch.scan import _with_fares

    d1, b6 = opt(3043, "Delta", "DL 1"), opt(3127, "JetBlue", "B6 3")
    back = {d1: ([opt(3043, "Delta", "DL 9")], None), b6: ([opt(3127, "JetBlue", "B6 8")], None)}
    first = FakeChrome({"Delta": fares.Choice("Delta", 3043, 3313, "Delta Main Classic", "seat", "Delta Main Basic"),
                        "JetBlue": fares.FareError("error 13")})
    with storage.connect(tmp_path / "t.db") as conn:
        _, _, rows = _with_fares(conn, SETTINGS, FLIGHTS, first, "PUJ", STAY, [d1, b6], back, c := counts())
        fetch = storage.record_flights(conn, "PUJ", STAY.checkin, STAY.checkout, "3 adults", 2, {}, fare_basis="tier")
        storage.record_fares(conn, fetch, rows)
        assert c == {"fare_lookups": 2, "fare_errors": 1}
        assert {r["airline"]: r["source"] for r in rows} == {"Delta": "lookup", "JetBlue": "none"}

        # Next sweep: Delta's lookup is fresh, so it is reused at today's price; no browser call.
        again = FakeChrome({"JetBlue": fares.FareError("error 13")})
        cheaper = opt(2900, "Delta", "DL 1")
        opts, _, rows = _with_fares(conn, SETTINGS, FLIGHTS, again, "PUJ", STAY, [cheaper], {}, counts())
        assert again.calls == [] and opts[0].price == 3170
        assert rows[0]["source"] == "cached" and rows[0]["tier"] == "Delta Main Classic"

        # Another stay of the route borrows the gap until it gets its own lookup.
        other = Window("2027-04-25", "2027-04-30", "primary")
        off = FakeChrome({}, usable=False)
        opts, _, rows = _with_fares(conn, SETTINGS, FLIGHTS, off, "PUJ", other, [cheaper], {}, counts())
        assert opts[0].price == 3170 and rows[0]["source"] == "route"

        # Stale lookups are redone.
        conn.execute("UPDATE flight_fares SET looked_at = ?",
                     ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),))
        redo = FakeChrome({"Delta": fares.Choice("Delta", 3043, 3343, "Delta Main Classic", "seat", None)})
        _with_fares(conn, SETTINGS, FLIGHTS, redo, "PUJ", STAY, [d1], back, counts())
        assert redo.calls == ["Delta"]


def test_fare_history_restarts_at_the_switch_to_tier_fares():
    from family_resort_watch.report import _since_tier_fares

    old = {"fare_basis": None, "fetched_at": "2026-10-20T00:00:00+00:00"}
    new = {"fare_basis": "tier", "fetched_at": "2026-10-28T12:00:00+00:00"}
    series, switch = _since_tier_fares({("PUJ", "a", "b"): [old, old, new, new], ("CUN", "a", "b"): [old]})
    assert series[("PUJ", "a", "b")] == [new, new] and series[("CUN", "a", "b")] == [old]
    assert switch == new["fetched_at"]
