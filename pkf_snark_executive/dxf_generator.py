"""
Генератор DXF исполнительных листов по шаблону.

Загружает DXF-шаблон, клонирует его и заполняет данными для каждой опоры:
- Заменяет текстовые плейсхолдеры (номер, тип, координаты)
- Рисует красную линию отклонения
- Заполняет таблицу и штамп

Слои по анализу шаблона:
- ОТКЛОНЕНИЯ_ИС: основная графика ИС
- 0_Номер опоры: метки опор
- отклонения: линии/размеры отклонений
"""
from __future__ import annotations

import io
import logging
import math
import tempfile
from datetime import datetime
from typing import Any

import ezdxf
from ezdxf.enums import TextEntityAlignment

from config import AppConfig

logger = logging.getLogger(__name__)


def generate_pole_dxf(
    result: dict[str, Any],
    pdata: dict[str, Any],
    cfg: AppConfig,
    template_bytes: bytes,
) -> bytes | None:
    """
    Генерирует DXF для одной опоры на основе шаблона.

    Args:
        result: данные отклонения опоры
        pdata: проектные данные
        cfg: конфигурация
        template_bytes: содержимое DXF-шаблона в байтах

    Returns:
        bytes DXF-файла или None при ошибке.
    """
    try:
        # Сохраняем шаблон во временный файл и читаем
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            tmp.write(template_bytes)
            tmp_path = tmp.name

        doc = ezdxf.readfile(tmp_path)
    except Exception as e:
        logger.error("Ошибка загрузки DXF-шаблона: %s", e)
        return None

    try:
        _fill_template(doc, result, pdata, cfg)
    except Exception as e:
        logger.error("Ошибка заполнения шаблона: %s", e)
        return None

    # Сохраняем в буфер
    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp_out:
            doc.saveas(tmp_out.name)
            with open(tmp_out.name, "rb") as f:
                return f.read()
    except Exception as e:
        logger.error("Ошибка сохранения DXF: %s", e)
        return None


def _fill_template(
    doc: ezdxf.document.Drawing,
    result: dict[str, Any],
    pdata: dict[str, Any],
    cfg: AppConfig,
):
    """Заполняет DXF-шаблон данными одной опоры."""
    layers = cfg.dxf_layers
    msp = doc.modelspace()

    # Замена текстовых плейсхолдеров
    _replace_texts(doc, result, cfg)

    # Обновление атрибутов блоков
    _update_block_attributes(doc, result)

    # Добавление линии отклонения
    _draw_deviation_line(msp, result, layers)


def _replace_texts(
    doc: ezdxf.document.Drawing,
    result: dict[str, Any],
    cfg: AppConfig,
):
    """
    Заменяет текстовые плейсхолдеры в шаблоне.

    Ищет TEXT/MTEXT на ключевых слоях и заменяет содержимое.
    """
    replacements = {
        "опора ТФГ-1500-10": f"Опора {result.get('pole_type', '')} № {result['pole_name']}",
        "Опора ТФГ-1500-10.": f"Опора {result.get('pole_type', '')} № {result['pole_name']}",
        "10000": f"{result.get('height_project', 0) * 1000:.0f}",
    }

    target_layers = {
        cfg.dxf_layers.deviations_is,
        cfg.dxf_layers.deviations,
        cfg.dxf_layers.text,
        "PDF _Текст",
        "Слои Цивил",
    }

    # TEXT entities
    for entity in doc.modelspace():
        if entity.dxftype() == "TEXT":
            layer = entity.dxf.layer
            if layer in target_layers:
                text = entity.dxf.text
                for old, new in replacements.items():
                    if old in text:
                        entity.dxf.text = text.replace(old, new)

        elif entity.dxftype() == "MTEXT":
            layer = entity.dxf.layer
            if layer in target_layers:
                text = entity.text
                for old, new in replacements.items():
                    if old in text:
                        entity.text = text.replace(old, new)

    # Paper space
    for layout in doc.layouts:
        if layout.name == "Model":
            continue
        for entity in layout:
            if entity.dxftype() == "TEXT":
                text = entity.dxf.text
                for old, new in replacements.items():
                    if old in text:
                        entity.dxf.text = text.replace(old, new)
            elif entity.dxftype() == "MTEXT":
                text = entity.text
                for old, new in replacements.items():
                    if old in text:
                        entity.text = text.replace(old, new)


def _update_block_attributes(doc: ezdxf.document.Drawing, result: dict[str, Any]):
    """Обновляет атрибуты именованных блоков (OTMET_TR, Опора КС Депо)."""
    msp = doc.modelspace()

    for entity in msp:
        if entity.dxftype() != "INSERT":
            continue

        # OTMET_TR — отметка высоты
        if entity.dxf.name in ("OTMET_TR", "*U39"):
            for attrib in entity.attribs:
                if attrib.dxf.tag == "OTM":
                    attrib.dxf.text = f"{result.get('height_fact', 0):.3f}"
                elif attrib.dxf.tag == "ОП.У.7":
                    attrib.dxf.text = f"№{result['pole_name']}"


def _draw_deviation_line(msp, result: dict[str, Any], layers):
    """
    Добавляет красную линию отклонения в model space.

    Линия идёт от проектного положения к фактическому верхнему сечению,
    масштабированная для наглядности.
    """
    dx_mm = result.get("dx_mm", 0)
    dy_mm = result.get("dy_mm", 0)
    deviation_mm = result.get("deviation_mm", 0)

    if deviation_mm < 0.1:
        return

    # Базовая точка (проектный центр опоры)
    x_proj = result.get("x_project", 0)
    y_proj = result.get("y_project", 0)

    if not x_proj or not y_proj:
        return

    # Линия отклонения (в метрах — масштаб чертежа)
    dx_m = dx_mm / 1000.0
    dy_m = dy_mm / 1000.0

    # Масштабируем для наглядности (×50)
    visual_scale = 50.0
    end_x = x_proj + dx_m * visual_scale
    end_y = y_proj + dy_m * visual_scale

    try:
        # Убеждаемся, что слой существует
        if layers.deviations not in [l.dxf.name for l in msp.doc.layers]:
            msp.doc.layers.add(layers.deviations, color=1)

        line = msp.add_line(
            (x_proj, y_proj),
            (end_x, end_y),
            dxfattribs={
                "layer": layers.deviations,
                "color": 1,  # красный (ACI)
                "lineweight": 50,
            },
        )

        # Текст отклонения рядом с линией
        text_x = (x_proj + end_x) / 2 + 0.5
        text_y = (y_proj + end_y) / 2 + 0.5

        msp.add_text(
            f"{deviation_mm:.1f}",
            dxfattribs={
                "layer": layers.deviations,
                "color": 1,
                "height": 0.8,
                "style": "Standard",
            },
        ).set_placement((text_x, text_y))

    except Exception as e:
        logger.error("Ошибка добавления линии отклонения: %s", e)
