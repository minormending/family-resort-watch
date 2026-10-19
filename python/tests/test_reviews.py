import json

from family_resort_watch import reviews

CRITERIA = ("kids_club", "water_play", "beach", "rooms", "food", "transfer", "condition")


def review(code="sucdp", **scores):
    return {
        "code": code,
        "name": "Sunscape Coco Punta Cana",
        "researched_on": "2026-10-03",
        "criteria": {k: {"score": scores.get(k, 3), "evidence": "Seen in three reports."} for k in CRITERIA},
        "kids_club": {"name": "Explorer's Club", "ages": "3-12", "hours": "9am-5pm", "cost": "included", "notes": ""},
        "transfer_minutes": {"PUJ": 25},
        "sargassum_late_april": "moderate",
        "pros": ["Short transfer", "Cheap"],
        "cons": ["Kids' club closes at 5pm", "Seaweed in spring"],
        "verdict": "Good value if the beach cooperates.",
        "confidence": "medium",
        "sources": [{"title": f"s{i}", "url": f"https://example.com/{i}"} for i in range(3)],
    }


def test_weights_sum_to_one_and_cover_every_criterion():
    assert set(reviews.WEIGHTS) == set(CRITERIA)
    assert abs(sum(reviews.WEIGHTS.values()) - 1) < 1e-9


def test_family_fit_maps_all_threes_to_six_and_all_fives_to_ten():
    assert reviews.family_fit(review()["criteria"]) == 6.0
    assert reviews.family_fit(review(**{k: 5 for k in CRITERIA})["criteria"]) == 10.0
    # The kids' club carries the most weight: 5 there moves the score most.
    assert reviews.family_fit(review(kids_club=5)["criteria"]) > reviews.family_fit(review(rooms=5)["criteria"])


def test_validate_flags_missing_and_out_of_range_fields():
    doc = review()
    doc["criteria"]["beach"]["score"] = 6
    doc["sources"] = doc["sources"][:1]
    del doc["verdict"]
    errors = reviews.validate_resort(doc, "sucdp")
    assert any("beach" in e for e in errors)
    assert any("sources" in e for e in errors)
    assert any("verdict" in e for e in errors)
    assert reviews.validate_resort(review(), "sucdp") == []


def test_load_resorts_scores_valid_files_and_reports_bad_ones(tmp_path):
    (tmp_path / "sucdp.json").write_text(json.dumps(review(kids_club=5)))
    (tmp_path / "drfpc.json").write_text(json.dumps(review(code="wrong")))
    (tmp_path / "broken.json").write_text("{not json")
    found, problems = reviews.load_resorts(tmp_path)
    assert list(found) == ["sucdp"] and found["sucdp"]["fit"] == 6.9
    assert set(problems) == {"drfpc", "broken"}


def test_thinness_flags_low_confidence_unverified_claims_and_excerpt_only_sources():
    solid = review()
    assert reviews.thinness(solid) == []
    thin = review()
    thin["confidence"] = "low"
    thin["criteria"]["rooms"]["evidence"] = "Connecting rooms unverified; sofa bed unverified; cribs unverified."
    thin["sources"] = [{"title": "TripAdvisor reviews (via search excerpts)", "url": "https://t.example"}] * 3
    why = reviews.thinness(thin)
    assert any("low confidence" in w for w in why)
    assert any("3 unverified" in w for w in why)
    assert any("only 0 directly read" in w for w in why)
