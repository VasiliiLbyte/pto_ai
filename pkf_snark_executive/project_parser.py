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

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any

import ezdxf

from config import AppConfig, DATA_DIR, OpenRouterConfig, get_config
from utils.geometry import Point3D, distance_2d
from utils.pdf_utils import (
    extract_tables_pdfplumber,
    extract_page_text,
    get_pdf_page_count,
    image_to_base64,
    page_to_image,
    parse_table_with_ai,
    query_openrouter,
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


def parse_pdf_with_llm(pdf_path: str, project_id: str) -> dict[str, Any]:
    """
    Полноценный LLM-парсинг PDF с двухпроходной валидацией и сохранением артефактов.

    Сохраняет в директорию проекта:
    - parsed_data.json
    - poles.csv
    - raw_tables/*.json (сырые page-level ответы первого прохода)
    """
    project_dir = DATA_DIR / project_id
    raw_tables_dir = project_dir / "raw_tables"
    parsed_data_path = project_dir / "parsed_data.json"
    poles_csv_path = project_dir / "poles.csv"

    raw_tables_dir.mkdir(parents=True, exist_ok=True)

    cfg = OpenRouterConfig()
    app_cfg = get_config()
    result: dict[str, Any] = {
        "project_id": project_id,
        "pdf_path": str(pdf_path),
        "model": cfg.model_pdf_parse,
        "passes": {"extract": "failed", "validate": "skipped"},
        "warnings": [],
        "raw_pages": [],
        "poles": [],
        "foundations": [],
        "embedded_parts": [],
        "saved_files": {
            "parsed_data_json": str(parsed_data_path),
            "poles_csv": str(poles_csv_path),
            "raw_tables_dir": str(raw_tables_dir),
        },
    }

    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            if isinstance(value, str):
                value = value.strip().replace(",", ".")
            return float(value)
        except Exception:
            return default

    def _normalize_pole(item: dict[str, Any], source: str) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        raw_name = str(item.get("name", "") or "").strip()
        name = _clean_pole_name(raw_name)
        if not name:
            return None
        return {
            "name": name,
            "type": str(item.get("type", "") or "").strip(),
            "height": _safe_float(item.get("height"), 0.0),
            "x": _safe_float(item.get("x"), 0.0),
            "y": _safe_float(item.get("y"), 0.0),
            "z": _safe_float(item.get("z"), 0.0),
            "source": source,
            "foundation": str(item.get("foundation", "") or "").strip(),
            "embedded_parts": item.get("embedded_parts", []),
        }

    try:
        page_count = get_pdf_page_count(pdf_path)
        result["page_count"] = page_count
        if page_count <= 0:
            result["warnings"].append("PDF не содержит страниц.")
    except Exception as e:
        logger.exception("LLM парсинг: ошибка чтения PDF %s", e)
        result["warnings"].append(f"Ошибка чтения PDF: {e}")
        page_count = 0

    # Проход 1: подробное извлечение page-level структуры.
    extracted_items: list[dict[str, Any]] = []
    if cfg.api_key and page_count > 0:
        max_pages = max(1, int(getattr(app_cfg, "max_llm_pages", 12)))
        pages_to_scan = min(page_count, max_pages)
        if pages_to_scan < page_count:
            result["warnings"].append(
                f"LLM-парсинг ограничен первыми {pages_to_scan} из {page_count} страниц."
            )
        extract_prompt = (
            "Ты извлекаешь данные из проектного PDF по опорам контактной сети. "
            "Верни ТОЛЬКО JSON-объект со структурой: "
            "{\"page\": <int>, \"poles\": ["
            "{\"name\":\"\", \"type\":\"\", \"height\":0, \"x\":null, \"y\":null, "
            "\"foundation\":\"\", \"embedded_parts\":[]}"
            "], \"raw_tables\": [\"...\"], \"notes\": [\"...\"]}. "
            "Извлеки максимально полно: ведомости опор, типы, проектные высоты, "
            "фундаменты, координаты X/Y, закладные детали."
        )
        for page_idx in range(pages_to_scan):
            page_record: dict[str, Any] = {"page": page_idx, "ok": False, "error": ""}
            try:
                img = page_to_image(pdf_path, page_idx, resolution=160)
                page_b64 = image_to_base64(img)
                response_text = query_openrouter(
                    prompt=extract_prompt,
                    image_base64=page_b64,
                    config=cfg,
                    model=cfg.model_pdf_parse,
                )
                payload_text = response_text.strip()
                if payload_text.startswith("```"):
                    lines = payload_text.splitlines()
                    json_lines: list[str] = []
                    in_block = False
                    for line in lines:
                        if line.startswith("```") and not in_block:
                            in_block = True
                            continue
                        if line.startswith("```") and in_block:
                            break
                        if in_block:
                            json_lines.append(line)
                    payload_text = "\n".join(json_lines).strip()

                page_payload: dict[str, Any] = json.loads(payload_text) if payload_text else {}
                page_record["ok"] = True
                page_record["data"] = page_payload

                for item in page_payload.get("poles", []) or []:
                    normalized = _normalize_pole(item, source="llm_page_extract")
                    if normalized:
                        extracted_items.append(normalized)
            except Exception as e:
                page_record["error"] = str(e)
                result["warnings"].append(f"Страница {page_idx + 1}: {e}")
                logger.warning("LLM extract page %d failed: %s", page_idx + 1, e)
            finally:
                result["raw_pages"].append(page_record)
                raw_path = raw_tables_dir / f"page_{page_idx + 1:03d}.json"
                try:
                    raw_path.write_text(
                        json.dumps(page_record, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception as e:
                    logger.warning("Не удалось сохранить raw page %s: %s", raw_path, e)
        result["passes"]["extract"] = "ok"
    else:
        if not cfg.api_key:
            result["warnings"].append("OPENROUTER_API_KEY не задан. LLM-парсинг пропущен.")
        result["passes"]["extract"] = "skipped"

    # Проход 2: валидация и коррекция агрегированных данных.
    validated_poles: list[dict[str, Any]] = []
    if extracted_items and cfg.api_key:
        try:
            validation_prompt = (
                "Проведи валидацию данных по опорам. "
                "Нормализуй/дедуплицируй записи, исправь очевидные ошибки формата. "
                "Верни ТОЛЬКО JSON-массив объектов с полями: "
                "name,type,height,x,y,z,foundation,embedded_parts."
            )
            validation_payload = json.dumps(extracted_items, ensure_ascii=False)
            validation_response = query_openrouter(
                prompt=f"{validation_prompt}\n\nДАННЫЕ:\n{validation_payload}",
                image_base64=None,
                config=cfg,
                model=cfg.model_pdf_parse,
            )
            validation_text = validation_response.strip()
            if validation_text.startswith("```"):
                lines = validation_text.splitlines()
                json_lines: list[str] = []
                in_block = False
                for line in lines:
                    if line.startswith("```") and not in_block:
                        in_block = True
                        continue
                    if line.startswith("```") and in_block:
                        break
                    if in_block:
                        json_lines.append(line)
                validation_text = "\n".join(json_lines).strip()

            payload = json.loads(validation_text) if validation_text else []
            if isinstance(payload, list):
                for item in payload:
                    normalized = _normalize_pole(item, source="llm_validated")
                    if normalized:
                        validated_poles.append(normalized)
            result["passes"]["validate"] = "ok"
        except Exception as e:
            logger.warning("LLM validation failed: %s", e)
            result["warnings"].append(f"Валидация LLM не выполнена: {e}")
            result["passes"]["validate"] = "failed"

    final_poles = validated_poles or extracted_items

    # Дедупликация по имени с приоритетом более заполненной записи.
    by_name: dict[str, dict[str, Any]] = {}
    for pole in final_poles:
        name = pole["name"]
        prev = by_name.get(name)
        if prev is None:
            by_name[name] = pole
            continue
        prev_score = int(bool(prev.get("type"))) + int(bool(prev.get("height"))) + int(bool(prev.get("x") or prev.get("y")))
        cur_score = int(bool(pole.get("type"))) + int(bool(pole.get("height"))) + int(bool(pole.get("x") or pole.get("y")))
        if cur_score >= prev_score:
            by_name[name] = pole

    result["poles"] = sorted(by_name.values(), key=lambda p: _sort_key(p.get("name", "")))
    result["foundations"] = [
        {"name": p["name"], "foundation": p.get("foundation", "")}
        for p in result["poles"]
        if p.get("foundation")
    ]
    result["embedded_parts"] = [
        {"name": p["name"], "embedded_parts": p.get("embedded_parts", [])}
        for p in result["poles"]
        if p.get("embedded_parts")
    ]

    # Сохранение итоговых артефактов.
    try:
        parsed_data_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Не удалось сохранить parsed_data.json: %s", e)
        result["warnings"].append(f"Не удалось сохранить parsed_data.json: {e}")

    try:
        with poles_csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["name", "type", "height", "x", "y", "z", "source", "foundation", "embedded_parts"],
            )
            writer.writeheader()
            for pole in result["poles"]:
                writer.writerow({
                    "name": pole.get("name", ""),
                    "type": pole.get("type", ""),
                    "height": pole.get("height", 0.0),
                    "x": pole.get("x", 0.0),
                    "y": pole.get("y", 0.0),
                    "z": pole.get("z", 0.0),
                    "source": pole.get("source", ""),
                    "foundation": pole.get("foundation", ""),
                    "embedded_parts": json.dumps(pole.get("embedded_parts", []), ensure_ascii=False),
                })
    except Exception as e:
        logger.warning("Не удалось сохранить poles.csv: %s", e)
        result["warnings"].append(f"Не удалось сохранить poles.csv: {e}")

    return result


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
