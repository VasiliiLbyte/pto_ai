"""
Парсер геодезических замеров (TXT, DXF, XML).

Поддерживаемые форматы:
- TXT: CSV без заголовка «ИмяТочки,X,Y,Z» (формат FL573A-579.593-4.txt)
- DXF: POINT + TEXT из файлов замеров
- XML: Leica/Trimble форматы

Привязка точек к опорам:
- По расстоянию в плане (порог 2 м по умолчанию)
- По имени точки (суффикс .1/.2/.3 → номер замера, префикс → номер опоры)
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import ezdxf

from utils.geometry import Point2D, Point3D, distance_2d

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Универсальный диспетчер
# ---------------------------------------------------------------------------
def parse_measurement_file(file_path: str) -> list[dict[str, Any]]:
    """
    Парсит файл замеров, определяя формат по расширению.

    Returns:
        Список точек: [{name, x, y, z, pole_id, point_suffix, is_station}, ...]
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".txt":
        return parse_txt_measurements(file_path)
    elif ext == ".dxf":
        return parse_dxf_measurements(file_path)
    elif ext == ".xml":
        return parse_xml_measurements(file_path)
    else:
        logger.warning("Неизвестный формат файла: %s", ext)
        return []


# ---------------------------------------------------------------------------
# TXT парсер (формат: ИмяТочки,X,Y,Z)
# ---------------------------------------------------------------------------
# Паттерн имени точки опоры: «573A.3» или «574.1» и т.д.
_POLE_POINT_RE = re.compile(r'^(\d{1,4}[A-Za-zА-Яа-я]?)\.(\d+)$')
# Паттерн точки стояния: «1 (34)» или «2(12)»
_STATION_RE = re.compile(r'^\d+\s*\(\d+\)$')


def parse_txt_measurements(file_path: str) -> list[dict[str, Any]]:
    """
    Парсит TXT-файл замеров (CSV без заголовка).

    Формат строки: ИмяТочки,X,Y,Z
    Пример: 573A.3,74204.183,119389.735,22.087
    """
    points: list[dict[str, Any]] = []

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split(",")
            if len(parts) < 4:
                # Пробуем разделитель «;» или табуляцию
                parts = re.split(r'[;\t]', line)
            if len(parts) < 4:
                logger.debug("Строка %d: недостаточно полей: %s", line_no, line)
                continue

            name = parts[0].strip()
            try:
                x = float(parts[1].strip().replace(",", "."))
                y = float(parts[2].strip().replace(",", "."))
                z = float(parts[3].strip().replace(",", "."))
            except ValueError:
                logger.debug("Строка %d: ошибка числового формата: %s", line_no, line)
                continue

            point = _classify_point(name, x, y, z)
            points.append(point)

    logger.info("TXT: прочитано %d точек из %s", len(points), file_path)
    return points


def _classify_point(name: str, x: float, y: float, z: float) -> dict[str, Any]:
    """Классифицирует точку: опорная / стояния."""
    result: dict[str, Any] = {
        "name": name,
        "x": x,
        "y": y,
        "z": z,
        "pole_id": "",
        "point_suffix": "",
        "is_station": False,
    }

    # Точка стояния
    if _STATION_RE.match(name):
        result["is_station"] = True
        return result

    # Точка опоры
    m = _POLE_POINT_RE.match(name)
    if m:
        result["pole_id"] = m.group(1)
        result["point_suffix"] = m.group(2)
        return result

    # Неопознанная — пробуем извлечь числовой ID
    num_match = re.match(r'^(\d{1,4}[A-Za-zА-Яа-я]?)', name)
    if num_match:
        result["pole_id"] = num_match.group(1)

    return result


# ---------------------------------------------------------------------------
# DXF парсер замеров
# ---------------------------------------------------------------------------
def parse_dxf_measurements(file_path: str) -> list[dict[str, Any]]:
    """Парсит DXF-файл замеров (POINT + TEXT рядом)."""
    points: list[dict[str, Any]] = []

    try:
        doc = ezdxf.readfile(file_path)
    except Exception as e:
        logger.error("Ошибка чтения DXF замеров: %s", e)
        return points

    msp = doc.modelspace()

    # Собираем все POINT
    dxf_points: list[Point3D] = []
    for entity in msp.query("POINT"):
        loc = entity.dxf.location
        dxf_points.append(Point3D(loc.x, loc.y, loc.z))

    # Собираем все TEXT
    texts: list[tuple[Point3D, str]] = []
    for entity in msp.query("TEXT"):
        pos = entity.dxf.insert
        text = entity.dxf.text.strip()
        if text:
            texts.append((Point3D(pos.x, pos.y, pos.z), text))

    # Сопоставляем POINT + TEXT (ближайший текст < 3 м)
    for pt in dxf_points:
        best_dist = float("inf")
        best_name = ""
        for tpt, tname in texts:
            d = distance_2d(pt, tpt)
            if d < best_dist and d < 3.0:
                best_dist = d
                best_name = tname

        name = best_name or f"P_{pt.x:.1f}_{pt.y:.1f}"
        point = _classify_point(name, pt.x, pt.y, pt.z)
        points.append(point)

    logger.info("DXF замеры: прочитано %d точек из %s", len(points), file_path)
    return points


# ---------------------------------------------------------------------------
# XML парсер (Leica / Trimble)
# ---------------------------------------------------------------------------
def parse_xml_measurements(file_path: str) -> list[dict[str, Any]]:
    """Парсит XML-файл замеров (поддержка Leica GSI-XML, LandXML, Trimble JXL)."""
    points: list[dict[str, Any]] = []

    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
    except ET.ParseError as e:
        logger.error("Ошибка XML: %s", e)
        return points

    # Универсальный поиск точек
    ns = _detect_xml_namespace(root)

    # LandXML: <CgPoints><CgPoint name="..." >X Y Z</CgPoint>
    for cg in root.iter(f"{ns}CgPoint"):
        name = cg.get("name", cg.get("pntRef", ""))
        text = (cg.text or "").strip()
        coords = text.split()
        if len(coords) >= 3:
            try:
                y, x, z = float(coords[0]), float(coords[1]), float(coords[2])
                point = _classify_point(name, x, y, z)
                points.append(point)
            except ValueError:
                continue

    # Leica / generic: <Point><Name>, <North>/<East>/<Height>
    for pt_elem in root.iter(f"{ns}Point"):
        name_elem = pt_elem.find(f"{ns}Name")
        if name_elem is None:
            name_elem = pt_elem.find(f"{ns}PointID")
        name = (name_elem.text if name_elem is not None else "").strip()

        x = _xml_float(pt_elem, f"{ns}East", f"{ns}X", f"{ns}Easting")
        y = _xml_float(pt_elem, f"{ns}North", f"{ns}Y", f"{ns}Northing")
        z = _xml_float(pt_elem, f"{ns}Height", f"{ns}Z", f"{ns}Elevation")

        if x is not None and y is not None:
            point = _classify_point(name, x, y, z or 0.0)
            points.append(point)

    logger.info("XML: прочитано %d точек из %s", len(points), file_path)
    return points


def _detect_xml_namespace(root: ET.Element) -> str:
    """Извлекает пространство имён из корневого элемента."""
    tag = root.tag
    if tag.startswith("{"):
        ns = tag.split("}")[0] + "}"
        return ns
    return ""


def _xml_float(elem: ET.Element, *tag_names: str) -> float | None:
    """Ищет числовое значение в дочерних элементах."""
    for tag in tag_names:
        child = elem.find(tag)
        if child is not None and child.text:
            try:
                return float(child.text.strip().replace(",", "."))
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Привязка точек к опорам
# ---------------------------------------------------------------------------
def match_points_to_poles(
    points: list[dict[str, Any]],
    poles: list[dict[str, Any]],
    threshold_m: float = 2.0,
) -> dict[str, list[dict[str, Any]]]:
    """
    Привязывает точки замеров к проектным опорам.

    Стратегия:
    1. По имени точки (pole_id из суффиксов .1/.2/.3)
    2. По расстоянию до проектного центра (< threshold_m)

    Returns:
        Словарь: {pole_name: [список привязанных точек]}
    """
    # Индекс опор по имени
    pole_index: dict[str, dict[str, Any]] = {
        p["name"]: p for p in poles if p.get("name")
    }

    matched: dict[str, list[dict[str, Any]]] = {p["name"]: [] for p in poles if p.get("name")}

    for point in points:
        if point.get("is_station"):
            continue

        pole_id = point.get("pole_id", "")

        # 1. Прямое совпадение по имени
        if pole_id and pole_id in pole_index:
            matched[pole_id].append(point)
            continue

        # 2. Совпадение по расстоянию
        if not pole_id:
            pt = Point2D(point["x"], point["y"])
            best_dist = float("inf")
            best_pole = ""
            for pname, pdata in pole_index.items():
                if pdata.get("x") and pdata.get("y"):
                    pole_pt = Point2D(pdata["x"], pdata["y"])
                    d = distance_2d(pt, pole_pt)
                    if d < best_dist and d < threshold_m:
                        best_dist = d
                        best_pole = pname

            if best_pole:
                point["pole_id"] = best_pole
                matched[best_pole].append(point)

    # Логируем статистику
    total_matched = sum(len(v) for v in matched.values())
    poles_with_points = sum(1 for v in matched.values() if v)
    logger.info(
        "Привязка: %d точек к %d опорам (из %d всего)",
        total_matched, poles_with_points, len(poles),
    )

    return matched


def classify_pole_points(
    points: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Разделяет точки опоры на нижние и верхние.

    Стратегия:
    - Если суффиксы .1 — нижние, .2/.3 — верхние
    - Если суффиксов нет — по Z: нижняя половина / верхняя половина
    - Если 3 точки — .1 нижняя, .2/.3 верхние (для 2-уровневой съёмки)
    - Если 6 точек — .1/.2/.3 нижние, .4/.5/.6 верхние
    """
    if not points:
        return [], []

    suffixes = [p.get("point_suffix", "") for p in points]
    has_suffixes = any(s for s in suffixes)

    if has_suffixes:
        max_suffix = max(int(s) for s in suffixes if s.isdigit()) if any(
            s.isdigit() for s in suffixes
        ) else 3

        if max_suffix <= 3:
            # 3 точки: .1 = нижняя, .2/.3 = верхние
            lower = [p for p in points if p.get("point_suffix") == "1"]
            upper = [p for p in points if p.get("point_suffix") in ("2", "3")]
        else:
            # 6+ точек: первая половина — нижние, вторая — верхние
            mid = max_suffix // 2
            lower = [p for p in points if p.get("point_suffix", "").isdigit()
                     and int(p["point_suffix"]) <= mid]
            upper = [p for p in points if p.get("point_suffix", "").isdigit()
                     and int(p["point_suffix"]) > mid]
    else:
        # По Z: сортируем и делим пополам
        sorted_pts = sorted(points, key=lambda p: p.get("z", 0))
        mid = len(sorted_pts) // 2
        lower = sorted_pts[:mid] if mid > 0 else sorted_pts[:1]
        upper = sorted_pts[mid:] if mid > 0 else sorted_pts[1:]

    return lower, upper
