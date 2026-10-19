"""One-time family-fit research: load, validate and score it.

Researchers rate seven criteria 1-5 against the anchors in
research/RUBRIC.md; `family_fit` turns those into a score out of 10 with
fixed weights, so the overall number is reproducible and comparable across
resorts researched by different people (or agents). The research itself is
static: it is not refreshed by the hourly run.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config

# Weights sum to 1. A young child's days are the kids' club, the pool and the
# beach, so those carry most of it; transfer matters but is a one-off.
WEIGHTS = {
    "kids_club": 0.22,
    "water_play": 0.15,
    "beach": 0.18,
    "rooms": 0.10,
    "food": 0.15,
    "transfer": 0.08,
    "condition": 0.12,
}
SARGASSUM = {"low", "moderate", "high", "unknown"}
CONFIDENCE = {"high", "medium", "low"}


def family_fit(criteria: dict) -> float:
    """Weighted 1-5 ratings as a score out of 10, one decimal."""
    total = sum(WEIGHTS[k] * criteria[k]["score"] for k in WEIGHTS)
    return round(total / 5 * 10, 1)


def _sources_ok(sources) -> bool:
    return isinstance(sources, list) and all(
        isinstance(s, dict) and str(s.get("url", "")).startswith("http") for s in sources
    )


def validate_resort(doc: dict, code: str) -> list[str]:
    """Problems with one review; empty when it matches research/RUBRIC.md."""
    errors = []
    if doc.get("code") != code:
        errors.append(f"code is {doc.get('code')!r}, file says {code!r}")
    crit = doc.get("criteria") or {}
    for k in WEIGHTS:
        c = crit.get(k)
        if not isinstance(c, dict) or not isinstance(c.get("score"), int) or not 1 <= c["score"] <= 5:
            errors.append(f"criteria.{k}.score must be an integer 1-5")
        elif not str(c.get("evidence", "")).strip():
            errors.append(f"criteria.{k}.evidence is empty")
    for k in ("pros", "cons"):
        items = doc.get(k)
        if not isinstance(items, list) or not 2 <= len(items) <= 6 or not all(isinstance(i, str) and i for i in items):
            errors.append(f"{k} must be 2-6 non-empty strings")
    if not str(doc.get("verdict", "")).strip():
        errors.append("verdict is empty")
    if doc.get("sargassum_late_april") not in SARGASSUM:
        errors.append(f"sargassum_late_april must be one of {sorted(SARGASSUM)}")
    if doc.get("confidence") not in CONFIDENCE:
        errors.append(f"confidence must be one of {sorted(CONFIDENCE)}")
    if not _sources_ok(doc.get("sources")) or len(doc.get("sources") or []) < 3:
        errors.append("sources must be 3+ entries with http(s) urls")
    return errors


SHIPPED_NOTE = (
    "Family-fit reviews ship with the tool: researched in October 2026 for two adults and a "
    "young child travelling in late April. Kids' club and seaweed notes may read differently "
    "for your family and dates; run the research skill to redo them for yours."
)


def uses_shipped(codes) -> bool:
    """Whether any of these resorts is shown with a shipped review rather than the user's own."""
    shipped, own = config.PACKAGE_DATA / "research" / "resorts", config.HOME / "research" / "resorts"
    return any((shipped / f"{c}.json").exists() and not (own / f"{c}.json").exists() for c in codes)


def load_resorts(directory: Path | None = None) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """({code: review with "fit"}, {code: problems}) for every research/resorts/*.json.

    The user's own research (<home>/research/resorts/) wins over the shipped
    starter set, file by file.
    """
    if directory is None:
        reviews_, problems_ = {}, {}
        for d in (config.PACKAGE_DATA / "research" / "resorts", config.HOME / "research" / "resorts"):
            if d.exists():
                r, p = load_resorts(d)
                reviews_.update(r)
                problems_.update(p)
                for code in r:
                    problems_.pop(code, None)
        return reviews_, problems_
    reviews, problems = {}, {}
    for path in sorted(directory.glob("*.json")):
        code = path.stem
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            problems[code] = [f"not valid JSON: {exc}"]
            continue
        errors = validate_resort(doc, code)
        if errors:
            problems[code] = errors
            continue
        reviews[code] = {**doc, "fit": family_fit(doc["criteria"])}
    return reviews, problems


def load_airlines(path: Path | None = None) -> dict:
    path = path or config.data_file("research", "airlines.json")
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    for name, a in (doc.get("airlines") or {}).items():
        if not _sources_ok(a.get("sources", [])):
            raise ValueError(f"airlines.json: {name} has a source without an http(s) url")
    return doc


def thinness(doc: dict) -> list[str]:
    """Why a review needs another pass; empty when it is solid enough.

    Thin means: low confidence, three or more claims marked unverified, or
    fewer than three sources that were read directly rather than seen as
    search-result excerpts (researchers label those "via search ...").
    """
    reasons = []
    if doc.get("confidence") == "low":
        reasons.append("low confidence")
    text = json.dumps(doc, ensure_ascii=False).lower()
    unverified = text.count("unverified")
    if unverified >= 3:
        reasons.append(f"{unverified} unverified claims")
    direct = [
        s for s in doc.get("sources", [])
        if "excerpt" not in str(s.get("title", "")).lower() and "via search" not in str(s.get("title", "")).lower()
    ]
    if len(direct) < 3:
        reasons.append(f"only {len(direct)} directly read sources")
    return reasons
