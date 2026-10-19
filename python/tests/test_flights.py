from family_resort_watch.flights import options, parse_option, pick
from family_resort_watch.report import family_fare

DAY = [2027, 4, 24]


def leg(frm, to, dep, arr, number=("B6", "1569"), arr_day=DAY, minutes=245):
    """A raw Google leg, filled at the indexes the parser reads (see the 2026-10-03 PUJ payload)."""
    row = [None] * 23
    row[3], row[6] = frm, to
    row[8], row[10], row[11] = dep, arr, minutes
    row[14], row[17] = "32 in", "Airbus A320"
    row[20], row[21] = DAY, arr_day
    row[22] = [number[0], number[1], None, "Airline"]
    return row


def item(price, airline, *legs):
    return [["", [airline], list(legs)], [[None, price]]]


def test_parse_option_reads_flight_number_times_and_legroom():
    o = parse_option(item(1690, "Arajet", leg("EWR", "PUJ", [12], [16, 19], number=("DM", "5311"), minutes=259)))
    assert (o.price, o.airlines, o.flight_numbers, o.depart_airport) == (1690, "Arajet", "DM 5311", "EWR")
    # Google drops trailing zeros: [12] is noon.
    assert (o.depart_time, o.arrive_time, o.duration_minutes) == ("12:00", "16:19", 259)
    assert (o.stops, o.legroom, o.aircraft) == (0, "32 in", "Airbus A320")


def test_parse_option_skips_malformed_and_unpriced_rows():
    assert parse_option(["garbage"]) is None
    assert parse_option(item(0, "Mystery", leg("JFK", "PUJ", [7], [11]))) is None


def test_options_drop_laguardia_dedupe_and_sort():
    raw = [
        item(900, "Spirit", leg("LGA", "PUJ", [6], [10], number=("NK", "1"))),     # wrong airport
        item(3661, "JetBlue", leg("JFK", "PUJ", [9, 15], [13, 20])),
        item(3517, "United", leg("EWR", "PUJ", [8, 11], [12, 15], number=("UA", "1514"))),
        item(3661, "JetBlue", leg("JFK", "PUJ", [9, 15], [13, 20])),                # same flight in slot 3
        item(3517, "United", leg("EWR", "PUJ", [7, 0], [11, 5], number=("UA", "2122"))),
    ]
    opts = options(raw, ("JFK", "EWR"))
    assert [(o.price, o.flight_numbers) for o in opts] == [(3517, "UA 2122"), (3517, "UA 1514"), (3661, "B6 1569")]


def test_overnight_arrival_is_marked():
    o = parse_option(item(1200, "Delta", leg("JFK", "CUN", [21], [0, 30], arr_day=[2027, 4, 25])))
    assert o.arrive_time == "00:30+1"


def test_pick_buckets_by_stops():
    raw = [
        item(1100, "American", leg("EWR", "MIA", [6], [9], number=("AA", "1")), leg("MIA", "PUJ", [11], [13], number=("AA", "2"))),
        item(1250, "United", leg("EWR", "PUJ", [9], [13], number=("UA", "3"))),
    ]
    q = pick(options(raw, ("JFK", "EWR")))
    assert (q["any"].price, q["nonstop"].price, q["one_stop"].price) == (1100, 1250, 1100)


def test_family_fare_respects_max_stops():
    quotes = {"any": {"price": 900}, "one_stop": {"price": 900}}
    assert family_fare(quotes, 0) is None                      # nonstop only: no fare
    assert family_fare(quotes, 1) == {"price": 900, "kind": "one_stop"}
    assert family_fare({**quotes, "nonstop": {"price": 1200}}, 0) == {"price": 1200, "kind": "nonstop"}


def test_selected_flight_bytes_match_googles_own_tfs():
    # Appended to the outbound leg, these bytes made Google list Arajet's paired
    # return (DM 5382) in a live search on 2026-10-03; pin them so they don't drift.
    expected = bytes.fromhex("2220") + b"\n\x03EWR\x12\n2027-04-24\x1a\x03PUJ*\x02DM2\x045311"
    from family_resort_watch.flights import selected_flight_bytes
    assert selected_flight_bytes((("EWR", "2027-04-24", "PUJ", "DM", "5311"),)) == expected


def test_parse_option_keeps_segments_for_selection_and_arrival_airport():
    o = parse_option(item(1690, "Arajet", leg("EWR", "PUJ", [12], [16, 19], number=("DM", "5311"))))
    assert o.segments == (("EWR", "2027-04-24", "PUJ", "DM", "5311"),)
    assert o.arrive_airport == "PUJ"


def test_returns_are_filtered_by_where_they_land():
    raw = [
        item(1700, "JetBlue", leg("PUJ", "LGA", [15], [19], number=("B6", "9"))),   # lands LGA: out
        item(1690, "Arajet", leg("PUJ", "EWR", [19, 10], [23, 30], number=("DM", "5382"))),
    ]
    rets = options(raw, ("JFK", "EWR"), returning=True)
    assert [(r.flight_numbers, r.arrive_airport) for r in rets] == [("DM 5382", "EWR")]


def test_outbounds_are_repriced_by_their_cheapest_eligible_return(monkeypatch):
    from types import SimpleNamespace

    from family_resort_watch import flights as fl
    from family_resort_watch.scan import _with_returns

    cheap = parse_option(item(1000, "Spirit", leg("EWR", "PUJ", [6], [10], number=("NK", "1"))))
    pricey = parse_option(item(1500, "United", leg("EWR", "PUJ", [8], [12], number=("UA", "2"))))
    unchecked = parse_option(item(1600, "Delta", leg("JFK", "PUJ", [9], [13], number=("DL", "3"))))
    back = {
        "NK 1": [],  # its only return lands at LGA, so options() left nothing
        "UA 2": [parse_option(item(1550, "United", leg("PUJ", "EWR", [16], [20], number=("UA", "4"))))],
    }
    monkeypatch.setattr(fl, "pause", lambda s: None)
    monkeypatch.setattr(fl, "returns_for", lambda s, d, a, b, o: back[o.flight_numbers])
    s = SimpleNamespace(return_lookups_per_route=2)
    counts = {"return_lookups": 0, "return_errors": 0}
    opts, found = _with_returns(s, fl, "PUJ", SimpleNamespace(checkin="2027-04-24", checkout="2027-04-29"),
                                [cheap, pricey, unchecked], counts)
    # Spirit is dropped, United takes its paired return's price, Delta (past the cap) keeps its own.
    assert [(o.flight_numbers, o.price) for o in opts] == [("UA 2", 1550), ("DL 3", 1600)]
    assert found[opts[0]][0][0].flight_numbers == "UA 4" and opts[1] not in found
    assert counts["return_lookups"] == 2


def test_search_merges_origins_and_remembers_which_found_each(monkeypatch):
    from family_resort_watch import flights as fl

    by_origin = {
        "ORD": [item(900, "United", leg("ORD", "CUN", [8], [12], number=("UA", "1"))),
                item(950, "Spirit", leg("MKE", "CUN", [9], [13], number=("NK", "2")))],   # not a home airport
        "MDW": [item(880, "Southwest", leg("MDW", "CUN", [7], [11], number=("WN", "3"))),
                item(900, "United", leg("ORD", "CUN", [8], [12], number=("UA", "1")))],   # seen from ORD too
    }
    seen = []

    def fake(s, query, what):
        origin = what.split("-")[0]
        seen.append(origin)
        return by_origin[origin]

    monkeypatch.setattr(fl, "_items_with_retries", fake)
    monkeypatch.setattr(fl, "pause", lambda s: None)
    s = fl.FlightSettings(
        origins=("ORD", "MDW"), departure_airports=("ORD", "MDW"), home_label="Chicago", max_stops=0,
        proxy_adults=2, exclude_basic_economy=True, carry_on_bags=1, checked_bags=0,
        request_delay_seconds=0, retries=0, every_hours=6, flex_every_hours=12,
    )
    count, found = fl.search(s, "CUN", "2027-04-24", "2027-04-29")
    assert seen == ["ORD", "MDW"] and count == 4
    assert [(o.flight_numbers, o.origin) for o in found] == [("WN 3", "MDW"), ("UA 1", "ORD")]
