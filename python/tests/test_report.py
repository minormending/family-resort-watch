from datetime import datetime, timezone

from family_resort_watch import report, storage
from family_resort_watch.bookcore import Availability, Hotel, Offer
from family_resort_watch.config import Config, Party, Window

PRIMARY = Window("2027-04-24", "2027-04-29", "primary")
CFG = Config(
    party=Party(adults=2, child_ages=(5,)),
    windows=(PRIMARY,),
    countries=frozenset({"Dominican Republic"}),
    reclassify_days=7,
    workers=1,
    timeout_seconds=5,
    retries=0,
    airports={"Punta Cana": ("PUJ",)},
)


def offer(total: float, refundable: bool = False, room: str = "King Room", inventory: int = 20) -> Offer:
    return Offer(
        room_code=room.lower().replace(" ", ""), room_name=room, rate_code="r" + str(int(refundable)),
        rate_name="Flexible" if refundable else "Non refundable", board="TI", refundable=refundable,
        free_cancel_until="2027-04-21" if refundable else None, public_total=total * 1.1,
        member_total=total, rack_total=total * 1.2, inventory=inventory, two_beds="Double" in room,
    )


def run_scan(conn, by_code: dict[str, tuple[Offer, ...]]) -> int:
    scan_id = storage.start_scan(conn, "2-1-0")
    for code, offers in by_code.items():
        result = Availability(code, code, "ok" if offers else "no_rooms", "", len(offers), offers)
        storage.record(conn, scan_id, code, PRIMARY.checkin, PRIMARY.checkout, result)
    storage.finish_scan(conn, scan_id)
    return scan_id


def test_snapshot_ranks_tracks_drops_and_compresses_history(tmp_path):
    with storage.connect(tmp_path / "t.db") as conn:
        storage.upsert_hotels(conn, [
            Hotel("aaa", "Dreams A", "Dominican Republic", "Punta Cana"),
            Hotel("bbb", "Sunscape B", "Dominican Republic", "Punta Cana"),
            Hotel("ccc", "Secrets C", "Dominican Republic", "Punta Cana"),
        ])
        storage.set_classification(conn, "aaa", True, "")
        storage.set_classification(conn, "bbb", True, "")
        storage.set_classification(conn, "ccc", False, "adults only")

        for _ in range(2):
            run_scan(conn, {"aaa": (offer(2000), offer(2100, refundable=True)),
                            "bbb": (offer(1500, room="Double Room"),)})
        run_scan(conn, {"aaa": (offer(1800, inventory=2), offer(2100, refundable=True)), "bbb": ()})

        latest, history = report.snapshot(conn, CFG, now=datetime(2026, 10, 3, tzinfo=timezone.utc))

    names = [r["code"] for r in latest["resorts"]]
    assert names == ["aaa", "bbb"]  # bbb sold out, so it sinks below aaa
    assert latest["adults_only"] == [{"code": "ccc", "name": "Secrets C", "area": "Punta Cana"}]

    a = latest["resorts"][0]
    stay = a["stays"][PRIMARY.key]
    assert a["best_primary"]["member_total"] == 1800
    assert stay["delta"] == -200 and stay["previous"] == 2000
    assert stay["cheapest_refundable"]["member_total"] == 2100
    assert a["kids_club"] == "Explorer's Club"

    kinds = {(e["code"], e["type"]) for e in latest["events"]}
    assert ("aaa", "drop") in kinds and ("aaa", "new_low") in kinds and ("aaa", "scarce") in kinds
    assert ("bbb", "sold_out") in kinds

    # A fourth scan with the same scarce room is not news again.
    with storage.connect(tmp_path / "t.db") as conn:
        run_scan(conn, {"aaa": (offer(1800, inventory=2), offer(2100, refundable=True)), "bbb": ()})
        again, _ = report.snapshot(conn, CFG, now=datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert not any(e["type"] == "scarce" for e in again["events"])

    # Three aaa fetches, two identical: the history keeps only change points.
    pts = history["hist-2027-04-24_2027-04-29"]["points"]["aaa"]
    assert [p[1] for p in pts] == [2000, 1800]


def test_points_estimate_adds_half_for_a_child_over_three():
    est = report._points("B", {"chart": {"B": [15000, 20000, 25000, 30000, 35000]}, "extra_guest_factor": 0.5},
                         Party(2, (5,)), 5, 990.0)
    assert est["stay_points"] == [112500, 262500]
    assert est["cpp"] == [0.38, 0.88]


def test_prune_keeps_every_value_the_history_draws(tmp_path):
    with storage.connect(tmp_path / "t.db") as conn:
        storage.upsert_hotels(conn, [Hotel("aaa", "Dreams A", "Dominican Republic", "Punta Cana")])
        storage.set_classification(conn, "aaa", True, "")
        offers = (offer(1000, room="King A"), offer(1100, room="King B"),
                  offer(1200, refundable=True, room="King C"), offer(1300, refundable=True, room="King D"),
                  offer(1400, room="Double A"), offer(1500, room="Double B"))
        run_scan(conn, {"aaa": offers})
        before = report._series(conn)
        conn.execute("UPDATE scans SET started_at = '2020-01-01T00:00:00+00:00'")
        removed = storage.prune(conn, keep_days=14)
        after = report._series(conn)
        left = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    assert removed == 3 and left == 3
    assert before == after


def test_a_new_trip_loads_and_renders_its_page(tmp_path):
    from family_resort_watch import config, page, trips
    from tests.test_trips import future_answers

    trips.create(tmp_path, "spring", future_answers())
    config.select(tmp_path, "spring")
    cfg = config.load()
    assert cfg.primary and {"title", "recess_label", "stay_label", "recess", "strip"} <= set(cfg.trip)
    html = page.build(cfg.trip).read_text()
    assert config.PAGE_PATH == tmp_path / "page" / "spring.html"
    assert f"<title>{cfg.trip['title']}</title>" in html and "{{" not in html
