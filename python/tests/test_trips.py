import json
from datetime import date, timedelta

import pytest

from family_resort_watch import config, main, trips


def future_answers(**changes) -> dict:
    """The shipped example, moved a year ahead so it never goes stale."""
    start = date.today() + timedelta(days=365)
    day = lambda n: (start + timedelta(days=n)).isoformat()  # noqa: E731
    a = json.loads(json.dumps(trips.EXAMPLE))
    a["recess"] = [day(0), day(8)]
    a["stays"] = {"preferred": [[day(2), day(7)]], "also": [[day(0), day(5)], [day(4), day(9)]]}
    a.update(changes)
    return a


def test_render_makes_a_config_that_loads(tmp_path):
    path = trips.create(tmp_path, "spring", future_answers())
    config.select(tmp_path, "spring")
    cfg = config.load()
    assert path == tmp_path / "trips" / "spring.toml"
    assert cfg.party.adults == 2 and cfg.party.child_ages == (7, 4)
    assert len(cfg.primary) == 1 and len(cfg.windows) == 3
    assert all(w.every_hours == 6 for w in cfg.windows if w.tier == "flex")
    f = cfg.flights
    assert f.origins == ("CHI",) and f.departure_airports == ("ORD", "MDW") and f.home_label == "Chicago"
    assert f.max_stops == 0 and f.proxy_adults == 4
    # The shipped area -> airport map comes along.
    assert "PUJ" in cfg.airports["Punta Cana"]


def test_babies_are_not_priced_a_seat_and_connections_can_be_allowed(tmp_path):
    a = future_answers(party={"adults": 2, "child_ages": [1]}, nonstop_only=False)
    a["home"] = {"airports": ["BOS"]}
    trips.create(tmp_path, "t", a)
    config.select(tmp_path, "t")
    f = config.load().flights
    assert f.proxy_adults == 2 and f.max_stops == 1
    assert f.origins == ("BOS",) and f.home_label == "BOS"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"party": {"adults": 0}}, "adults"),
        ({"party": {"adults": 2, "child_ages": [3, 4, 5, 6]}}, "at most 5 guests"),
        ({"home": {"airports": ["chicago"]}}, "3-letter"),
        ({"home": {"airports": []}}, "at least one airport"),
        ({"stays": {"preferred": [["2020-01-01", "2020-01-06"]]}}, "in the past"),
        ({"stays": {"also": [["2099-01-01", "2099-01-06"]]}}, "preferred needs"),
    ],
)
def test_bad_answers_say_what_is_wrong(tmp_path, change, message):
    with pytest.raises(trips.AnswerError, match=message):
        trips.create(tmp_path, "t", future_answers(**change))


def test_create_refuses_to_overwrite_unless_asked(tmp_path):
    trips.create(tmp_path, "t", future_answers())
    with pytest.raises(trips.AnswerError, match="already exists"):
        trips.create(tmp_path, "t", future_answers())
    trips.create(tmp_path, "t", future_answers(title="Changed"), overwrite=True)
    assert 'title = "Changed"' in (tmp_path / "trips" / "t.toml").read_text()


def test_set_artifact_adds_then_replaces(tmp_path):
    path = trips.create(tmp_path, "t", future_answers())
    trips.set_artifact(path, "https://claude.ai/artifact/abc123")
    trips.set_artifact(path, "https://claude.ai/artifact/def456")
    text = path.read_text()
    assert text.count("artifact = ") == 1 and "def456" in text
    config.select(tmp_path, "t")
    assert config.load().trip["artifact"] == "https://claude.ai/artifact/def456"
    with pytest.raises(trips.AnswerError):
        trips.set_artifact(path, "https://example.com/x")


def test_cli_new_trip_then_context(tmp_path, capsys):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(future_answers()))
    home = str(tmp_path / "home")
    assert main.main(["--home", home, "new-trip", "spring", "--answers", str(answers)]) == 0
    assert main.main(["--home", home, "set-artifact", "https://claude.ai/artifact/xyz"]) == 0
    capsys.readouterr()
    assert main.main(["--home", home, "context"]) == 0
    ctx = json.loads(capsys.readouterr().out)
    assert ctx["trip"] == "spring" and ctx["artifact"] == "https://claude.ai/artifact/xyz"
    assert ctx["party"] == "2 adults + child (7), child (4)"
    assert ctx["home"] == "Chicago (ORD, MDW)" and ctx["flights"] == "nonstop only"
    assert ctx["page"].endswith("page/spring.html")


def test_cli_asks_which_trip_when_there_are_several(tmp_path, capsys):
    for name in ("a", "b"):
        trips.create(tmp_path, name, future_answers())
    assert main.main(["--home", str(tmp_path), "context"]) == 2
    assert "pass --trip" in capsys.readouterr().err
    assert main.main(["--home", str(tmp_path), "--trip", "b", "context"]) == 0


def test_home_research_overrides_shipped_research(tmp_path):
    from family_resort_watch import reviews

    config.select(tmp_path)
    shipped, problems = reviews.load_resorts()
    assert shipped and not problems
    code, doc = next(iter(sorted(shipped.items())))
    own = {k: v for k, v in doc.items() if k != "fit"}
    own["verdict"] = "Our own take."
    folder = tmp_path / "research" / "resorts"
    folder.mkdir(parents=True)
    (folder / f"{code}.json").write_text(json.dumps(own))
    mine, _ = reviews.load_resorts()
    assert mine[code]["verdict"] == "Our own take." and len(mine) == len(shipped)


def test_cli_resorts_lists_family_resorts_with_their_review(tmp_path, capsys):
    from family_resort_watch import reviews, storage

    trips.create(tmp_path, "t", future_answers())
    config.select(tmp_path, "t")
    shipped = next(iter(sorted(reviews.load_resorts()[0])))
    with storage.connect(config.DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO hotels (code, name, country, area, takes_party, listed_at) VALUES (?, ?, ?, ?, ?, '')",
            [(shipped, "Reviewed", "Mexico", "Cancún", 1), ("newone", "New", "Jamaica", "Montego Bay", 1),
             ("adults", "Adults Only", "Mexico", "Cancún", 0), ("far", "Far Away", "Spain", "Mallorca", 1)],
        )
    capsys.readouterr()
    assert main.main(["--home", str(tmp_path), "resorts"]) == 0
    rows = {r["code"]: r for r in json.loads(capsys.readouterr().out)}
    assert set(rows) == {shipped, "newone"}
    assert rows[shipped]["review"] == "shipped" and rows["newone"]["review"] is None
    assert rows["newone"]["airports"] == ["MBJ"]
