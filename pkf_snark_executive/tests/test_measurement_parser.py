from __future__ import annotations

import tempfile
from pathlib import Path

from measurement_parser import (
    classify_pole_points,
    match_points_to_poles,
    parse_txt_measurements,
    trim_pole_points_for_verticality,
)


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


def test_parse_txt_yx_order_matches_project_axes():
    """Y,X,Z в файле: вторая колонка — Y (~74k), третья — X (~119k)."""
    content = "574.3,74156.863,119387.010,21.578\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(content)
        path = tmp.name
    try:
        pts = parse_txt_measurements(path, coord_order="yx")
        assert len(pts) == 1
        assert abs(pts[0]["x"] - 119387.010) < 1e-6
        assert abs(pts[0]["y"] - 74156.863) < 1e-6
        assert pts[0]["name"] == "574.3"
    finally:
        Path(path).unlink(missing_ok=True)


def test_classify_pole_points_six_points_by_height():
    """Шесть точек: три нижних и три верхних по Z."""
    points = [
        {"name": "p1", "point_suffix": "1", "z": 10.0, "x": 0.0, "y": 0.0},
        {"name": "p2", "point_suffix": "2", "z": 11.0, "x": 0.0, "y": 0.0},
        {"name": "p3", "point_suffix": "3", "z": 12.0, "x": 0.0, "y": 0.0},
        {"name": "p4", "point_suffix": "4", "z": 20.0, "x": 0.0, "y": 0.0},
        {"name": "p5", "point_suffix": "5", "z": 21.0, "x": 0.0, "y": 0.0},
        {"name": "p6", "point_suffix": "6", "z": 22.0, "x": 0.0, "y": 0.0},
    ]
    lower, upper = classify_pole_points(points)
    assert [p["name"] for p in lower] == ["p1", "p2", "p3"]
    assert [p["name"] for p in upper] == ["p4", "p5", "p6"]


def test_trim_pole_points_keeps_three_low_three_high_z():
    zs = [13.0, 13.1, 13.2, 17.0, 18.0, 19.0, 21.0, 21.1, 21.2]
    pts = [{"name": f"t{i}", "z": z, "x": 0.0, "y": 0.0} for i, z in enumerate(zs)]
    trimmed, note = trim_pole_points_for_verticality(pts, 3, 3)
    assert len(trimmed) == 6
    assert note is not None
    tz = sorted([p["z"] for p in trimmed])
    assert tz[:3] == [13.0, 13.1, 13.2]
    assert tz[3:] == [21.0, 21.1, 21.2]


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

