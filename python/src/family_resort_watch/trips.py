"""Create a trip config from a few answers.

The setup skill interviews the user and writes their answers as JSON; this
module validates them and renders <home>/trips/<name>.toml, so the TOML is
never hand-written and every trip gets the same sensible defaults.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

# Countries (as the booking engine names them) with family resorts in the
# Americas; the engine also lists Spain, Portugal, Greece and Bulgaria.
DEFAULT_COUNTRIES = [
    "Mexico", "Dominican Republic", "Jamaica", "Aruba", "Curaçao",
    "Costa Rica", "Panamá", "Colombia", "Saint Lucia",
]

EXAMPLE = {
    "title": "Spring Break Resort Watch",
    "recess_label": "School spring break · Thu Apr 22 – Fri Apr 30, 2027",
    "recess": ["2027-04-22", "2027-04-30"],
    "party": {"adults": 2, "child_ages": [7, 4]},
    "home": {"airports": ["ORD", "MDW"], "origins": ["CHI"], "label": "Chicago"},
    "nonstop_only": True,
    "stays": {
        "preferred": [["2027-04-24", "2027-04-29"]],
        "also": [["2027-04-22", "2027-04-27"], ["2027-04-26", "2027-05-01"]],
    },
    "countries": DEFAULT_COUNTRIES,
}


class AnswerError(ValueError):
    pass


def _day(v, what: str) -> date:
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        raise AnswerError(f"{what}: {v!r} is not a YYYY-MM-DD date") from None


def _airport(v, what: str) -> str:
    if not isinstance(v, str) or not re.fullmatch(r"[A-Z]{3}", v):
        raise AnswerError(f"{what}: {v!r} is not a 3-letter airport or metro code")
    return v


def _q(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


def _list(vs) -> str:
    return "[" + ", ".join(_q(v) if isinstance(v, str) else str(v) for v in vs) + "]"


def validate(a: dict) -> dict:
    """Normalised answers, or AnswerError naming the first problem."""
    party = a.get("party") or {}
    adults = party.get("adults")
    if not isinstance(adults, int) or not 1 <= adults <= 3:
        raise AnswerError("party.adults must be 1-3 (one room)")
    ages = party.get("child_ages", [])
    if not isinstance(ages, list) or not all(isinstance(x, int) and 0 <= x <= 17 for x in ages):
        raise AnswerError("party.child_ages must be a list of ages 0-17")
    if adults + len(ages) > 5 or len(ages) > 3:
        raise AnswerError("the booking engine prices at most 5 guests and 3 children in one room")

    home = a.get("home") or {}
    airports = [_airport(x, "home.airports") for x in home.get("airports", [])]
    if not airports:
        raise AnswerError("home.airports needs at least one airport")
    origins = [_airport(x, "home.origins") for x in (home.get("origins") or airports)]

    stays = a.get("stays") or {}
    windows = []
    for tier, key in (("primary", "preferred"), ("flex", "also")):
        for pair in stays.get(key, []):
            if not isinstance(pair, list) or len(pair) != 2:
                raise AnswerError(f"stays.{key} entries are [checkin, checkout] pairs")
            ci, co = _day(pair[0], f"stays.{key} check-in"), _day(pair[1], f"stays.{key} check-out")
            if not 1 <= (co - ci).days <= 21:
                raise AnswerError(f"stays.{key}: {pair} must be 1-21 nights")
            if ci <= date.today():
                raise AnswerError(f"stays.{key}: {pair} is in the past")
            windows.append((tier, ci.isoformat(), co.isoformat()))
    if not any(t == "primary" for t, _, _ in windows):
        raise AnswerError("stays.preferred needs at least one stay")
    if len(windows) > 8:
        raise AnswerError("at most 8 stays; each one multiplies the searches")
    if len({(c, o) for _, c, o in windows}) != len(windows):
        raise AnswerError("the same stay is listed twice")

    days = sorted({d for _, c, o in windows for d in (c, o)})
    recess = a.get("recess") or [days[0], days[-1]]
    recess = [_day(recess[0], "recess").isoformat(), _day(recess[1], "recess").isoformat()]
    strip = [min(days[0], recess[0]), max(days[-1], recess[1])]
    title = str(a.get("title") or "Family Resort Watch").strip()[:60]
    return {
        "title": title,
        "recess_label": str(a.get("recess_label") or f"Trip window · {recess[0]} to {recess[1]}").strip()[:90],
        "recess": recess,
        "strip": strip,
        "stay_label": str(a.get("stay_label") or "Stays you're watching").strip()[:60],
        "adults": adults,
        "child_ages": ages,
        "airports": airports,
        "origins": origins,
        "home_label": str(home.get("label") or " / ".join(airports)).strip()[:40],
        "max_stops": 0 if a.get("nonstop_only", True) else 1,
        "windows": windows,
        "countries": list(a.get("countries") or DEFAULT_COUNTRIES),
    }


def render(a: dict) -> str:
    v = validate(a)
    # Google returns nothing for most international routes when a child is in
    # the search, so fares are searched for that many adult seats instead.
    seats = v["adults"] + sum(1 for x in v["child_ages"] if x >= 2)
    lines = [
        "# Made by `frw new-trip`. Edit freely; `frw --trip <name> check` validates it.",
        "",
        "[trip]",
        f"title = {_q(v['title'])}",
        f"recess_label = {_q(v['recess_label'])}",
        f"recess = {_list(v['recess'])}    # days off, highlighted on the page",
        f"strip = {_list(v['strip'])}     # days drawn on the page",
        f"stay_label = {_q(v['stay_label'])}",
        "",
        "[party]",
        f"adults = {v['adults']}",
        f"child_ages = {_list(v['child_ages'])}   # the engine treats 2-12 as child, under 2 as baby",
        "",
        "# preferred stays are checked every run; the others every 6 hours.",
    ]
    for tier, ci, co in v["windows"]:
        lines += ["", "[[windows]]", f'checkin = "{ci}"', f'checkout = "{co}"', f'tier = "{tier}"']
        if tier == "flex":
            lines.append("every_hours = 6")
    lines += [
        "",
        "[scope]",
        f"countries = {_list(v['countries'])}",
        "reclassify_days = 7   # re-check which resorts take children weekly",
        "",
        "[fetch]",
        "workers = 4",
        "timeout_seconds = 30",
        "retries = 2",
        "",
        "[flights]",
        f"home_airports = {_list(v['airports'])}   # flights must leave from and return to one of these",
        f"origins = {_list(v['origins'])}          # what to search from: airports, or a metro code like NYC",
        f"home_label = {_q(v['home_label'])}",
        f"max_stops = {v['max_stops']}                   # 0 = nonstop only",
        f"proxy_adults = {seats}                # seats to price; see flights.py on child searches",
        "exclude_basic_economy = true",
        "carry_on_bags = 1",
        "checked_bags = 0",
        "request_delay_seconds = 2.5",
        "retries = 2",
        "every_hours = 6",
        "flex_every_hours = 12",
        "return_lookups_per_route = 20",
        "fare_tiers = true              # price each airline's tier with free seat selection (needs Chrome)",
        "fare_lookup_every_hours = 48",
        "fare_lookups_per_sweep = 20",
        "",
        "# Override which airport serves an area here, e.g.",
        '# [airports]',
        '# "Riviera Maya" = ["CUN", "TQO"]',
        "",
    ]
    return "\n".join(lines)


def create(home: Path, name: str, answers: dict, overwrite: bool = False) -> Path:
    path = home / "trips" / f"{name}.toml"
    if path.exists() and not overwrite:
        raise AnswerError(f"trip {name!r} already exists at {path}; pass --overwrite to replace it")
    text = render(answers)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


ARTIFACT_LINE = re.compile(r'^artifact = ".*"$', re.M)


def set_artifact(config_path: Path, url: str) -> None:
    """Remember the trip's artifact URL in its own config, under [trip]."""
    if not re.fullmatch(r"https://claude\.ai/(code/)?artifact/[\w-]+", url):
        raise AnswerError(f"{url!r} doesn't look like a claude.ai artifact link")
    text = config_path.read_text()
    line = f'artifact = "{url}"'
    text = ARTIFACT_LINE.sub(line, text) if ARTIFACT_LINE.search(text) else text.replace("[trip]\n", f"[trip]\n{line}\n", 1)
    config_path.write_text(text)
