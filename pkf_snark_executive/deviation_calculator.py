"""
Расчёт вертикальности опор по ГОСТ Р 51872-2024.

Алгоритм:
1. Для каждой опоры: группируем привязанные точки на нижние и верхние
2. Вычисляем центры нижнего и верхнего сечений
3. Определяем вектор отклонения (ΔX, ΔY)
4. Полное отклонение = √(ΔX² + ΔY²)
5. Угол отклонения (азимут от оси Y)
6. Проверка допуска по ГОСТ

Формулы:
- ΔX = X_верх - X_низ (в мм)
- ΔY = Y_верх - Y_низ (в мм)
- Отклонение = √(ΔX² + ΔY²)
- Допуск = max(H/150, абсолютный_порог) (мм)
"""
from __future__ import annotations

import logging
import math
from typing import Any

from config import AppConfig
from measurement_parser import classify_pole_points
from utils.geometry import (
    Point2D,
    Point3D,
    center_of_points_2d,
    center_of_points_3d,
    deviation_vector,
)
from utils.gost_checker import DeviationStatus, check_tolerance

logger = logging.getLogger(__name__)


def calculate_single_deviation(
    pole: dict[str, Any],
    points: list[dict[str, Any]],
    cfg: AppConfig,
) -> dict[str, Any] | None:
    """
    Расчёт отклонения вертикальности для одной опоры.

    Args:
        pole: данные опоры {name, type, height, x, y, z}
        points: привязанные к опоре точки замеров
        cfg: конфигурация приложения

    Returns:
        Словарь с результатами или None, если данных недостаточно.
    """
    pole_name = pole.get("name", "?")
    pole_type = pole.get("type", "DEFAULT")
    pole_height = pole.get("height", 0.0) or 10.0  # fallback

    if len(points) < 2:
        logger.warning("Опора %s: менее 2 точек (%d), пропуск", pole_name, len(points))
        return None

    # Разделяем на нижние и верхние
    lower, upper = classify_pole_points(points)

    if not lower or not upper:
        logger.warning(
            "Опора %s: нет нижних (%d) или верхних (%d) точек",
            pole_name, len(lower), len(upper),
        )
        # Если есть хотя бы 2 точки — используем проектный центр как нижний
        if len(points) >= 2 and pole.get("x") is not None and pole.get("y") is not None:
            return _calculate_from_project_center(pole, points, cfg)
        return None

    # Центры сечений
    lower_pts_3d = [Point3D(p["x"], p["y"], p["z"]) for p in lower]
    upper_pts_3d = [Point3D(p["x"], p["y"], p["z"]) for p in upper]

    center_low = center_of_points_2d(lower_pts_3d)
    center_high = center_of_points_2d(upper_pts_3d)

    center_low_3d = center_of_points_3d(lower_pts_3d)
    center_high_3d = center_of_points_3d(upper_pts_3d)

    # Вектор отклонения
    dx_mm, dy_mm, total_mm, angle_deg = deviation_vector(center_low, center_high)

    # Фактическая высота (разница Z между центрами сечений)
    height_diff = abs(center_high_3d.z - center_low_3d.z)
    height_fact = height_diff if height_diff > 0.1 else pole_height

    # Проверка допуска
    tolerance_result = check_tolerance(total_mm, pole_type, pole_height, cfg.gost)

    return {
        "pole_name": pole_name,
        "pole_type": pole_type,
        "height_project": pole_height,
        "height_fact": round(height_fact, 3),
        "x_project": pole.get("x", 0.0),
        "y_project": pole.get("y", 0.0),
        "x_fact_low": round(center_low.x, 3),
        "y_fact_low": round(center_low.y, 3),
        "x_fact_high": round(center_high.x, 3),
        "y_fact_high": round(center_high.y, 3),
        "dx_mm": round(dx_mm, 1),
        "dy_mm": round(dy_mm, 1),
        "deviation_mm": round(total_mm, 1),
        "angle_deg": round(angle_deg, 1),
        "tolerance_mm": round(tolerance_result.tolerance_mm, 1),
        "status": tolerance_result.status.value,
        "status_detail": tolerance_result.status_text,
        "n_lower": len(lower),
        "n_upper": len(upper),
    }


def _calculate_from_project_center(
    pole: dict[str, Any],
    points: list[dict[str, Any]],
    cfg: AppConfig,
) -> dict[str, Any] | None:
    """
    Расчёт отклонения при отсутствии разделения на нижние/верхние.

    Нижний центр = проектная координата опоры.
    Верхний центр = среднее всех фактических точек.
    """
    pole_name = pole.get("name", "?")
    pole_type = pole.get("type", "DEFAULT")
    pole_height = pole.get("height", 0.0) or 10.0

    center_low = Point2D(pole["x"], pole["y"])

    fact_pts = [Point2D(p["x"], p["y"]) for p in points]
    center_high = center_of_points_2d(fact_pts)

    dx_mm, dy_mm, total_mm, angle_deg = deviation_vector(center_low, center_high)

    # Средняя Z фактических точек
    avg_z = sum(p["z"] for p in points) / len(points) if points else 0

    tolerance_result = check_tolerance(total_mm, pole_type, pole_height, cfg.gost)

    return {
        "pole_name": pole_name,
        "pole_type": pole_type,
        "height_project": pole_height,
        "height_fact": round(avg_z, 3),
        "x_project": pole.get("x", 0.0),
        "y_project": pole.get("y", 0.0),
        "x_fact_low": round(center_low.x, 3),
        "y_fact_low": round(center_low.y, 3),
        "x_fact_high": round(center_high.x, 3),
        "y_fact_high": round(center_high.y, 3),
        "dx_mm": round(dx_mm, 1),
        "dy_mm": round(dy_mm, 1),
        "deviation_mm": round(total_mm, 1),
        "angle_deg": round(angle_deg, 1),
        "tolerance_mm": round(tolerance_result.tolerance_mm, 1),
        "status": tolerance_result.status.value,
        "status_detail": tolerance_result.status_text,
        "n_lower": 0,
        "n_upper": len(points),
    }


def calculate_all_deviations(
    matched: dict[str, list[dict[str, Any]]],
    poles: list[dict[str, Any]],
    cfg: AppConfig,
) -> list[dict[str, Any]]:
    """
    Расчёт отклонений для всех опор.

    Args:
        matched: {pole_name: [точки]} из match_points_to_poles
        poles: проектные данные опор
        cfg: конфигурация

    Returns:
        Список результатов отклонений.
    """
    pole_index = {p["name"]: p for p in poles if p.get("name")}
    results: list[dict[str, Any]] = []

    for pole_name, points in matched.items():
        if not points:
            continue

        pole = pole_index.get(pole_name)
        if pole is None:
            logger.warning("Опора %s: нет проектных данных", pole_name)
            continue

        result = calculate_single_deviation(pole, points, cfg)
        if result is not None:
            results.append(result)

    # Сортировка по имени опоры
    results.sort(key=lambda r: _sort_key_numeric(r["pole_name"]))

    logger.info("Расчёт: %d опор с отклонениями из %d привязанных", len(results), len(matched))
    return results


def _sort_key_numeric(name: str) -> tuple[int, str]:
    """Числовая сортировка имён опор."""
    import re
    m = re.match(r'(\d+)(.*)', name)
    if m:
        return (int(m.group(1)), m.group(2))
    return (999999, name)
