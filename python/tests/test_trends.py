from datetime import datetime, timedelta, timezone

from family_resort_watch import trends
from family_resort_watch.config import Window

W = Window("2027-04-24", "2027-04-29", "primary")
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


RESORTS = [
    {"code": "aaa", "kids_club": "Explorer's Club", "airports": ["PUJ"]},
    {"code": "bbb", "kids_club": "Bahia Scouts", "airports": ["PUJ", "SDQ"]},
    {"code": "ccc", "kids_club": "Explorer's Club", "airports": ["CZM"]},   # no fares: hotel only
    {"code": "zzz", "kids_club": "", "airports": ["PUJ"]},                    # no kids club: ignored
]


def fetch(scan_id, best):
    return {"scan_id": scan_id, "best": best, "fetched_at": "x"}


def test_trip_numbers_use_the_fare_in_force_at_each_scan():
    scans = {1: ts(10), 2: ts(1)}
    series = {
        ("aaa", W.checkin, W.checkout): [fetch(1, 1000), fetch(2, 1100)],
        ("bbb", W.checkin, W.checkout): [fetch(1, 1500), fetch(2, 1400)],
        ("ccc", W.checkin, W.checkout): [fetch(1, 900), fetch(2, 900)],
        ("zzz", W.checkin, W.checkout): [fetch(1, 10), fetch(2, 10)],
    }
    fseries = {
        ("PUJ", W.checkin, W.checkout): [{"fetched_at": ts(11), "family": 2000}, {"fetched_at": ts(2), "family": 1800}],
        # SDQ appears days after the first sweep: a route added, not a price move.
        ("SDQ", W.checkin, W.checkout): [{"fetched_at": ts(5), "family": 1200}],
    }
    doc = trends.stay_trends(series, fseries, RESORTS, (W,), scans, NOW)
    first, last = doc["stays"][W.key]
    # Scan 1: PUJ fare 2000 only. Trips aaa 3000, bbb 3500; ccc has no fare; zzz isn't a kids-club resort.
    assert first[1:] == [3250, 3000, 1000, 2000, "aaa"]
    # Scan 2: PUJ 1800, SDQ 1200 -> aaa 2900, bbb 1400+1200=2600. Median hotel over aaa, bbb, ccc.
    assert last[1:] == [2750, 2600, 1100, 1200, "bbb"]
    assert doc["annotations"] == [{"t": int(datetime.fromisoformat(ts(5)).timestamp()), "label": "SDQ flights added"}]


def test_old_points_thin_to_one_per_day_and_summary_reports_changes():
    t0 = NOW - timedelta(days=9)
    pts = []
    for h in range(0, 9 * 24, 6):  # every 6 hours for 9 days
        t = t0 + timedelta(hours=h)
        pts.append([int(t.timestamp()), 3000 - h, 2500, 1800, 1200, "aaa"])
    thinned = trends._thin(pts, NOW)
    old = [p for p in thinned if p[0] < (NOW - trends.KEEP_EVERY_POINT).timestamp()]
    days = {datetime.fromtimestamp(p[0], timezone.utc).date() for p in old}
    assert len(old) == len(days)                       # one per day before the cutoff
    assert len(thinned) - len(old) == sum(1 for p in pts if p[0] >= (NOW - trends.KEEP_EVERY_POINT).timestamp())

    doc = {"stays": {W.key: thinned}}
    (s,) = trends.summary(doc, (W,), NOW)
    assert s["median_trip"] == thinned[-1][1]
    assert s["median_trip_24h"] < 0 and s["median_trip_7d"] < s["median_trip_24h"]   # steadily falling
    assert s["since"] == t0.date().isoformat()
