from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Shipped with the package: resort notes, the page template, starter research.
PACKAGE_DATA = Path(__file__).resolve().parent / "data"

# Everything a user owns lives under one home folder (the plugin's data folder
# when installed as a Claude Code plugin):
#
#   <home>/trips/<trip>.toml        what to watch
#   <home>/data/<trip>.db           price history
#   <home>/data/raw/<trip>/         latest raw responses, for debugging
#   <home>/data/<trip>-published.json  what the artifact db already holds
#   <home>/report/<trip>/           changes.md, analysis.json, db/*.json
#   <home>/page/<trip>.html         the artifact page
#   <home>/flights.lock             one flight sweep at a time, across trips
#   <home>/research/, resorts.toml  optional overrides of the shipped data
DEFAULT_HOME = Path(os.environ.get("FRW_HOME", Path.home() / ".family-resort-watch")).expanduser()

HOME = DEFAULT_HOME
TRIP = ""
CONFIG_PATH = DB_PATH = RAW_DIR = REPORT_DIR = PUBLISHED_PATH = PAGE_PATH = Path()


def select(home: Path | str, trip: str | None = None, must_exist: bool = True) -> None:
    """Point the module's paths at one home folder and, optionally, one trip in it."""
    global HOME, TRIP, CONFIG_PATH, DB_PATH, RAW_DIR, REPORT_DIR, PUBLISHED_PATH, PAGE_PATH
    HOME = Path(home).expanduser().resolve()
    TRIP = trip or ""
    if not trip:
        return
    if not trip.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"trip names use letters, digits, - and _: {trip!r}")
    CONFIG_PATH = HOME / "trips" / f"{trip}.toml"
    DB_PATH = HOME / "data" / f"{trip}.db"
    RAW_DIR = HOME / "data" / "raw" / trip
    REPORT_DIR = HOME / "report" / trip
    PUBLISHED_PATH = HOME / "data" / f"{trip}-published.json"
    PAGE_PATH = HOME / "page" / f"{trip}.html"
    if must_exist and not CONFIG_PATH.exists():
        raise FileNotFoundError(f"no trip {trip!r}: {CONFIG_PATH} does not exist (run `frw new-trip` first)")


def trips(home: Path | None = None) -> list[str]:
    folder = (home or HOME) / "trips"
    return sorted(p.stem for p in folder.glob("*.toml")) if folder.exists() else []


def data_file(*parts: str) -> Path:
    """A shipped data file, unless the user's home has its own copy."""
    own = HOME.joinpath(*parts)
    return own if own.exists() else PACKAGE_DATA.joinpath(*parts)


@dataclass(frozen=True)
class Party:
    adults: int
    child_ages: tuple[int, ...]

    @property
    def babies(self) -> int:
        return sum(1 for a in self.child_ages if a < 2)

    @property
    def children(self) -> int:
        return len(self.child_ages) - self.babies

    @property
    def code(self) -> str:
        """Occupancy key the booking engine uses: adults-children-babies."""
        return f"{self.adults}-{self.children}-{self.babies}"


@dataclass(frozen=True)
class Window:
    checkin: str
    checkout: str
    tier: str
    every_hours: int = 1

    @property
    def key(self) -> str:
        return f"{self.checkin}/{self.checkout}"

    @property
    def nights(self) -> int:
        return (date.fromisoformat(self.checkout) - date.fromisoformat(self.checkin)).days


@dataclass(frozen=True)
class Config:
    party: Party
    windows: tuple[Window, ...]
    countries: frozenset[str]
    reclassify_days: int
    workers: int
    timeout_seconds: int
    retries: int
    airports: dict[str, tuple[str, ...]] = field(default_factory=dict)
    flights: "FlightSettings | None" = None
    # Display facts for the page: title, the recess dates, the strip of days.
    trip: dict = field(default_factory=dict)

    @property
    def primary(self) -> tuple[Window, ...]:
        return tuple(w for w in self.windows if w.tier == "primary")


def load(path: Path | None = None) -> Config:
    raw = tomllib.loads((path or CONFIG_PATH).read_text())
    windows = tuple(
        Window(
            checkin=w["checkin"],
            checkout=w["checkout"],
            tier=w["tier"],
            every_hours=int(w.get("every_hours", 1)),
        )
        for w in raw["windows"]
    )
    if not any(w.tier == "primary" for w in windows):
        raise ValueError(f"{CONFIG_PATH.name} needs at least one primary window")
    return Config(
        party=Party(adults=raw["party"]["adults"], child_ages=tuple(raw["party"]["child_ages"])),
        windows=windows,
        countries=frozenset(raw["scope"]["countries"]),
        reclassify_days=int(raw["scope"]["reclassify_days"]),
        workers=int(raw["fetch"]["workers"]),
        timeout_seconds=int(raw["fetch"]["timeout_seconds"]),
        retries=int(raw["fetch"]["retries"]),
        # Shipped area -> airport map, with this trip's own overrides on top.
        airports={
            area: (codes,) if isinstance(codes, str) else tuple(codes)
            for area, codes in {
                **tomllib.loads(data_file("airports.toml").read_text()).get("airports", {}),
                **raw.get("airports", {}),
            }.items()
        },
        flights=_flights(raw.get("flights")),
        trip=dict(raw.get("trip", {})),
    )


def _flights(raw: dict | None):
    if not raw:
        return None
    from .flights import FlightSettings

    home_airports = tuple(raw["home_airports"])
    return FlightSettings(
        # Google codes to search from: an airport, or a metro code such as NYC
        # or CHI that covers several. Defaults to searching each home airport.
        origins=tuple(raw.get("origins") or home_airports),
        departure_airports=home_airports,
        home_label=raw.get("home_label") or " / ".join(home_airports),
        max_stops=raw.get("max_stops"),
        proxy_adults=int(raw["proxy_adults"]),
        exclude_basic_economy=bool(raw["exclude_basic_economy"]),
        carry_on_bags=int(raw["carry_on_bags"]),
        checked_bags=int(raw["checked_bags"]),
        request_delay_seconds=float(raw["request_delay_seconds"]),
        retries=int(raw["retries"]),
        every_hours=int(raw["every_hours"]),
        flex_every_hours=int(raw["flex_every_hours"]),
        return_lookups_per_route=int(raw.get("return_lookups_per_route", 20)),
    )


def load_notes(path: Path | None = None) -> dict:
    """Hand-researched facts: kids club by brand, award chart, award category per resort."""
    path = path or data_file("resorts.toml")
    if not path.exists():
        return {"brand": {}, "award": {}, "resort": {}}
    raw = tomllib.loads(path.read_text())
    return {"brand": raw.get("brand", {}), "award": raw.get("award", {}), "resort": raw.get("resort", {})}
