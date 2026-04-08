from __future__ import annotations

from config import AppConfig
from deviation_calculator import calculate_single_deviation


def test_calculate_single_deviation_uses_project_center_fallback_for_zero_coords():
    cfg = AppConfig()
    pole = {"name": "0A", "type": "DEFAULT", "height": 10.0, "x": 0.0, "y": 0.0}
    # Оба суффикса одинаковы -> верхняя группа пустая -> fallback path
    points = [
        {"name": "0A.1", "x": 0.2, "y": 0.2, "z": 1.0, "point_suffix": "1"},
        {"name": "0A.1b", "x": 0.4, "y": 0.1, "z": 1.2, "point_suffix": "1"},
    ]

    result = calculate_single_deviation(pole, points, cfg)

    assert result is not None
    assert result["pole_name"] == "0A"
    assert result["n_lower"] == 0
    assert result["n_upper"] == 2
    assert result["deviation_mm"] > 0


def test_calculate_single_deviation_regular_path():
    cfg = AppConfig()
    pole = {"name": "317", "type": "ТФГ-1500-10", "height": 10.0, "x": 100.0, "y": 200.0}
    points = [
        {"name": "317.1", "x": 100.0, "y": 200.0, "z": 1.0, "point_suffix": "1"},
        {"name": "317.2", "x": 100.05, "y": 200.02, "z": 10.0, "point_suffix": "2"},
        {"name": "317.3", "x": 100.06, "y": 200.03, "z": 10.1, "point_suffix": "3"},
    ]

    result = calculate_single_deviation(pole, points, cfg)

    assert result is not None
    assert result["pole_name"] == "317"
    assert result["n_lower"] == 1
    assert result["n_upper"] == 2
    assert result["tolerance_mm"] > 0


def test_calculate_single_deviation_marks_insufficient_when_z_span_too_small():
    cfg = AppConfig()
    pole = {"name": "574", "type": "DEFAULT", "height": 10.0, "x": 100.0, "y": 200.0}
    points = [
        {"name": "574.1", "x": 100.0, "y": 200.0, "z": 21.580, "point_suffix": "1"},
        {"name": "574.2", "x": 100.2, "y": 200.2, "z": 21.567, "point_suffix": "2"},
        {"name": "574.3", "x": 100.3, "y": 200.1, "z": 21.583, "point_suffix": "3"},
    ]
    result = calculate_single_deviation(pole, points, cfg)
    assert result is not None
    assert result["status"] == "Недостаточно данных"
    assert "разнесения" in result["status_detail"]


def test_calculate_single_deviation_filters_single_extreme_outlier():
    cfg = AppConfig()
    pole = {"name": "577", "type": "DEFAULT", "height": 10.0, "x": 100.0, "y": 200.0}
    points = [
        {"name": "577.1", "x": 180.0, "y": 240.0, "z": 1.0, "point_suffix": "1"},
        {"name": "577.2", "x": 100.0, "y": 200.0, "z": 1.2, "point_suffix": "2"},
        {"name": "577.3", "x": 100.1, "y": 200.1, "z": 11.0, "point_suffix": "3"},
        {"name": "577.4", "x": 100.2, "y": 200.2, "z": 11.2, "point_suffix": "4"},
    ]
    result = calculate_single_deviation(pole, points, cfg)
    assert result is not None
    assert result["status"] in ("Норма", "Предупреждение", "Превышение")
    assert "Исключён выброс" in result["quality_note"]


def test_calculate_single_deviation_returns_insufficient_for_single_point():
    cfg = AppConfig()
    pole = {"name": "999", "type": "DEFAULT", "height": 10.0, "x": 100.0, "y": 200.0}
    points = [{"name": "999.1", "x": 100.0, "y": 200.0, "z": 10.0, "point_suffix": "1"}]
    result = calculate_single_deviation(pole, points, cfg)
    assert result is not None
    assert result["status"] == "Недостаточно данных"
    assert "минимум 2" in result["status_detail"]

