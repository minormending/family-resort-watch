from family_resort_watch import geo


def sample():
    return {
        "airports": {"PUJ": {"lat": 18.567, "lon": -68.363, "name": "Punta Cana Intl"},
                     "SDQ": {"lat": 18.43, "lon": -69.669, "name": "Las Américas"}},
        "resorts": {"sucdp": {"lat": 18.7, "lon": -68.45, "source": "OpenStreetMap: \"Sunscape\" Coco"}},
        "drive": {},
    }


def test_dump_round_trips_through_load(tmp_path):
    g = sample()
    g["drive"]["sucdp"] = {"PUJ": {"min": 25, "km": 21.4}}
    path = tmp_path / "geo.toml"
    path.write_text(geo.dump(g))
    assert geo.load(path) == g
    assert geo.drive_minutes(g, "sucdp") == {"PUJ": 25} and geo.drive_minutes(g, "nope") == {}
    assert geo.load(tmp_path / "missing.toml") == {"airports": {}, "resorts": {}, "drive": {}}


def test_fill_routes_only_what_is_missing_and_names_what_has_no_location(monkeypatch):
    calls = []

    def fake_route(a, b, timeout=30):
        calls.append((a["name"], b["source"]))
        return 47, 52.3

    monkeypatch.setattr(geo, "route", fake_route)
    g = sample()
    g["drive"]["sucdp"] = {"PUJ": {"min": 25, "km": 21.4}}
    added, missing, failed = geo.fill(g, {"sucdp": ("PUJ", "SDQ", "LRM"), "dredl": ("PUJ",)}, pause=0)
    assert added == ["sucdp SDQ 47 min"] and len(calls) == 1
    assert missing == ["LRM", "dredl"] and failed == []
    assert g["drive"]["sucdp"] == {"PUJ": {"min": 25, "km": 21.4}, "SDQ": {"min": 47, "km": 52.3}}


def test_fill_keeps_going_past_a_pair_with_no_road(monkeypatch):
    def no_road(a, b, timeout=30):
        raise geo.GeoError("router: NoRoute")

    monkeypatch.setattr(geo, "route", no_road)
    added, missing, failed = geo.fill(sample(), {"sucdp": ("PUJ", "SDQ")}, pause=0)
    assert added == [] and missing == [] and failed == ["sucdp PUJ: router: NoRoute", "sucdp SDQ: router: NoRoute"]


def test_a_road_point_is_routed_instead_and_slow_drives_are_flagged(monkeypatch):
    seen = []
    monkeypatch.setattr(geo, "route", lambda a, b, timeout=30: seen.append(b) or (20, 14.1))
    g = sample()
    g["resorts"]["sucdp"]["road"] = {"lat": 18.6487, "lon": -68.3623}
    geo.fill(g, {"sucdp": ("PUJ",)}, pause=0)
    assert seen == [{"lat": 18.6487, "lon": -68.3623}]
    assert "road = { lat = 18.6487, lon = -68.3623 }" in geo.dump(g)
    g["drive"]["slowpoke"] = {"PUJ": {"min": 48, "km": 20.2}}
    assert geo.slow(g) == ["slowpoke PUJ: 20.2 km in 48 min"]


def test_the_users_geo_adds_to_the_shipped_one(tmp_path):
    from family_resort_watch import config

    config.select(tmp_path)
    shipped = geo.load()
    assert len(shipped["resorts"]) >= 40 and "PUJ" in shipped["airports"]
    assert all(shipped["drive"].get(code) for code in ("sucdp", "dredl", "drfpc"))
    own = {"airports": {}, "resorts": {"newone": {"lat": 18.6, "lon": -68.4, "source": "mine"}},
           "drive": {"newone": {"PUJ": {"min": 30, "km": 25.0}}, "sucdp": {"SDQ": {"min": 140, "km": 190.0}}}}
    geo.own_path().write_text(geo.dump(own))
    both = geo.load()
    assert both["resorts"]["newone"]["source"] == "mine"
    assert geo.drive_minutes(both, "sucdp") == {**geo.drive_minutes(shipped, "sucdp"), "SDQ": 140}
