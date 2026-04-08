"""
Парсер проектных данных: PDF (ведомости опор) + DXF (координаты).

Извлекает из проектной документации:
- Номера опор, типы, проектные высоты
- Координаты опор (из DXF — приоритет, из PDF — fallback)
- Объединяет данные из обоих источников

Формат DXF проекта (по анализу TPAMBAY4.dxf):
- CIRCLE на слое 0_Point_Symbols -> центры опор
- TEXT на слое 0_Point_Name -> метки NXX
- TEXT на слое 0_Point_Height -> высоты
- Привязка элементов по близости координат
"""
from __future__ import annotations

import logging
import re
from typing import Any

import ezdxf

from config import AppConfig
from utils.geometry import Point3D, distance_2d
from utils.pdf_utils import (
    extract_tables_pdfplumber,
    extract_page_text,
    get_pdf_page_count,
    parse_table_with_ai,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------
def _empty_pole(name: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "type": "",
        "height": 0.0,
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
        "source": "",
    }


# ---------------------------------------------------------------------------
# Парсинг DXF проекта
# ---------------------------------------------------------------------------
def parse_dxf_project(dxf_path: str, cfg: AppConfig) -> list[dict[str, Any]]:
    """
    Извлекает опоры из DXF-плана проекта.

    Стратегия:
    1. Собираем CIRCLE на слое 0_Point_Symbols -> центры
    2. Собираем TEXT на слое 0_Point_Name -> имена (NXX)
    3. Собираем TEXT на слое 0_Point_Height -> высоты
    4. Сопоставляем по близости координат (< 3 ед.)
    """
    layers = cfg.dxf_layers
    poles: list[dict[str, Any]] = []

    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as e:
        logger.error("Ошибка чтения DXF: %s", e)
        return poles

    msp = doc.modelspace()

    # 1. Центры опор (CIRCLE)
    circles: list[Point3D] = []
    for entity in msp.query(f'CIRCLE[layer=="{layers.project_symbols}"]'):
        c = entity.dxf.center
        circles.append(Point3D(c.x, c.y, c.z))

    # Также проверяем слой «Опоры проект» (дублирует, но может содержать уникальные)
    for entity in msp.query('CIRCLE[layer=="Опоры проект"]'):
        c = entity.dxf.center
        pt = Point3D(c.x, c.y, c.z)
        # Не добавляем дубликаты (расстояние < 0.1 м)
        if not any(distance_2d(pt, existing) < 0.1 for existing in circles):
            circles.append(pt)

    # 2. Имена (TEXT на слое 0_Point_Name)
    names: list[tuple[Point3D, str]] = []
    for entity in msp.query(f'TEXT[layer=="{layers.project_name}"]'):
        pos = entity.dxf.insert
        text = entity.dxf.text.strip()
        if text:
            names.append((Point3D(pos.x, pos.y, pos.z), text))

    # 3. Высоты (TEXT на слое 0_Point_Height)
    heights: list[tuple[Point3D, float]] = []
    for entity in msp.query(f'TEXT[layer=="{layers.project_height}"]'):
        pos = entity.dxf.insert
        text = entity.dxf.text.strip()
        try:
            h = float(text.replace(",", "."))
            heights.append((Point3D(pos.x, pos.y, pos.z), h))
        except ValueError:
            continue

    logger.info(
        "DXF: кругов=%d, имён=%d, высот=%d",
        len(circles), len(names), len(heights),
    )

    # 4. Сопоставление
    match_radius = 5.0  # единиц чертежа (метров) — имена/высоты рядом с кругом

    for center in circles:
        pole = _empty_pole()
        pole["x"] = center.x
        pole["y"] = center.y
        pole["z"] = center.z
        pole["source"] = "dxf"

        # Ближайшее имя
        best_name_dist = float("inf")
        for pt, name_text in names:
            d = distance_2d(center, pt)
            if d < best_name_dist and d < match_radius:
                best_name_dist = d
                pole["name"] = _clean_pole_name(name_text)

        # Ближайшая высота
        best_h_dist = float("inf")
        for pt, h_val in heights:
            d = distance_2d(center, pt)
            if d < best_h_dist and d < match_radius:
                best_h_dist = d
                pole["height"] = h_val

        if pole["name"]:
            poles.append(pole)

    logger.info("DXF: извлечено %d опор", len(poles))
    return poles


def _clean_pole_name(raw: str) -> str:
    """Очищает имя опоры: 'N317' -> '317', 'П-317' -> '317'."""
    cleaned = raw.strip()
    cleaned = re.sub(r'^[NnNПп\-_]+', '', cleaned)
    return cleaned or raw.strip()


# ---------------------------------------------------------------------------
# Парсинг PDF проекта
# ---------------------------------------------------------------------------

# Промпт для AI-парсинга ведомости опор
_AI_PROMPT_POLE_TABLE = """
Ты — геодезист-эксперт. На изображении — страница из проектной документации
контактной сети трамвая. Найди таблицу с ведомостью опор.

Извлеки данные в формате JSON-массива. Каждый элемент:
{
    "name": "номер опоры (только цифры, без N/П)",
    "type": "тип опоры (напр. ОФК-2300-10)",
    "height": высота опоры в метрах (число),
    "x": координата X (число или null),
    "y": координата Y (число или null)
}

Правила:
- Номер опоры — только числовая часть (216, 217, 317 и т.д.)
- Если координат нет — ставь null
- Высоту бери из колонки «высота» или «H»
- Тип опоры — полное обозначение (ТФГ-1500-10, ОФК-2300-10 и т.д.)

Ответь ТОЛЬКО JSON-массивом, без пояснений.
"""


def parse_pdf_project(pdf_path: str, cfg: AppConfig) -> list[dict[str, Any]]:
    """
    Извлекает данные опор из PDF проекта.

    Стратегия:
    1. Сканируем текстовые слои всех страниц
    2. Ищем ведомости опор (по ключевым словам)
    3. Извлекаем таблицы через pdfplumber
    4. Если таблицы плохо парсятся — используем AI (Gemini 2.5 Pro)
    """
    poles: list[dict[str, Any]] = []
    page_count = get_pdf_page_count(pdf_path)
    logger.info("PDF: %d страниц", page_count)

    # Ищем страницы с ведомостями
    vedmost_pages: list[int] = []
    for i in range(page_count):
        text = extract_page_text(pdf_path, i)
        text_lower = text.lower()
        if any(kw in text_lower for kw in ["ведомост", "опор", "фундамент"]):
            if any(kw in text_lower for kw in ["номер", "тип", "высота", "марка"]):
                vedmost_pages.append(i)

    logger.info("PDF: найдено %d страниц с ведомостями: %s", len(vedmost_pages), vedmost_pages)

    # Пробуем pdfplumber
    for page_idx in vedmost_pages:
        page_poles = _parse_vedmost_pdfplumber(pdf_path, page_idx)
        if page_poles:
            poles.extend(page_poles)

    # Если pdfplumber не дал результатов — пробуем AI
    if not poles and cfg.openrouter.api_key:
        logger.info("pdfplumber не извлёк данные, пробуем AI...")
        for page_idx in vedmost_pages[:5]:  # максимум 5 страниц через AI
            ai_poles = parse_table_with_ai(
                pdf_path, page_idx, _AI_PROMPT_POLE_TABLE, cfg.openrouter
            )
            for item in ai_poles:
                pole = _empty_pole(str(item.get("name", "")))
                pole["type"] = str(item.get("type", ""))
                pole["height"] = float(item.get("height", 0) or 0)
                pole["x"] = float(item.get("x", 0) or 0)
                pole["y"] = float(item.get("y", 0) or 0)
                pole["source"] = "pdf_ai"
                if pole["name"]:
                    poles.append(pole)

    # Fallback: извлечение из текста по регулярным выражениям
    if not poles:
        logger.info("AI недоступен или не дал результат, пробуем regex...")
        poles = _parse_poles_regex(pdf_path, vedmost_pages)

    logger.info("PDF: итого извлечено %d опор", len(poles))
    return poles


def _parse_vedmost_pdfplumber(pdf_path: str, page_idx: int) -> list[dict[str, Any]]:
    """Парсит ведомость опор через pdfplumber."""
    tables = extract_tables_pdfplumber(pdf_path, [page_idx])
    poles: list[dict[str, Any]] = []

    for table in tables:
        if len(table) < 2:
            continue

        # Определяем индексы колонок по заголовку
        header = [str(c or "").lower().strip() for c in table[0]]
        col_map = _detect_columns(header)

        if "name" not in col_map:
            continue

        for row in table[1:]:
            if not row or len(row) <= max(col_map.values()):
                continue

            name_val = str(row[col_map["name"]] or "").strip()
            name_val = _clean_pole_name(name_val)
            if not name_val or not re.search(r'\d', name_val):
                continue

            pole = _empty_pole(name_val)
            pole["source"] = "pdf_table"

            if "type" in col_map:
                pole["type"] = str(row[col_map["type"]] or "").strip()
            if "height" in col_map:
                try:
                    pole["height"] = float(
                        str(row[col_map["height"]] or "0").replace(",", ".")
                    )
                except ValueError:
                    pass

            poles.append(pole)

    return poles


def _detect_columns(header: list[str]) -> dict[str, int]:
    """Автоматическое определение колонок по заголовку таблицы."""
    col_map: dict[str, int] = {}
    for i, cell in enumerate(header):
        if not cell:
            continue
        if any(kw in cell for kw in ["номер", "№", "n ", "опор"]):
            col_map.setdefault("name", i)
        elif any(kw in cell for kw in ["тип", "марка", "обозначен"]):
            col_map.setdefault("type", i)
        elif any(kw in cell for kw in ["высота", "h ", "высот"]):
            col_map.setdefault("height", i)
        elif "x" in cell or "абсцис" in cell:
            col_map.setdefault("x", i)
        elif "y" in cell or "ордин" in cell:
            col_map.setdefault("y", i)
    return col_map


def _parse_poles_regex(pdf_path: str, pages: list[int]) -> list[dict[str, Any]]:
    """Fallback-парсинг опор через регулярные выражения из текста PDF."""
    poles: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    # Паттерны для опор
    pole_pattern = re.compile(
        r'(?:опора|оп\.?|N|П|п)\s*[№#]?\s*(\d{1,4}[A-Za-zА-Яа-я]?)',
        re.IGNORECASE,
    )
    type_pattern = re.compile(
        r'((?:ТФГ|ОФК|СТ|КС)\s*[-–]\s*\d{3,4}\s*[-–]\s*\d{1,3})',
        re.IGNORECASE,
    )

    for page_idx in pages:
        text = extract_page_text(pdf_path, page_idx)
        if not text:
            continue

        for match in pole_pattern.finditer(text):
            name = match.group(1).strip()
            if name in seen_names:
                continue
            seen_names.add(name)

            pole = _empty_pole(name)
            pole["source"] = "pdf_regex"

            # Ищем тип рядом
            context = text[max(0, match.start() - 100):match.end() + 200]
            type_match = type_pattern.search(context)
            if type_match:
                pole["type"] = type_match.group(1).strip()

            poles.append(pole)

    return poles


# ---------------------------------------------------------------------------
# Объединение данных PDF + DXF
# ---------------------------------------------------------------------------
def merge_project_data(
    pdf_data: list[dict[str, Any]],
    dxf_data: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    Объединяет данные из PDF и DXF.

    Приоритеты:
    - Координаты: DXF (точнее) > PDF
    - Тип/высота: PDF (таблицы) > DXF (если есть)
    - Если DXF не загружен — только PDF
    """
    if not dxf_data:
        return pdf_data if pdf_data else []

    # Строим индекс по имени из DXF
    dxf_index: dict[str, dict[str, Any]] = {}
    for pole in dxf_data:
        name = pole.get("name", "")
        if name:
            dxf_index[name] = pole

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Сначала обрабатываем данные PDF
    for pdf_pole in pdf_data:
        name = pdf_pole.get("name", "")
        if not name:
            continue

        result = dict(pdf_pole)

        # Если есть DXF-данные для этой опоры — берём координаты оттуда
        if name in dxf_index:
            dxf_pole = dxf_index[name]
            result["x"] = dxf_pole["x"]
            result["y"] = dxf_pole["y"]
            result["z"] = dxf_pole["z"]
            result["source"] = "pdf+dxf"

            if not result.get("height") and dxf_pole.get("height"):
                result["height"] = dxf_pole["height"]

        merged.append(result)
        seen.add(name)

    # Добавляем опоры из DXF, которых не было в PDF
    for dxf_pole in dxf_data:
        name = dxf_pole.get("name", "")
        if name and name not in seen:
            merged.append(dxf_pole)
            seen.add(name)

    # Сортировка по имени (числовая)
    merged.sort(key=lambda p: _sort_key(p.get("name", "")))
    return merged


def _sort_key(name: str) -> tuple[int, str]:
    """Ключ сортировки: числовая часть + буквенный суффикс."""
    match = re.match(r'(\d+)(.*)', name)
    if match:
        return (int(match.group(1)), match.group(2))
    return (999999, name)
