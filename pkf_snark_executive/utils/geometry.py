"""
Геометрические утилиты для расчётов координат и отклонений.

Работа с точками в 2D/3D, вычисление расстояний, углов, центров.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Point2D:
    """Точка в двумерном пространстве."""
    x: float
    y: float


@dataclass(frozen=True)
class Point3D:
    """Точка в трёхмерном пространстве."""
    x: float
    y: float
    z: float

    def to_2d(self) -> Point2D:
        return Point2D(self.x, self.y)


def distance_2d(p1: Point2D | Point3D, p2: Point2D | Point3D) -> float:
    """Расстояние между двумя точками в плане (XY)."""
    return math.hypot(p1.x - p2.x, p1.y - p2.y)


def distance_3d(p1: Point3D, p2: Point3D) -> float:
    """Расстояние в 3D."""
    return math.sqrt(
        (p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2 + (p1.z - p2.z) ** 2
    )


def center_of_points_2d(points: list[Point2D | Point3D]) -> Point2D:
    """Центр группы точек в плане."""
    if not points:
        raise ValueError("Список точек пуст")
    n = len(points)
    cx = sum(p.x for p in points) / n
    cy = sum(p.y for p in points) / n
    return Point2D(cx, cy)


def center_of_points_3d(points: list[Point3D]) -> Point3D:
    """Центр группы точек в 3D."""
    if not points:
        raise ValueError("Список точек пуст")
    n = len(points)
    cx = sum(p.x for p in points) / n
    cy = sum(p.y for p in points) / n
    cz = sum(p.z for p in points) / n
    return Point3D(cx, cy, cz)


def angle_from_north(dx: float, dy: float) -> float:
    """
    Азимут направления (от оси Y по часовой стрелке) в градусах.
    dx, dy — смещения (восток +, север +).
    """
    angle_rad = math.atan2(dx, dy)
    angle_deg = math.degrees(angle_rad)
    if angle_deg < 0:
        angle_deg += 360.0
    return angle_deg


def angle_math(dx: float, dy: float) -> float:
    """Математический угол (от оси X против часовой стрелки) в градусах."""
    return math.degrees(math.atan2(dy, dx))


def points_in_radius(
    target: Point2D | Point3D,
    points: list[Point2D | Point3D],
    radius: float,
) -> list[int]:
    """Индексы точек из списка, находящихся в радиусе от target."""
    return [i for i, p in enumerate(points) if distance_2d(target, p) <= radius]


def deviation_vector(
    center_low: Point2D, center_high: Point2D
) -> tuple[float, float, float, float]:
    """
    Вектор отклонения от нижнего сечения к верхнему.

    Возвращает (dx_mm, dy_mm, total_mm, angle_deg).
    """
    dx_m = center_high.x - center_low.x
    dy_m = center_high.y - center_low.y
    dx_mm = dx_m * 1000.0
    dy_mm = dy_m * 1000.0
    total_mm = math.hypot(dx_mm, dy_mm)
    angle_deg = angle_from_north(dx_mm, dy_mm)
    return dx_mm, dy_mm, total_mm, angle_deg
