"""
Парсер геодезических замеров (TXT, DXF, XML).

Поддерживаемые форматы:
- TXT: CSV без заголовка; по умолчанию «Имя,X,Y,Z», опционально «Имя,Y,X,Z»
  (см. measurement_txt_coord_order / настройки замеров)
- DXF: расширенный парсинг геодезических файлов с поддержкой:
  1) INSERT блока замера (обычно «Measured») с координатой в точке вставки;
  2) классических POINT;
  3) подписей TEXT и MTEXT.
- XML: Leica/Trimble форматы.

Новая логика DXF (для формата наподобие 176-171.dxf):
- Собираем геометрию из INSERT и POINT modelspace;
- Собираем подписи из TEXT/MTEXT;
- Нормализуем подпись (игнор регистра/лишних пробелов);
- Извлекаем номер опоры из строки и приводим к виду N###
  (пример: N171, n 171, 171.18 -> N171);
- Для каждой точки ищем ближайшую подпись в радиусе 5 м;
- Логируем детальную статистику (кол-во сущностей, распознанных подписей,
  сопоставленных точек и уникальных опор).

Привязка точек к опорам:
- По расстоянию в плане (порог 2 м по умолчанию)
- По имени точки (суффикс .1/.2/.3 → номер замера, префикс → номер опоры)
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ezdxf

from utils.geometry import Point2D, Point3D, distance_2d

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)
_DXF_LABEL_RADIUS_M = 5.0
_DXF_POLE_RE_PREF_N = re.compile(r'^N\s*(\d{1,4})$', re.IGNORECASE)
_DXF_POLE_RE_NUMERIC = re.compile(r'^(\d{3,4})(?:\.(\d+))?$')


def debug_parse_measurements_dxf(
    dxf_path: str,
    *,
    block_name: str = "Measured",
    preferred_label_layer: str = "Nomer",
    search_radius_m: float = _DXF_LABEL_RADIUS_M,
) -> dict[str, Any]:
    """
    Диагностический отчёт по DXF-замерам.

    Не меняет рабочую логику, но возвращает структуру, которая помогает понять:
    - какие подписи реально присутствуют в файле,
    - сколько точек не удалось сопоставить в текущем радиусе,
    - какие именно опоры были распознаны.
    """
    report: dict[str, Any] = {
        "dxf_path": dxf_path,
        "block_name": block_name,
        "preferred_label_layer": preferred_label_layer,
        "search_radius_m": float(search_radius_m),
        "insert_count": 0,
        "point_count": 0,
        "text_count": 0,
        "mtext_count": 0,
        "normalized_label_count": 0,
        "normalized_labels_unique": [],
        "matched_points_count": 0,
        "matched_points_by_pole": {},
        "unmatched_points_count": 0,
        "unmatched_points_preview": [],
        "raw_label_samples": [],
        "warning": "",
    }

    try:
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()
    except Exception as e:
        report["warning"] = f"DXF read failed: {e}"
        return report

    def _clean_text(raw: str) -> str:
        return re.sub(r"\s+", " ", (raw or "").strip())

    def _normalize_pole_name(raw: str) -> str | None:
        text = _clean_text(raw).upper()
        if not text:
            return None
        compact = re.sub(r"\s+", "", text)
        m_n = _DXF_POLE_RE_PREF_N.match(compact)
        if m_n:
            return f"N{int(m_n.group(1))}"
        m_num = _DXF_POLE_RE_NUMERIC.match(compact)
        if m_num:
            return f"N{int(m_num.group(1))}"
        return None

    inserts: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []

    for ent in msp.query("INSERT"):
        try:
            if str(getattr(ent.dxf, "name", "") or "").lower() != block_name.lower():
                continue
            ins = ent.dxf.insert
            inserts.append(
                {
                    "x": float(ins.x),
                    "y": float(ins.y),
                    "z": float(ins.z),
                    "layer": str(getattr(ent.dxf, "layer", "") or ""),
                }
            )
        except Exception:
            continue

    for ent in msp.query("POINT"):
        try:
            loc = ent.dxf.location
            points.append(
                {
                    "x": float(loc.x),
                    "y": float(loc.y),
                    "z": float(loc.z),
                    "layer": str(getattr(ent.dxf, "layer", "") or ""),
                }
            )
        except Exception:
            continue

    for ent in msp.query("TEXT"):
        try:
            txt = _clean_text(str(getattr(ent.dxf, "text", "") or ""))
            pos = ent.dxf.insert
            labels.append(
                {
                    "raw_text": txt,
                    "name": _normalize_pole_name(txt),
                    "x": float(pos.x),
                    "y": float(pos.y),
                    "z": float(pos.z),
                    "layer": str(getattr(ent.dxf, "layer", "") or ""),
                    "kind": "TEXT",
                }
            )
        except Exception:
            continue

    for ent in msp.query("MTEXT"):
        try:
            txt = _clean_text(ent.plain_text())
            pos = ent.dxf.insert
            labels.append(
                {
                    "raw_text": txt,
                    "name": _normalize_pole_name(txt),
                    "x": float(pos.x),
                    "y": float(pos.y),
                    "z": float(pos.z),
                    "layer": str(getattr(ent.dxf, "layer", "") or ""),
                    "kind": "MTEXT",
                }
            )
        except Exception:
            continue

    report["insert_count"] = len(inserts)
    report["point_count"] = len(points)
    report["text_count"] = sum(1 for l in labels if l["kind"] == "TEXT")
    report["mtext_count"] = sum(1 for l in labels if l["kind"] == "MTEXT")
    report["raw_label_samples"] = [l["raw_text"] for l in labels[:30]]

    valid_all = [l for l in labels if l["name"]]
    valid_layer = [l for l in valid_all if str(l["layer"]).lower() == preferred_label_layer.lower()]
    valid = valid_layer or valid_all
    unique_norm = sorted({str(l["name"]) for l in valid})
    report["normalized_label_count"] = len(valid)
    report["normalized_labels_unique"] = unique_norm

    geom = inserts if inserts else points
    matched_by_pole: dict[str, int] = {}
    unmatched: list[dict[str, Any]] = []
    for g in geom:
        gp = Point3D(g["x"], g["y"], g["z"])
        best_name = None
        best_dist = float("inf")
        for lbl in valid:
            lp = Point3D(lbl["x"], lbl["y"], lbl["z"])
            d = distance_2d(gp, lp)
            if d <= search_radius_m and d < best_dist:
                best_dist = d
                best_name = str(lbl["name"])
        if best_name:
            matched_by_pole[best_name] = matched_by_pole.get(best_name, 0) + 1
        else:
            unmatched.append(
                {
                    "x": round(float(g["x"]), 3),
                    "y": round(float(g["y"]), 3),
                    "z": round(float(g["z"]), 3),
                    "layer": str(g.get("layer", "")),
                }
            )

    report["matched_points_by_pole"] = dict(sorted(matched_by_pole.items()))
    report["matched_points_count"] = sum(matched_by_pole.values())
    report["unmatched_points_count"] = len(unmatched)
    report["unmatched_points_preview"] = unmatched[:20]
    if len(unique_norm) <= 2:
        report["warning"] = (
            "В DXF распознано мало уникальных подписей опор. "
            "Проверьте, действительно ли в файле есть N172..N178 "
            "или они расположены в другом слое/формате подписи."
        )
    return report


# ---------------------------------------------------------------------------
# Универсальный диспетчер
# ---------------------------------------------------------------------------
def parse_measurement_file(
    file_path: str,
    *,
    txt_coord_order: str = "xy",
    app_cfg: "AppConfig | None" = None,
) -> list[dict[str, Any]]:
    """
    Парсит файл замеров, определяя формат по расширению.

    Args:
        file_path: путь к файлу
        txt_coord_order: для .txt — «xy» (колонки X,Y,Z) или «yx» (Y,X,Z после имени)
        app_cfg: для .dxf — имена блока/слоя и порог подписи

    Returns:
        Список точек: [{name, x, y, z, pole_id, point_suffix, is_station}, ...]
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".txt":
        return parse_txt_measurements(file_path, coord_order=txt_coord_order)
    elif ext == ".dxf":
        return parse_dxf_measurements(file_path, cfg=app_cfg)
    elif ext == ".xml":
        return parse_xml_measurements(file_path)
    else:
        logger.warning("Неизвестный формат файла: %s", ext)
        return []


def parse_measurements_txt(file_path: str) -> list[dict[str, Any]]:
    """Совместимый wrapper: TXT-парсинг без изменения старой логики."""
    return parse_txt_measurements(file_path)


def parse_measurements_dxf(
    dxf_path: str,
    *,
    block_name: str = "Measured",
    preferred_label_layer: str = "Nomer",
    search_radius_m: float = _DXF_LABEL_RADIUS_M,
    include_unlabeled: bool = False,
    allow_raw_label_fallback: bool = False,
    prioritize_insert: bool = True,
) -> list[dict[str, Any]]:
    """
    Надёжный DXF-парсинг для геодезических замеров.

    Возвращает список точек в формате:
      [{'name': 'N171', 'x': ..., 'y': ..., 'z': ..., 'layer': ...}, ...]
    """
    points: list[dict[str, Any]] = []
    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as e:
        logger.error("DXF: ошибка чтения %s: %s", dxf_path, e)
        return points

    try:
        msp = doc.modelspace()
    except Exception as e:
        logger.error("DXF: не удалось получить modelspace %s: %s", dxf_path, e)
        return points

    insert_geometry: list[dict[str, Any]] = []
    point_geometry: list[dict[str, Any]] = []
    label_candidates: list[dict[str, Any]] = []

    def _clean_text(raw: str) -> str:
        return re.sub(r"\s+", " ", (raw or "").strip())

    def _normalize_pole_name(raw: str) -> str | None:
        text = _clean_text(raw).upper()
        if not text:
            return None
        compact = re.sub(r"\s+", "", text)

        m_n = _DXF_POLE_RE_PREF_N.match(compact)
        if m_n:
            return f"N{int(m_n.group(1))}"

        m_num = _DXF_POLE_RE_NUMERIC.match(compact)
        if m_num:
            # 171.18 -> N171, 176 -> N176
            return f"N{int(m_num.group(1))}"
        return None

    # 1) Геометрия: INSERT блока замера + POINT
    for ent in msp.query("INSERT"):
        try:
            name = str(getattr(ent.dxf, "name", "") or "")
            if name.lower() != block_name.lower():
                continue
            ins = ent.dxf.insert
            insert_geometry.append(
                {
                    "kind": "INSERT",
                    "x": float(ins.x),
                    "y": float(ins.y),
                    "z": float(ins.z),
                    "layer": str(getattr(ent.dxf, "layer", "") or ""),
                }
            )
        except Exception:
            continue

    for ent in msp.query("POINT"):
        try:
            loc = ent.dxf.location
            point_geometry.append(
                {
                    "kind": "POINT",
                    "x": float(loc.x),
                    "y": float(loc.y),
                    "z": float(loc.z),
                    "layer": str(getattr(ent.dxf, "layer", "") or ""),
                }
            )
        except Exception:
            continue

    # 2) Подписи: TEXT + MTEXT
    for ent in msp.query("TEXT"):
        try:
            txt = _clean_text(str(getattr(ent.dxf, "text", "") or ""))
            pos = ent.dxf.insert
            label_candidates.append(
                {
                    "raw_text": txt,
                    "name": _normalize_pole_name(txt),
                    "x": float(pos.x),
                    "y": float(pos.y),
                    "z": float(pos.z),
                    "layer": str(getattr(ent.dxf, "layer", "") or ""),
                }
            )
        except Exception:
            continue

    for ent in msp.query("MTEXT"):
        try:
            txt = _clean_text(ent.plain_text())
            pos = ent.dxf.insert
            label_candidates.append(
                {
                    "raw_text": txt,
                    "name": _normalize_pole_name(txt),
                    "x": float(pos.x),
                    "y": float(pos.y),
                    "z": float(pos.z),
                    "layer": str(getattr(ent.dxf, "layer", "") or ""),
                }
            )
        except Exception:
            continue

    valid_labels_all = [l for l in label_candidates if l["name"]]
    valid_labels = [
        l for l in valid_labels_all
        if str(l.get("layer", "")).lower() == preferred_label_layer.lower()
    ]
    if not valid_labels:
        valid_labels = valid_labels_all

    if prioritize_insert and insert_geometry:
        geometry = insert_geometry
    else:
        geometry = insert_geometry + point_geometry

    logger.info(
        (
            "DXF %s: insert=%d, point=%d, used_geometry=%d, labels=%d (TEXT+MTEXT), "
            "valid_pole_labels=%d, radius=%.3f m"
        ),
        dxf_path,
        len(insert_geometry),
        len(point_geometry),
        len(geometry),
        len(label_candidates),
        len(valid_labels_all),
        float(search_radius_m),
    )

    # 3) Сопоставление геометрии с ближайшей валидной подписью в радиусе 5 м
    for g in geometry:
        gp = Point3D(g["x"], g["y"], g["z"])
        best_name = None
        best_dist = float("inf")
        for lbl in valid_labels:
            lp = Point3D(lbl["x"], lbl["y"], lbl["z"])
            d = distance_2d(gp, lp)
            if d <= search_radius_m and d < best_dist:
                best_dist = d
                best_name = lbl["name"]
        if not best_name:
            if include_unlabeled:
                best_name = f"P_{g['x']:.3f}_{g['y']:.3f}"
            else:
                continue
        if allow_raw_label_fallback and best_name.startswith("P_"):
            # Ищем ближайший сырой текст, даже если не распознан как N###.
            best_raw = ""
            best_raw_dist = float("inf")
            for lbl in label_candidates:
                lp = Point3D(lbl["x"], lbl["y"], lbl["z"])
                d = distance_2d(gp, lp)
                if d <= search_radius_m and d < best_raw_dist:
                    best_raw_dist = d
                    best_raw = str(lbl.get("raw_text", "") or "")
            cleaned_raw = _clean_text(best_raw)
            if cleaned_raw:
                best_name = cleaned_raw
        points.append(
            {
                "name": best_name,
                "x": g["x"],
                "y": g["y"],
                "z": g["z"],
                "layer": g["layer"],
            }
        )

    unique_poles = sorted({p["name"] for p in points})
    poles_preview = unique_poles[:20]
    logger.info(
        "DXF %s: matched_points=%d, unique_poles=%d, poles_preview=%s",
        dxf_path,
        len(points),
        len(unique_poles),
        poles_preview if poles_preview else "none",
    )
    if len(unique_poles) <= 2:
        logger.warning(
            (
                "DXF matched low unique pole count (%d). "
                "Run debug_parse_measurements_dxf(...) for detailed report."
            ),
            len(unique_poles),
        )
    return points


def parse_measurements(file_path: str) -> list[dict[str, Any]]:
    """
    Автоопределение формата файла:
    - .dxf -> parse_measurements_dxf
    - .txt -> parse_measurements_txt
    - .xml -> parse_xml_measurements
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".dxf":
        return parse_measurements_dxf(file_path)
    if ext == ".txt":
        return parse_measurements_txt(file_path)
    if ext == ".xml":
        return parse_xml_measurements(file_path)
    logger.warning("parse_measurements: неизвестный формат %s", ext)
    return []


# ---------------------------------------------------------------------------
# TXT парсер (формат: ИмяТочки,X,Y,Z)
# ---------------------------------------------------------------------------
# Паттерн имени точки опоры: «573A.3» или «574.1» и т.д.
_POLE_POINT_RE = re.compile(r'^(\d{1,4}[A-Za-zА-Яа-я]?)\.(\d+)$')
# Паттерн точки стояния: «1 (34)» или «2(12)»
_STATION_RE = re.compile(r'^\d+\s*\(\d+\)$')


def parse_txt_measurements(
    file_path: str,
    coord_order: str = "xy",
) -> list[dict[str, Any]]:
    """
    Парсит TXT-файл замеров (CSV без заголовка).

    Формат строки (coord_order «xy»): ИмяТочки,X,Y,Z
    Пример: 573A.3,74204.183,119389.735,22.087

    Формат (coord_order «yx»): ИмяТочки,Y,X,Z — сначала северная/широтная, потом восточная
    (типично для части выгрузок с тахеометра).
    """
    points: list[dict[str, Any]] = []
    order = (coord_order or "xy").strip().lower()
    if order not in ("xy", "yx"):
        order = "xy"

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
                c1 = float(parts[1].strip().replace(",", "."))
                c2 = float(parts[2].strip().replace(",", "."))
                z = float(parts[3].strip().replace(",", "."))
                if order == "yx":
                    y, x = c1, c2
                else:
                    x, y = c1, c2
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
def _parse_dxf_measured_inserts(
    msp: Any,
    block_name: str,
    label_layer: str,
    match_radius_m: float,
) -> list[dict[str, Any]]:
    """
    Точки из INSERT блока (например Trimble: блок «Measured»), координаты — точка вставки;
    подпись номера — на слое label_layer (TEXT/MTEXT рядом с вставкой).
    """
    points: list[dict[str, Any]] = []
    layer_q = label_layer.replace('"', '\\"')
    try:
        label_texts = list(msp.query(f'TEXT[layer=="{layer_q}"]'))
    except Exception:
        label_texts = []
    try:
        label_texts.extend(msp.query(f'MTEXT[layer=="{layer_q}"]'))
    except Exception:
        pass

    def _text_pos(ent: Any) -> Point3D:
        if ent.dxftype() == "MTEXT":
            return Point3D(ent.dxf.insert.x, ent.dxf.insert.y, ent.dxf.insert.z)
        return Point3D(ent.dxf.insert.x, ent.dxf.insert.y, ent.dxf.insert.z)

    def _text_content(ent: Any) -> str:
        if ent.dxftype() == "MTEXT":
            try:
                return ent.plain_text().strip()
            except Exception:
                return (getattr(ent, "text", "") or "").strip()
        return (ent.dxf.text or "").strip()

    for entity in msp.query("INSERT"):
        if (entity.dxf.name or "") != block_name:
            continue
        ins = entity.dxf.insert
        ix, iy, iz = float(ins.x), float(ins.y), float(ins.z)
        ins_pt = Point3D(ix, iy, iz)

        best_dist = float("inf")
        best_name = ""
        for t_ent in label_texts:
            tpos = _text_pos(t_ent)
            d = distance_2d(ins_pt, tpos)
            if d < best_dist and d <= match_radius_m:
                best_dist = d
                best_name = _text_content(t_ent)

        name = best_name or f"P_{ix:.1f}_{iy:.1f}"
        points.append(_classify_point(name, ix, iy, iz))

    return points


def parse_dxf_measurements(
    file_path: str,
    cfg: "AppConfig | None" = None,
) -> list[dict[str, Any]]:
    """Совместимый API: DXF -> общий парсер + преобразование в внутренний формат."""
    from config import AppConfig

    c = cfg or AppConfig()
    block_name = getattr(c, "dxf_measurement_block_name", "Measured") or "Measured"
    label_layer = getattr(c, "dxf_measurement_label_layer", "Nomer") or "Nomer"
    match_r = float(getattr(c, "dxf_measurement_label_radius_m", 0.15) or 0.15)

    raw_points = parse_measurements_dxf(
        file_path,
        block_name=block_name,
        preferred_label_layer=label_layer,
        search_radius_m=match_r,
        include_unlabeled=True,
        allow_raw_label_fallback=True,
        prioritize_insert=True,
    )
    points: list[dict[str, Any]] = []
    for rp in raw_points:
        name = str(rp.get("name", "") or "").strip()
        x = float(rp.get("x", 0.0) or 0.0)
        y = float(rp.get("y", 0.0) or 0.0)
        z = float(rp.get("z", 0.0) or 0.0)
        point = _classify_point(name, x, y, z)
        # Для имени N171 сохраняем совместимую привязку к проектной опоре 171.
        m = re.match(r'^[Nn]\s*(\d+)$', name)
        if m:
            point["pole_id"] = m.group(1)
        if "layer" in rp:
            point["layer"] = rp["layer"]
        points.append(point)

    logger.info(
        "DXF замеры: преобразовано %d точек в совместимый формат (%s)",
        len(points),
        file_path,
    )
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

    def _has_xy_coords(pole_data: dict[str, Any]) -> bool:
        """Проверяет наличие координат, не отбрасывая валидные 0.0."""
        return pole_data.get("x") is not None and pole_data.get("y") is not None

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
                if _has_xy_coords(pdata):
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


def trim_pole_points_for_verticality(
    points: list[dict[str, Any]],
    lower_n: int,
    upper_n: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Если на опоре больше точек, чем lower_n + upper_n, оставляет lower_n с минимальным Z
    и upper_n с максимальным Z (промежуточные повторы/лишние съёмки отбрасываются).
    """
    need = lower_n + upper_n
    if len(points) <= need:
        return list(points), None

    indexed = list(enumerate(points))
    sorted_by_z = sorted(
        indexed,
        key=lambda t: float(t[1].get("z", 0.0)),
    )
    keep_idx: set[int] = set()
    for i in range(lower_n):
        keep_idx.add(sorted_by_z[i][0])
    for i in range(upper_n):
        keep_idx.add(sorted_by_z[-(i + 1)][0])

    trimmed = [points[i] for i in sorted(keep_idx)]
    note = (
        f"Отобрано {need} из {len(points)} точек: {lower_n} с минимальным Z и {upper_n} с максимальным Z "
        "(промежуточные съёмки исключены)."
    )
    return trimmed, note


def classify_pole_points(
    points: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Разделяет точки опоры на нижние и верхние.

    Стратегия:
    - Ровно 6 точек: 3 с минимальным Z (низ) и 3 с максимальным Z (верх)
    - Если суффиксы .1 — нижние, .2/.3 — верхние (до 3 точек по суффиксам)
    - Если суффиксов нет — по Z: нижняя половина / верхняя половина
    - Если 3 точки — .1 нижняя, .2/.3 верхние
    - Если 6+ точек с суффиксами — по номеру суффикса (половина / половина)
    """
    if not points:
        return [], []

    if len(points) == 6:
        sorted_pts = sorted(
            points,
            key=lambda p: (
                float(p.get("z", 0.0)),
                int(p["point_suffix"])
                if str(p.get("point_suffix", "")).isdigit()
                else 0,
            ),
        )
        return sorted_pts[:3], sorted_pts[3:]

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
