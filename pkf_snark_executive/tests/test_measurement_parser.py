from __future__ import annotations

from measurement_parser import classify_pole_points, match_points_to_poles


def test_match_points_to_poles_by_name():
    poles = [{"name": "317", "x": 10.0, "y": 20.0}]
    points = [{"name": "317.1", "x": 10.3, "y": 20.1, "z": 5.0, "pole_id": "317", "is_station": False}]

    matched = match_points_to_poles(points, poles, threshold_m=2.0)

    assert len(matched["317"]) == 1
    assert matched["317"][0]["name"] == "317.1"


def test_match_points_to_poles_by_distance_with_zero_coordinates():
    poles = [{"name": "0A", "x": 0.0, "y": 0.0}]
    points = [{"name": "unknown", "x": 0.5, "y": 0.4, "z": 1.0, "pole_id": "", "is_station": False}]

    matched = match_points_to_poles(points, poles, threshold_m=2.0)

    assert len(matched["0A"]) == 1
    assert matched["0A"][0]["pole_id"] == "0A"


def test_classify_pole_points_suffix_strategy():
    points = [
        {"name": "101.1", "point_suffix": "1", "z": 1.0},
        {"name": "101.2", "point_suffix": "2", "z": 2.0},
        {"name": "101.3", "point_suffix": "3", "z": 3.0},
    ]
    lower, upper = classify_pole_points(points)

    assert [p["name"] for p in lower] == ["101.1"]
    assert [p["name"] for p in upper] == ["101.2", "101.3"]


def test_classify_pole_points_z_strategy_without_suffixes():
    points = [
        {"name": "p1", "point_suffix": "", "z": 10.0},
        {"name": "p2", "point_suffix": "", "z": 11.0},
        {"name": "p3", "point_suffix": "", "z": 20.0},
        {"name": "p4", "point_suffix": "", "z": 21.0},
    ]
    lower, upper = classify_pole_points(points)

    assert len(lower) == 2
    assert len(upper) == 2
    assert max(p["z"] for p in lower) <= min(p["z"] for p in upper)

