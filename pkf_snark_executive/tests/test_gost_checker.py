from __future__ import annotations

from config import GOSTTolerances
from utils.gost_checker import DeviationStatus, check_tolerance


def test_check_tolerance_ok_warning_exceeded_thresholds():
    gost = GOSTTolerances(
        relative_divisor=150,
        min_tolerance_mm=10.0,
        max_tolerance_mm=120.0,
        absolute_tolerances={"DEFAULT": 60.0},
    )
    # tolerance = max(10000/150, 60) = 66.666...
    pole_type = "DEFAULT"
    pole_height_m = 10.0

    ok = check_tolerance(50.0, pole_type, pole_height_m, gost)
    warn = check_tolerance(60.0, pole_type, pole_height_m, gost)
    exceeded = check_tolerance(80.0, pole_type, pole_height_m, gost)

    assert ok.status == DeviationStatus.OK
    assert warn.status == DeviationStatus.WARNING
    assert exceeded.status == DeviationStatus.EXCEEDED
    assert ok.tolerance_mm > 0

