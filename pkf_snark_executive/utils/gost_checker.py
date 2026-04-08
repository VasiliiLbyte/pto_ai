"""
Проверка допусков вертикальности по ГОСТ Р 51872-2024.

Таблица допусков для опор контактной сети, стоек, колонн.
Используется deviation_calculator.py для оценки статуса «Норма / Превышение».
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from config import GOSTTolerances


class DeviationStatus(Enum):
    """Статус отклонения."""
    OK = "Норма"
    WARNING = "Предупреждение"
    EXCEEDED = "Превышение"


@dataclass(frozen=True)
class ToleranceResult:
    """Результат проверки допуска."""
    tolerance_mm: float
    deviation_mm: float
    status: DeviationStatus
    ratio: float  # deviation / tolerance (0..1+ )

    @property
    def status_text(self) -> str:
        if self.status == DeviationStatus.OK:
            return f"Норма ({self.deviation_mm:.1f} / {self.tolerance_mm:.1f} мм)"
        elif self.status == DeviationStatus.WARNING:
            return (
                f"Предупреждение ({self.deviation_mm:.1f} / "
                f"{self.tolerance_mm:.1f} мм, {self.ratio:.0%})"
            )
        else:
            excess = self.deviation_mm - self.tolerance_mm
            return (
                f"Превышение на {excess:.1f} мм "
                f"({self.deviation_mm:.1f} / {self.tolerance_mm:.1f} мм)"
            )


WARNING_THRESHOLD = 0.8  # 80% от допуска — предупреждение


def check_tolerance(
    deviation_mm: float,
    pole_type: str,
    pole_height_m: float,
    gost: GOSTTolerances | None = None,
) -> ToleranceResult:
    """
    Проверяет отклонение на соответствие допуску ГОСТ Р 51872-2024.

    Args:
        deviation_mm: фактическое отклонение (мм)
        pole_type: тип опоры (напр. «ТФГ-2300-10»)
        pole_height_m: высота опоры (м)
        gost: объект допусков (если None — используются значения по умолчанию)

    Returns:
        ToleranceResult с результатом проверки
    """
    if gost is None:
        gost = GOSTTolerances()

    tolerance = gost.get_tolerance(pole_type, pole_height_m)
    ratio = deviation_mm / tolerance if tolerance > 0 else 0.0

    if deviation_mm <= tolerance * WARNING_THRESHOLD:
        status = DeviationStatus.OK
    elif deviation_mm <= tolerance:
        status = DeviationStatus.WARNING
    else:
        status = DeviationStatus.EXCEEDED

    return ToleranceResult(
        tolerance_mm=tolerance,
        deviation_mm=deviation_mm,
        status=status,
        ratio=ratio,
    )


def format_status_color(status: DeviationStatus) -> str:
    """CSS-цвет для статуса (используется в Streamlit)."""
    return {
        DeviationStatus.OK: "#28a745",
        DeviationStatus.WARNING: "#ffc107",
        DeviationStatus.EXCEEDED: "#dc3545",
    }[status]
