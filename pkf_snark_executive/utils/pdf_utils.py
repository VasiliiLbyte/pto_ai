"""
Утилиты для работы с PDF (pdfplumber) и OpenRouter API.

- Извлечение таблиц и текста из PDF
- Запросы к Gemini 2.5 Pro через OpenRouter для парсинга сложных таблиц
- Вырезка изображений разрезов/узлов из PDF
"""
from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any

import httpx
import pdfplumber
from PIL import Image

from config import OpenRouterConfig

logger = logging.getLogger(__name__)


def extract_tables_pdfplumber(
    pdf_path: str, pages: list[int] | None = None
) -> list[list[list[str | None]]]:
    """
    Извлекает таблицы из PDF через pdfplumber.

    Args:
        pdf_path: путь к PDF-файлу
        pages: номера страниц (0-based); None = все

    Returns:
        Список таблиц, каждая таблица — список строк, каждая строка — список ячеек.
    """
    all_tables: list[list[list[str | None]]] = []
    with pdfplumber.open(pdf_path) as pdf:
        target_pages = pages if pages is not None else range(len(pdf.pages))
        for page_idx in target_pages:
            if page_idx >= len(pdf.pages):
                continue
            page = pdf.pages[page_idx]
            tables = page.extract_tables()
            if tables:
                all_tables.extend(tables)
    return all_tables


def extract_text_by_area(
    pdf_path: str, page_idx: int, bbox: tuple[float, float, float, float]
) -> str:
    """
    Извлекает текст из прямоугольной области страницы.

    Args:
        pdf_path: путь к PDF
        page_idx: индекс страницы (0-based)
        bbox: (x0, top, x1, bottom) в единицах PDF (точки)

    Returns:
        Текст из указанной области.
    """
    with pdfplumber.open(pdf_path) as pdf:
        if page_idx >= len(pdf.pages):
            return ""
        page = pdf.pages[page_idx]
        cropped = page.within_bbox(bbox)
        return cropped.extract_text() or ""


def extract_page_text(pdf_path: str, page_idx: int) -> str:
    """Извлекает весь текст со страницы."""
    with pdfplumber.open(pdf_path) as pdf:
        if page_idx >= len(pdf.pages):
            return ""
        return pdf.pages[page_idx].extract_text() or ""


def get_pdf_page_count(pdf_path: str) -> int:
    """Возвращает количество страниц в PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def page_to_image(pdf_path: str, page_idx: int, resolution: int = 150) -> Image.Image:
    """Рендерит страницу PDF в изображение PIL."""
    with pdfplumber.open(pdf_path) as pdf:
        if page_idx >= len(pdf.pages):
            raise IndexError(f"Страница {page_idx} не найдена")
        page = pdf.pages[page_idx]
        return page.to_image(resolution=resolution).original


def image_to_base64(img: Image.Image, fmt: str = "PNG") -> str:
    """Конвертирует PIL Image в base64-строку."""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def query_openrouter(
    prompt: str,
    image_base64: str | None = None,
    config: OpenRouterConfig | None = None,
    model: str | None = None,
) -> str:
    """
    Отправляет запрос к OpenRouter API.

    Args:
        prompt: текстовый промпт
        image_base64: base64-изображение (опционально, для vision-моделей)
        config: настройки API
        model: модель (если None — берётся из config)

    Returns:
        Текстовый ответ модели.
    """
    if config is None:
        config = OpenRouterConfig()

    if not config.api_key:
        logger.warning("OpenRouter API key не задан — AI-парсинг недоступен")
        return ""

    target_model = model or config.model_pdf_parse

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_base64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{image_base64}"},
        })

    payload = {
        "model": target_model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
    }

    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://pkf-snark.ru",
        "X-Title": "PKF SNARK Executive Docs",
    }

    try:
        resp = httpx.post(
            f"{config.base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("Ошибка запроса OpenRouter: %s", e)
        return ""


def parse_table_with_ai(
    pdf_path: str,
    page_idx: int,
    prompt_template: str,
    config: OpenRouterConfig | None = None,
) -> list[dict[str, Any]]:
    """
    Парсит таблицу со страницы PDF через AI (Gemini 2.5 Pro).

    Рендерит страницу в изображение, отправляет на распознавание,
    ожидает JSON-массив с данными.

    Args:
        pdf_path: путь к PDF
        page_idx: номер страницы
        prompt_template: промпт с инструкциями по извлечению
        config: настройки API

    Returns:
        Список словарей с данными.
    """
    try:
        img = page_to_image(pdf_path, page_idx)
        b64 = image_to_base64(img)
        response = query_openrouter(prompt_template, image_base64=b64, config=config)
        if not response:
            return []

        # Извлекаем JSON из ответа (может быть обёрнут в ```json```)
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            json_lines = []
            inside = False
            for line in lines:
                if line.startswith("```") and not inside:
                    inside = True
                    continue
                if line.startswith("```") and inside:
                    break
                if inside:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        return json.loads(text)
    except (json.JSONDecodeError, Exception) as e:
        logger.error("Ошибка AI-парсинга таблицы: %s", e)
        return []
