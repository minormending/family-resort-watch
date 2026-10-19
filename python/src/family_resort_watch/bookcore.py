"""Client for the Inclusive Collection booking engine ("bookcore").

Two things come from it:

* the hotel list, embedded (JS-escaped) in the booking homepage, and
* per-hotel availability for one stay and one party, from the same JSON
  endpoint the booking page calls.

No login is needed. An anonymous response carries the World of Hyatt member
price in `locked_loyalty_price`; a logged-in response puts it in `total`
instead. `member_total()` reads whichever is set.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import Party

BASE = "https://bookings.hyattinclusivecollection.com"
AVAILABILITY_PATH = "/bookcore/ajax/desktop/availability/rooms/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
ALL_INCLUSIVE = "TI"


@dataclass(frozen=True)
class Hotel:
    code: str
    name: str
    country: str
    area: str


@dataclass(frozen=True)
class Offer:
    room_code: str
    room_name: str
    rate_code: str
    rate_name: str
    board: str
    refundable: bool
    free_cancel_until: str | None
    public_total: float
    member_total: float
    rack_total: float
    inventory: int | None
    two_beds: bool


@dataclass(frozen=True)
class Availability:
    hotel_code: str
    hotel_name: str
    status: str  # ok | no_party_rooms | no_rooms
    message: str
    room_types: int
    offers: tuple[Offer, ...]


class FetchError(RuntimeError):
    pass


def _get(url: str, timeout: int, retries: int, accept: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "X-Requested-With": "XMLHttpRequest",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise FetchError(f"{url}: {last}")


def availability_url(hotel_code: str, checkin: str, checkout: str, party: Party) -> str:
    params = {
        "adults": party.adults,
        "babies": party.babies,
        "checkin": checkin,
        "checkout": checkout,
        "children-ages": ",".join(str(a) for a in party.child_ages if a >= 2),
        "hotelCode": hotel_code,
        "isDesktop": "true",
        "ninos": party.children,
        "occp": 1,
        "rrc": 1,
        "lang": "en",
    }
    return f"{BASE}{AVAILABILITY_PATH}?{urllib.parse.urlencode(params)}"


def booking_url(hotel_code: str, checkin: str, checkout: str, party: Party) -> str:
    """The booking site's own results page for one resort, stay and party.

    Format taken from the site's own address bar (2026-10-03): the path
    carries "<adults>;<child ages>", and `occupancies` is a JSON list that the
    site URL-encodes twice.
    """
    ages = [a for a in party.child_ages if a >= 2]
    # json.dumps' default ", " / ": " separators are what the site sends.
    occupancies = json.dumps(
        [{"adults": party.adults, "children": len(ages), "ages": ",".join(str(a) for a in ages)}]
    )
    twice = urllib.parse.quote(urllib.parse.quote(occupancies, safe=""), safe="")
    party_path = ";".join([str(party.adults), *(str(a) for a in ages)])
    query = (
        f"rrc=1&adults={party.adults}&children-ages={','.join(str(a) for a in ages)}"
        f"&ninos={len(ages)}&occupancies={twice}&occp=1"
    )
    return f"{BASE}/bookcore/availability/{hotel_code}/{checkin}/{checkout}/{party_path}/?{query}"


def fetch_availability_raw(
    hotel_code: str, checkin: str, checkout: str, party: Party, timeout: int = 30, retries: int = 2
) -> dict:
    body = _get(availability_url(hotel_code, checkin, checkout, party), timeout, retries, "application/json")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{hotel_code} {checkin}: response was not JSON ({body[:120]!r})") from exc


def member_total(board: dict) -> float:
    locked = float(board.get("locked_loyalty_price") or 0)
    return locked if locked > 0 else float(board["total"])


def _two_beds(room_name: str) -> bool:
    name = room_name.lower()
    return any(k in name for k in ("double", "two bed", "2 bed", "queen", "family", "bedroom"))


def parse_availability(raw: dict, party: Party) -> Availability:
    offers: list[Offer] = []
    rooms = raw.get("roomData") or []
    for room in rooms:
        info = room["parsedRoomData"]
        room_name = " ".join(info["nombre"].split())
        for rate in info.get("tarifas", []):
            free_cancel = (rate.get("cancelacion_gratis") or {}).get("formato1")
            for occ in rate.get("ocupaciones", []):
                if occ["codigo"] != party.code:
                    continue
                for board in occ.get("pensiones", []):
                    if board["codigo"] != ALL_INCLUSIVE:
                        continue
                    offers.append(
                        Offer(
                            room_code=info["codigo"],
                            room_name=room_name,
                            rate_code=rate["codigo"],
                            rate_name=rate["nombre"],
                            board=board["codigo"],
                            refundable=bool(board.get("cancelable")),
                            free_cancel_until=free_cancel,
                            public_total=round(float(board["total"]), 2),
                            member_total=round(member_total(board), 2),
                            rack_total=round(float(board.get("total_antes") or board["total"]), 2),
                            inventory=board.get("cupo"),
                            two_beds=_two_beds(room_name),
                        )
                    )
    if offers:
        status = "ok"
    elif rooms:
        # Adults-only resorts land here: rooms come back, none priced for a child.
        status = "no_party_rooms"
    else:
        status = "no_rooms"
    message = re.sub(r"<[^>]+>", "", raw.get("message") or "").strip()
    return Availability(
        hotel_code=raw.get("hotelCode", ""),
        hotel_name=(raw.get("hotelName") or "").strip(),
        status=status,
        message=message,
        room_types=len(rooms),
        offers=tuple(offers),
    )


_UNICODE_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})")


def parse_hotel_list(homepage_html: str) -> list[Hotel]:
    """Pull the destination > area > hotels tree out of the booking homepage."""
    text = html.unescape(_UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), homepage_html))
    decoder = json.JSONDecoder()
    best: list | None = None
    for match in re.finditer(r'\[\{"code": "~', text):
        try:
            tree, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if best is None or len(json.dumps(tree)) > len(json.dumps(best)):
            best = tree
    if not best:
        raise FetchError("hotel list not found in booking homepage")
    hotels: dict[str, Hotel] = {}
    for country in best:
        for area in country.get("states", []):
            for h in area.get("hotels", []):
                hotels.setdefault(
                    h["code"],
                    Hotel(code=h["code"], name=" ".join(h["name"].split()), country=country["name"], area=area["name"]),
                )
    return list(hotels.values())


def fetch_hotel_list(timeout: int = 30, retries: int = 2) -> list[Hotel]:
    body = _get(f"{BASE}/", timeout, retries, "text/html").decode("utf-8", "replace")
    return parse_hotel_list(body)
