"""
Конфигурация приложения «ПКФ СНАРК — Исполнительная геодезическая документация».

Содержит:
- Настройки OpenRouter API (модели Gemini 2.5 Pro и DeepSeek-R1)
- Константы ГОСТ Р 51872-2024 (допуски вертикальности)
- Параметры листа, шрифтов, штампа
- Пути к директориям проекта
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "projects"
TEMPLATES_DIR = BASE_DIR / "templates"
ASSETS_DIR = BASE_DIR / "assets"


# ---------------------------------------------------------------------------
# OpenRouter API
# ---------------------------------------------------------------------------
@dataclass
class OpenRouterConfig:
    """Настройки доступа к OpenRouter API."""
    api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    base_url: str = "https://openrouter.ai/api/v1"
    model_pdf_parse: str = "google/gemini-2.5-pro-preview-03-25"
    model_code: str = "deepseek/deepseek-r1"
    max_tokens: int = 4096
    temperature: float = 0.1


# ---------------------------------------------------------------------------
# ГОСТ Р 51872-2024 — допуски вертикальности
# ---------------------------------------------------------------------------
@dataclass
class GOSTTolerances:
    """
    Допуски вертикальности по ГОСТ Р 51872-2024.

    Для стоек / колонн / опор контактной сети:
      - Относительное отклонение: L / 150 (где L — высота конструкции)
      - Абсолютные пороги задаются для типовых высот
    """
    relative_divisor: int = 150
    min_tolerance_mm: float = 10.0
    max_tolerance_mm: float = 120.0

    # Абсолютные допуски по типам опор (мм)
    absolute_tolerances: dict[str, float] = field(default_factory=lambda: {
        "ТФГ-1500-10": 50.0,
        "ТФГ-2300-10": 75.0,
        "СТ-110": 40.0,
        "DEFAULT": 60.0,
    })

    def get_tolerance(self, pole_type: str, pole_height_m: float) -> float:
        """Вычисляет допуск (мм) для конкретной опоры."""
        relative = (pole_height_m * 1000) / self.relative_divisor
        absolute = self.absolute_tolerances.get(
            pole_type, self.absolute_tolerances["DEFAULT"]
        )
        tolerance = max(relative, absolute)
        return max(self.min_tolerance_mm, min(tolerance, self.max_tolerance_mm))


# ---------------------------------------------------------------------------
# Параметры листа и штампа
# ---------------------------------------------------------------------------
@dataclass
class SheetConfig:
    """Размеры листа A3 и параметры оформления."""
    width_mm: float = 420.0
    height_mm: float = 297.0
    margin_mm: float = 5.0
    inner_margin_left_mm: float = 20.0
    inner_margin_mm: float = 5.0

    # Шрифты (для reportlab)
    font_family: str = "Helvetica"
    font_size_title: int = 14
    font_size_normal: int = 10
    font_size_small: int = 8
    font_size_table: int = 9


@dataclass
class StampConfig:
    """Данные штампа ПКФ СНАРК."""
    organization: str = 'ООО "ПКФ СНАРК"'
    city: str = "г. Москва"
    license_info: str = "СРО-Г-123456"
    chief_engineer: str = ""
    surveyor: str = ""
    checker: str = ""
    stage: str = "Р"
    document_type: str = "Исполнительная схема вертикальности опор"


# ---------------------------------------------------------------------------
# DXF-слои (из анализа шаблона)
# ---------------------------------------------------------------------------
@dataclass
class DXFLayers:
    """Имена слоёв DXF-шаблона для заполнения."""
    deviations_is: str = "ОТКЛОНЕНИЯ_ИС"
    pole_number: str = "0_Номер опоры"
    pole_center: str = "0_Центр опоры"
    pole_project: str = "0_Опора проект"
    height_project: str = "0_Высота проект"
    height_fact: str = "!_Высота факт"
    survey: str = "!Съемка"
    deviations: str = "отклонения"
    frame: str = "Общ-Рамка"
    text: str = "Общ-Текст"
    perpendicular: str = "перпендикуляр"

    # Слои DXF проекта (TPAMBAY4.dxf)
    project_symbols: str = "0_Point_Symbols"
    project_name: str = "0_Point_Name"
    project_height: str = "0_Point_Height"
    project_code: str = "0_Point_Code"


# ---------------------------------------------------------------------------
# Сборный конфиг
# ---------------------------------------------------------------------------
@dataclass
class AppConfig:
    """Главный конфигурационный объект приложения."""
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)
    gost: GOSTTolerances = field(default_factory=GOSTTolerances)
    sheet: SheetConfig = field(default_factory=SheetConfig)
    stamp: StampConfig = field(default_factory=StampConfig)
    dxf_layers: DXFLayers = field(default_factory=DXFLayers)

    # Порог привязки точек к опорам (м)
    match_radius_m: float = 2.0

    # Максимальное количество файлов замеров
    max_measurement_files: int = 10


def get_config() -> AppConfig:
    """Фабричный метод для получения конфигурации."""
    return AppConfig()
