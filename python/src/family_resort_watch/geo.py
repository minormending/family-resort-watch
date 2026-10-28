"""Where each resort is, and how long the drive is from each of its airports.

Locations live in geo.toml, shipped with the tool and found once and checked by hand (OpenStreetMap,
the resort's own site), with the source next to each point: the booking
engine gives no coordinates, and a fuzzy geocoder happily puts a resort at
the hotel next door. Drive times come from OSRM's public router on
OpenStreetMap roads, once per resort and airport, since roads don't move.

OSRM drives at the speed limit with no traffic and no shuttle stops, so a
real transfer usually runs 10-30% longer. The page says so.

Where OpenStreetMap is missing a resort's access road, OSRM wanders in over
service tracks; a resort can then carry a `road` point (its entrance on the
mapped road) that routing uses instead. `slow` flags drives that average
under SLOW_KMH so they get that look.
"""

from __future__ import annotations

import json
import logging
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

OSRM = "https://router.project-osrm.org/route/v1/driving/{a};{b}?overview=false"
USER_AGENT = "family-resort-watch (resort transfer times; one request per resort and airport)"
SLOW_KMH = 30


class GeoError(RuntimeError):
    pass


def own_path() -> Path:
    """The user's geo.toml, where `frw geo` writes: resorts and drives the tool did not ship."""
    return config.HOME / "geo.toml"


def load(path: Path | None = None) -> dict:
    """The shipped geo.toml with the user's own on top (or just `path`, when given)."""
    out: dict = {"airports": {}, "resorts": {}, "drive": {}}
    for p in ((path,) if path else (config.PACKAGE_DATA / "geo.toml", own_path())):
        raw = tomllib.loads(p.read_text()) if p.exists() else {}
        for key in out:
            for code, value in raw.get(key, {}).items():
                out[key][code] = {**out[key].get(code, {}), **value} if key == "drive" else value
    return out


def drive_minutes(geo: dict, code: str) -> dict[str, int]:
    """{airport: minutes} for one resort, for the airports with a known drive."""
    return {ap: d["min"] for ap, d in (geo["drive"].get(code) or {}).items()}


def route(a: dict, b: dict, timeout: float = 30) -> tuple[int, float]:
    """(minutes, km) by road between two {lat, lon} points."""
    url = OSRM.format(a=f"{a['lon']},{a['lat']}", b=f"{b['lon']},{b['lat']}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.load(resp)
    except urllib.error.HTTPError as exc:  # OSRM explains a refused route in the body
        try:
            doc = json.load(exc)
        except ValueError:
            raise GeoError(f"router: {exc}") from exc
    except OSError as exc:
        raise GeoError(f"router: {exc}") from exc
    if doc.get("code") != "Ok" or not doc.get("routes"):
        raise GeoError(f"router: {doc.get('code')} {doc.get('message', '')}".strip())
    r = doc["routes"][0]
    return round(r["duration"] / 60), round(r["distance"] / 1000, 1)


def fill(geo: dict, wanted: dict[str, tuple[str, ...]], pause: float = 1.0) -> tuple[list[str], list[str], list[str]]:
    """Route every (resort, airport) in `wanted` that has no drive yet.

    Returns (what was added, what is missing a location, what could not be
    routed). A pair with no road (an island resort reached by ferry) stays
    without a drive time.
    """
    added, missing, failed = [], [], []
    for code, airports in sorted(wanted.items()):
        spot = geo["resorts"].get(code)
        if not spot:
            missing.append(code)
            continue
        for ap in airports:
            if ap in geo["drive"].get(code, {}):
                continue
            port = geo["airports"].get(ap)
            if not port:
                missing.append(ap)
                continue
            time.sleep(pause)
            try:
                minutes, km = route(port, spot.get("road") or spot)
            except GeoError as exc:
                log.warning("%s from %s: %s", code, ap, exc)
                failed.append(f"{code} {ap}: {exc}")
                continue
            geo["drive"].setdefault(code, {})[ap] = {"min": minutes, "km": km}
            added.append(f"{code} {ap} {minutes} min")
    return added, sorted(set(missing)), failed


def slow(geo: dict) -> list[str]:
    """Drives averaging under SLOW_KMH: usually a point OSRM can't reach by a mapped road."""
    return [
        f"{code} {ap}: {d['km']} km in {d['min']} min"
        for code, by_ap in sorted(geo["drive"].items())
        for ap, d in sorted(by_ap.items())
        if d["min"] and d["km"] / (d["min"] / 60) < SLOW_KMH
    ]


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def dump(geo: dict) -> str:
    """geo.toml, in a stable order so diffs stay readable."""
    lines = [
        "# Resort and airport locations, and drive times between them. See geo.py.",
        "# Locations were found once and checked by hand; `source` says where each came from.",
        "# `frw geo` routes any resort and airport pair that has no drive yet.",
        "",
        "[airports]",
    ]
    for ap, p in sorted(geo["airports"].items()):
        lines.append(f"{ap} = {{ lat = {p['lat']}, lon = {p['lon']}, name = {_q(p.get('name', ''))} }}")
    lines += ["", "[resorts]"]
    for code, p in sorted(geo["resorts"].items()):
        road = f", road = {{ lat = {p['road']['lat']}, lon = {p['road']['lon']} }}" if p.get("road") else ""
        lines.append(f"{code} = {{ lat = {p['lat']}, lon = {p['lon']}{road}, source = {_q(p.get('source', ''))} }}")
    lines += ["", "# Minutes and km by road from each airport (OSRM, no traffic)."]
    for code, by_ap in sorted(geo["drive"].items()):
        lines += ["", f"[drive.{code}]"]
        for ap, d in sorted(by_ap.items()):
            lines.append(f"{ap} = {{ min = {d['min']}, km = {d['km']} }}")
    return "\n".join(lines) + "\n"
