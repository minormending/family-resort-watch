import gzip
import json
from pathlib import Path

from family_resort_watch.bookcore import booking_url, parse_availability, parse_hotel_list
from family_resort_watch.config import Party

FIXTURES = Path(__file__).parent / "fixtures"
FAMILY = Party(adults=2, child_ages=(5,))


def load(name: str) -> dict:
    return json.loads(gzip.decompress((FIXTURES / name).read_bytes()))


def test_party_code_splits_babies_from_children():
    assert FAMILY.code == "2-1-0"
    assert Party(adults=2, child_ages=(1, 5, 7)).code == "2-2-1"


def test_anonymous_response_yields_member_price():
    result = parse_availability(load("family_bpgrandrom_anon.json.gz"), FAMILY)
    assert result.status == "ok"
    nonref = [o for o in result.offers if o.room_code == "jdbl" and not o.refundable]
    assert len(nonref) == 1
    # Logged-in total for the same search was 1553.68; anonymous total is 1726.31.
    assert nonref[0].member_total == 1553.68
    assert nonref[0].public_total == 1726.31
    assert nonref[0].two_beds is True


def test_refundable_rate_carries_free_cancel_date():
    result = parse_availability(load("family_bpgrandrom_anon.json.gz"), FAMILY)
    flex = [o for o in result.offers if o.refundable]
    assert flex and all(o.free_cancel_until == "2027-04-21" for o in flex)


def test_adults_only_resort_returns_rooms_but_no_offers():
    result = parse_availability(load("adults_only_secun.json.gz"), FAMILY)
    assert result.status == "no_party_rooms"
    assert result.room_types > 0 and result.offers == ()


def test_hotel_list_parses_js_escaped_tree():
    tree = [
        {"code": "~DO", "name": "Dominican Republic", "states": [
            {"code": "#laromana", "name": "La Romana", "hotels": [
                {"code": "bpgrandrom", "name": " Bahia Principe Explore La Romana"},
            ]},
        ]},
    ]
    # The homepage embeds the tree HTML-entity-quoted inside a JS string.
    page = "<script>var x = " + json.dumps(tree).replace('"', "\\u0026quot\\u003B") + ";</script>"
    hotels = parse_hotel_list(page)
    assert [(h.code, h.name, h.country, h.area) for h in hotels] == [
        ("bpgrandrom", "Bahia Principe Explore La Romana", "Dominican Republic", "La Romana")
    ]


def test_booking_url_matches_the_sites_own_link():
    # Copied from the booking site's address bar for this exact search.
    expected = (
        "https://bookings.hyattinclusivecollection.com/bookcore/availability/bpgrandrom/2027-04-24/2027-04-29/2;5/"
        "?rrc=1&adults=2&children-ages=5&ninos=1&occupancies=%255B%257B%2522adults%2522%253A%25202%252C%2520"
        "%2522children%2522%253A%25201%252C%2520%2522ages%2522%253A%2520%25225%2522%257D%255D&occp=1"
    )
    assert booking_url("bpgrandrom", "2027-04-24", "2027-04-29", FAMILY) == expected
