"""How each stay moves over time, as a whole rather than resort by resort.

For every stay window and every hotel scan, four numbers over the resorts
that take the party and have a kids' club:

* median_trip   — median of (resort member total + its cheapest fare)
* cheapest_trip — the single cheapest trip, and which resort it was
* median_hotel  — median resort member total, flights ignored
* cheapest_fare — cheapest family fare to any of those resorts' airports

Fares are matched "as of" the scan: the latest search of that route at or
before the scan started. A resort whose airports have no fare yet counts for
the hotel numbers but not the trip ones.

The median follows the whole stay; the cheapest trip can jump when the leader
changes. Points are kept per scan for the last few days and one per day (the
day's last scan) before that, so the document stays small as months pass.
"""

from __future__ import annotations

import bisect
import statistics
from datetime import datetime, timedelta, timezone

COLUMNS = ["t", "median_trip", "cheapest_trip", "median_hotel", "cheapest_fare", "cheapest_code"]
KEEP_EVERY_POINT = timedelta(days=3)
# A route whose first fare arrives this long after the first sweep was added
# later (a config change), which is worth marking on the chart.
LATE_ROUTE = timedelta(hours=3)


def _epoch(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp())


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


class _Fares:
    """As-of lookup of the family fare per (airport, stay)."""

    def __init__(self, fseries: dict):
        self.routes: dict[tuple[str, str], tuple[list[str], list[float | None]]] = {}
        for (dest, depart, ret), fetches in fseries.items():
            times = [f["fetched_at"] for f in fetches]
            self.routes[(dest, f"{depart}/{ret}")] = (times, [f["family"] for f in fetches])

    def at(self, airport: str, stay: str, when: str) -> float | None:
        route = self.routes.get((airport, stay))
        if not route:
            return None
        times, prices = route
        i = bisect.bisect_right(times, when) - 1
        return prices[i] if i >= 0 else None


def _thin(points: list[list], now: datetime) -> list[list]:
    """Every point in the last few days; before that, each UTC day's last point."""
    cutoff = (now - KEEP_EVERY_POINT).timestamp()
    by_day: dict[str, list] = {}
    recent = []
    for p in points:
        if p[0] >= cutoff:
            recent.append(p)
        else:
            by_day[datetime.fromtimestamp(p[0], timezone.utc).date().isoformat()] = p
    return [by_day[d] for d in sorted(by_day)] + recent


def stay_trends(series: dict, fseries: dict, resorts: list[dict], windows, scan_times: dict[int, str],
                now: datetime) -> dict:
    """The sbw/trends document. `series` and `fseries` are report's per-fetch histories."""
    fares = _Fares(fseries)
    family = [r for r in resorts if r.get("kids_club")]
    stays: dict[str, list[list]] = {}
    for w in windows:
        by_scan: dict[int, list[tuple[str, float]]] = {}
        for r in family:
            for f in series.get((r["code"], w.checkin, w.checkout), []):
                if f["best"] is not None:
                    by_scan.setdefault(f["scan_id"], []).append((r["code"], f["best"]))
        airports_of = {r["code"]: r["airports"] for r in family}
        points = []
        for scan_id in sorted(by_scan):
            when = scan_times.get(scan_id)
            if when is None:
                continue
            hotels = by_scan[scan_id]
            trips, all_fares = [], []
            for code, hotel in hotels:
                found = [p for p in (fares.at(ap, w.key, when) for ap in airports_of[code]) if p is not None]
                all_fares += found
                if found:
                    trips.append((hotel + min(found), code))
            cheapest = min(trips) if trips else None
            points.append([
                _epoch(when),
                _round(statistics.median(t for t, _ in trips)) if trips else None,
                _round(cheapest[0]) if cheapest else None,
                _round(statistics.median(h for _, h in hotels)),
                _round(min(all_fares)) if all_fares else None,
                cheapest[1] if cheapest else None,
            ])
        stays[w.key] = _thin(points, now)

    # Routes that only started being searched after the first sweep: a change
    # in what is watched, not in prices.
    first_seen: dict[str, str] = {}
    for (dest, _, _), fetches in fseries.items():
        if fetches:
            t = fetches[0]["fetched_at"]
            if dest not in first_seen or t < first_seen[dest]:
                first_seen[dest] = t
    annotations = []
    if first_seen:
        start = datetime.fromisoformat(min(first_seen.values()))
        for dest, t in sorted(first_seen.items(), key=lambda kv: kv[1]):
            if datetime.fromisoformat(t) - start > LATE_ROUTE:
                annotations.append({"t": _epoch(t), "label": f"{dest} flights added"})
    return {
        "schema": 1,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "columns": COLUMNS,
        "stays": stays,
        "annotations": annotations,
    }


def summary(doc: dict, windows, now: datetime) -> list[dict]:
    """Per stay: latest median and cheapest trip, with changes over 24 hours and 7 days."""
    out = []
    for w in windows:
        pts = [p for p in doc["stays"].get(w.key, []) if p[1] is not None]
        if not pts:
            continue
        last = pts[-1]

        def ago(delta):
            t = (now - delta).timestamp()
            older = [p for p in pts if p[0] <= t]
            return older[-1] if older else None

        def change(old, col):
            return None if old is None or old[col] is None or last[col] is None else round(last[col] - old[col], 2)

        day, week = ago(timedelta(hours=24)), ago(timedelta(days=7))
        out.append({
            "stay": w.key,
            "tier": w.tier,
            "median_trip": last[1],
            "cheapest_trip": last[2],
            "cheapest_code": last[5],
            "median_trip_24h": change(day, 1),
            "median_trip_7d": change(week, 1),
            "cheapest_trip_7d": change(week, 2),
            "since": datetime.fromtimestamp(pts[0][0], timezone.utc).date().isoformat(),
            "median_trip_since": round(last[1] - pts[0][1], 2) if pts[0][1] is not None else None,
        })
    return sorted(out, key=lambda s: s["median_trip"])
