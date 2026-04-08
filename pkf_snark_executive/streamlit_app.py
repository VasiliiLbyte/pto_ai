"""
ПКФ СНАРК — Исполнительная геодезическая документация.

Главное Streamlit-приложение: навигация, экраны, управление состоянием.
Запуск: streamlit run streamlit_app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Добавляем корень пакета в sys.path
_pkg_root = Path(__file__).resolve().parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

import io
import json
import re
import tempfile
import zipfile
from datetime import datetime
from time import perf_counter

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config import AppConfig, get_config, DATA_DIR, ASSETS_DIR

# ---------------------------------------------------------------------------
# Настройка страницы
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="ПКФ СНАРК — Исполнительная документация",
    page_icon="📐",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Инициализация состояния сессии
# ---------------------------------------------------------------------------
_DEFAULT_STATE = {
    "config": None,
    "current_step": "new_project",
    "project_name": "",
    "project_data": None,       # dict: poles, metadata
    "measurement_data": None,   # dict: points, matched
    "deviation_results": None,  # list[DeviationResult]
    "generated_files": None,    # bytes (ZIP)
    "file_stats": None,         # list[dict] — статистика по файлам замеров
    "last_project_processing_log": None,  # dict: status, elapsed_s, lines
}

for key, default in _DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = default

if st.session_state.config is None:
    st.session_state.config = get_config()


def get_cfg() -> AppConfig:
    return st.session_state.config


def _safe_project_slug(name: str) -> str:
    """Безопасное имя директории проекта."""
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = normalized.strip("._")
    return normalized or "project"


def _safe_upload_name(filename: str) -> str:
    """Безопасное имя загружаемого файла без traversal."""
    base_name = Path(filename).name
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base_name)
    cleaned = cleaned.strip()
    return cleaned or "uploaded_file"


# ---------------------------------------------------------------------------
# Пользовательские стили
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .main-header {
        font-size: 1.8rem;
        font-weight: 700;
        color: #1a365d;
        margin-bottom: 0.5rem;
    }
    .sub-header {
        font-size: 1.0rem;
        color: #4a5568;
        margin-bottom: 1.5rem;
    }
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 1.2rem;
        border-radius: 0.75rem;
        color: white;
        text-align: center;
    }
    .metric-card h3 { margin: 0; font-size: 2rem; }
    .metric-card p  { margin: 0; font-size: 0.85rem; opacity: 0.9; }
    .status-ok      { color: #28a745; font-weight: 600; }
    .status-warn    { color: #ffc107; font-weight: 600; }
    .status-fail    { color: #dc3545; font-weight: 600; }
    div[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #1a365d 0%, #2d3748 100%);
    }
    div[data-testid="stSidebar"] .stMarkdown { color: #e2e8f0; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Боковая панель
# ---------------------------------------------------------------------------
def render_sidebar():
    with st.sidebar:
        logo_path = ASSETS_DIR / "logo_snark.png"
        if logo_path.exists():
            st.image(str(logo_path), width=200)
        st.markdown("### 📐 ПКФ СНАРК")
        st.markdown("Исполнительная геодезическая документация")
        st.divider()

        all_options = [
            "new_project", "saved_projects",
            "project_overview", "measurements", "generation",
        ]
        labels = {
            "new_project": "🆕 Новый проект",
            "saved_projects": "📂 Загруженные проекты",
            "project_overview": "📊 Обзор проекта",
            "measurements": "📏 Замеры",
            "generation": "📄 Генерация",
        }
        current = st.session_state.current_step
        if current not in all_options:
            current = "new_project"

        nav = st.radio(
            "Навигация",
            options=all_options,
            format_func=lambda x: labels[x],
            index=all_options.index(current),
            key="nav_radio",
        )
        st.session_state.current_step = nav

        st.divider()

        # Настройки штампа
        with st.expander("⚙️ Настройки штампа"):
            stamp = get_cfg().stamp
            stamp.surveyor = st.text_input("Геодезист", value=stamp.surveyor, key="stamp_surv")
            stamp.checker = st.text_input("Проверил", value=stamp.checker, key="stamp_check")
            stamp.chief_engineer = st.text_input("ГИП", value=stamp.chief_engineer, key="stamp_gip")

        st.divider()

        # API-ключ
        api_key = st.text_input(
            "🔑 OpenRouter API Key",
            value=get_cfg().openrouter.api_key,
            type="password",
        )
        if api_key != get_cfg().openrouter.api_key:
            get_cfg().openrouter.api_key = api_key

        st.divider()
        st.caption(f"v1.0.0 • {datetime.now().strftime('%d.%m.%Y')}")
        st.caption("ГОСТ Р 51872-2024")


# ---------------------------------------------------------------------------
# Экран 1: Новый проект
# ---------------------------------------------------------------------------
def render_new_project():
    st.markdown('<div class="main-header">Новый проект</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-header">'
        "Загрузите PDF проекта и опционально DXF для точных координат"
        "</div>",
        unsafe_allow_html=True,
    )

    project_name = st.text_input(
        "Название проекта *",
        value=st.session_state.project_name,
        placeholder="Например: Трамвайная линия участок 1.2.1",
    )
    st.session_state.project_name = project_name

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### 📄 PDF проекта (обязательно)")
        pdf_file = st.file_uploader(
            "Загрузите PDF проекта",
            type=["pdf"],
            key="pdf_upload",
            help="Комплект КС/КЖ с ведомостями опор, планами, разрезами",
        )

    with col2:
        st.markdown("#### 📐 DXF проекта (опционально)")
        dxf_file = st.file_uploader(
            "Загрузите DXF проекта",
            type=["dxf"],
            key="dxf_upload",
            help="DXF-план с точными координатами опор",
        )
        if dxf_file is None:
            st.warning(
                "⚠️ DXF не загружен — точные координаты будут извлечены только из PDF. "
                "Рекомендуется загрузить DXF для лучшей точности."
            )

    st.divider()

    last_log = st.session_state.last_project_processing_log
    if last_log and last_log.get("project_name") == project_name.strip():
        st.markdown("#### 🧾 Последний лог обработки проекта")
        status = last_log.get("status", "UNKNOWN")
        elapsed_s = float(last_log.get("elapsed_s", 0.0))
        if status == "OK":
            st.success(f"Статус: {status} • Время: {elapsed_s:.1f} c")
        elif status == "FAILED":
            st.error(f"Статус: {status} • Время: {elapsed_s:.1f} c")
        else:
            st.info(f"Статус: {status} • Время: {elapsed_s:.1f} c")
        st.code("\n".join(last_log.get("lines", [])) or "Лог пуст")

    if st.button("🚀 Обработать проект", type="primary", use_container_width=True):
        if not project_name.strip():
            st.error("Укажите название проекта")
            return
        if pdf_file is None:
            st.error("Загрузите PDF проекта")
            return

        _process_project(project_name.strip(), pdf_file, dxf_file)


def _process_project(name: str, pdf_file, dxf_file):
    """Обработка загруженного проекта: парсинг PDF + DXF."""
    from project_parser import parse_pdf_project, parse_dxf_project, merge_project_data

    progress = st.progress(0, text="Подготовка обработки...")
    log_placeholder = st.empty()
    start_ts = perf_counter()
    log_lines: list[str] = []
    st.session_state.last_project_processing_log = None

    def elapsed_s() -> float:
        return perf_counter() - start_ts

    def log_step(step_text: str, pct: int) -> None:
        pct_clamped = max(0, min(100, int(pct)))
        line = f"[+{elapsed_s():.1f}s] [{pct_clamped:>3}%] {step_text}"
        log_lines.append(line)
        progress.progress(pct_clamped, text=step_text)
        log_placeholder.code("\n".join(log_lines))

    try:
        # Сохраняем файлы во временную директорию
        log_step("Подготовка директории проекта...", 5)
        project_dir = DATA_DIR / _safe_project_slug(name)
        project_dir.mkdir(parents=True, exist_ok=True)

        pdf_path = project_dir / "project.pdf"
        pdf_path.write_bytes(pdf_file.getvalue())
        log_step("PDF сохранён. Парсинг PDF...", 15)

        dxf_path = None
        if dxf_file is not None:
            dxf_path = project_dir / "project.dxf"
            dxf_path.write_bytes(dxf_file.getvalue())
            log_step("DXF сохранён. Парсинг PDF продолжается...", 20)

        # Парсинг PDF
        pdf_data = parse_pdf_project(str(pdf_path), get_cfg())
        log_step("PDF обработан.", 50)

        # Парсинг DXF
        dxf_data = None
        if dxf_path:
            log_step("Парсинг DXF...", 60)
            dxf_data = parse_dxf_project(str(dxf_path), get_cfg())
            log_step("DXF обработан.", 75)
        else:
            log_step("DXF не загружен. Этап парсинга DXF пропущен.", 75)

        # Объединение
        log_step("Объединение данных проекта...", 90)
        merged = merge_project_data(pdf_data, dxf_data)

        st.session_state.project_data = {
            "name": name,
            "poles": merged,
            "pdf_path": str(pdf_path),
            "dxf_path": str(dxf_path) if dxf_path else None,
            "project_dir": str(project_dir),
        }

        # Сохраняем метаданные
        meta = {
            "name": name,
            "created": datetime.now().isoformat(),
            "pole_count": len(merged),
            "has_dxf": dxf_path is not None,
        }
        (project_dir / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        log_step("Финализация завершена.", 100)
        total_elapsed = elapsed_s()
        log_lines[-1] = (
            f"[+{total_elapsed:.1f}s] [100%] "
            f"Финализация завершена (OK). Найдено опор: {len(merged)}"
        )
        log_placeholder.code("\n".join(log_lines))
        st.session_state.last_project_processing_log = {
            "status": "OK",
            "elapsed_s": total_elapsed,
            "project_name": name,
            "lines": log_lines,
        }

        if not merged:
            st.warning(
                "Опоры не найдены автоматически. Вы можете добавить их "
                "вручную на экране «Обзор проекта»."
            )

        st.session_state.current_step = "project_overview"
        st.rerun()

    except Exception as e:
        total_elapsed = elapsed_s()
        fail_line = f"[+{total_elapsed:.1f}s] [100%] FAILED: {e}"
        log_lines.append(fail_line)
        progress.progress(100, text="Обработка завершилась с ошибкой")
        log_placeholder.code("\n".join(log_lines))
        st.session_state.last_project_processing_log = {
            "status": "FAILED",
            "elapsed_s": total_elapsed,
            "project_name": name,
            "lines": log_lines,
        }
        st.error(f"Ошибка обработки проекта: {e}")
        import traceback
        st.expander("Подробности ошибки").code(traceback.format_exc())


# ---------------------------------------------------------------------------
# Экран: Загруженные проекты
# ---------------------------------------------------------------------------
def render_saved_projects():
    st.markdown(
        '<div class="main-header">Загруженные проекты</div>',
        unsafe_allow_html=True,
    )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    project_dirs = [
        d for d in DATA_DIR.iterdir()
        if d.is_dir() and (d / "metadata.json").exists()
    ]

    if not project_dirs:
        st.info("Нет сохранённых проектов. Создайте новый проект.")
        return

    for pdir in sorted(project_dirs):
        try:
            meta = json.loads((pdir / "metadata.json").read_text(encoding="utf-8"))
        except Exception:
            continue

        with st.container(border=True):
            col1, col2, col3 = st.columns([3, 2, 1])
            with col1:
                st.markdown(f"**{meta.get('name', pdir.name)}**")
                st.caption(f"Создан: {meta.get('created', '?')[:10]}")
            with col2:
                st.metric("Опор", meta.get("pole_count", "?"))
            with col3:
                if st.button("Открыть", key=f"open_{pdir.name}"):
                    _load_saved_project(pdir, meta)


def _load_saved_project(pdir: Path, meta: dict):
    """Загружает ранее сохранённый проект."""
    from project_parser import parse_pdf_project, parse_dxf_project, merge_project_data

    pdf_path = pdir / "project.pdf"
    dxf_path = pdir / "project.dxf"

    pdf_data = parse_pdf_project(str(pdf_path), get_cfg()) if pdf_path.exists() else []
    dxf_data = parse_dxf_project(str(dxf_path), get_cfg()) if dxf_path.exists() else None

    merged = merge_project_data(pdf_data, dxf_data)

    st.session_state.project_data = {
        "name": meta.get("name", pdir.name),
        "poles": merged,
        "pdf_path": str(pdf_path) if pdf_path.exists() else None,
        "dxf_path": str(dxf_path) if dxf_path.exists() else None,
        "project_dir": str(pdir),
    }
    st.session_state.last_project_processing_log = None
    st.session_state.project_name = meta.get("name", "")
    st.session_state.current_step = "project_overview"
    st.rerun()


# ---------------------------------------------------------------------------
# Экран 2: Обзор проекта
# ---------------------------------------------------------------------------
def render_project_overview():
    st.markdown('<div class="main-header">Обзор проекта</div>', unsafe_allow_html=True)

    pdata = st.session_state.project_data
    if pdata is None:
        st.info("Сначала загрузите и обработайте проект на экране «Новый проект».")
        return

    poles = pdata["poles"]
    st.markdown(
        f'<div class="sub-header">Проект: {pdata["name"]} • '
        f'Опор: {len(poles)}</div>',
        unsafe_allow_html=True,
    )

    # Карточки-метрики
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Опор найдено", len(poles))
    with col2:
        types = set(p.get("type", "—") for p in poles)
        st.metric("Типов опор", len(types))
    with col3:
        st.metric("DXF", "Загружен ✅" if pdata.get("dxf_path") else "Не загружен ⚠️")

    st.divider()

    # Интерактивная карта
    if poles:
        df = pd.DataFrame(poles)
        if "x" in df.columns and "y" in df.columns:
            st.subheader("📍 План расположения опор")
            fig = px.scatter(
                df,
                x="x",
                y="y",
                text="name",
                color="type" if "type" in df.columns else None,
                hover_data=["name", "type", "height"] if "height" in df.columns else ["name"],
                title="Расположение опор (проектные координаты)",
            )
            fig.update_traces(textposition="top center", marker=dict(size=10))
            fig.update_layout(
                xaxis_title="X (м)",
                yaxis_title="Y (м)",
                height=600,
                yaxis_scaleanchor="x",
            )
            st.plotly_chart(fig, use_container_width=True)

    # Редактируемая таблица
    st.subheader("📋 Таблица опор")
    if poles:
        df = pd.DataFrame(poles)
        edited = st.data_editor(
            df,
            use_container_width=True,
            num_rows="dynamic",
            key="poles_editor",
        )
        st.session_state.project_data["poles"] = edited.to_dict("records")

    st.divider()
    if st.button("📏 Перейти к загрузке замеров", type="primary"):
        st.session_state.current_step = "measurements"
        st.rerun()


# ---------------------------------------------------------------------------
# Экран 3: Загрузка геодезических данных
# ---------------------------------------------------------------------------
def render_measurements():
    st.markdown(
        '<div class="main-header">Загрузка геодезических данных</div>',
        unsafe_allow_html=True,
    )

    pdata = st.session_state.project_data
    if pdata is None:
        st.info("Сначала загрузите проект.")
        return

    st.markdown(
        '<div class="sub-header">'
        "Загрузите файлы замеров (TXT, DXF, XML) — до 10 файлов"
        "</div>",
        unsafe_allow_html=True,
    )

    files = st.file_uploader(
        "Файлы замеров",
        type=["txt", "dxf", "xml"],
        accept_multiple_files=True,
        key="measurement_upload",
    )

    if files and st.button("⚙️ Обработать замеры", type="primary"):
        _process_measurements(files, pdata)

    # Статистика файлов (если была обработка)
    if st.session_state.file_stats:
        st.subheader("📁 Загруженные файлы")
        st.dataframe(pd.DataFrame(st.session_state.file_stats), use_container_width=True)

    # Отображение результатов
    if st.session_state.deviation_results is not None:
        _render_deviation_table()

        st.divider()
        col_left, col_right = st.columns(2)
        with col_left:
            if st.button("📄 Перейти к генерации", type="primary", use_container_width=True):
                st.session_state.current_step = "generation"
                st.rerun()
        with col_right:
            if st.session_state.deviation_results:
                count_ok = sum(
                    1 for r in st.session_state.deviation_results if r.get("status") == "Норма"
                )
                count_fail = sum(
                    1 for r in st.session_state.deviation_results if r.get("status") == "Превышение"
                )
                st.success(f"Норма: {count_ok}  |  Превышение: {count_fail}")


def _process_measurements(files, pdata):
    """Обработка файлов замеров: парсинг, привязка, расчёт отклонений."""
    from measurement_parser import parse_measurement_file, match_points_to_poles
    from deviation_calculator import calculate_all_deviations

    cfg = get_cfg()
    poles = pdata["poles"]
    all_points = []
    max_files = max(1, int(cfg.max_measurement_files))
    uploaded_count = len(files)
    files = list(files[:max_files])
    if uploaded_count > max_files:
        st.warning(f"Будут обработаны первые {max_files} файлов из {uploaded_count}.")

    progress = st.progress(0, text="Парсинг файлов замеров...")
    total = len(files)

    file_stats = []
    for i, f in enumerate(files):
        progress.progress(
            int((i / total) * 50),
            text=f"Обработка {f.name} ({i + 1}/{total})...",
        )
        # Сохраняем файл временно
        project_dir = Path(pdata["project_dir"])
        meas_dir = project_dir / "measurements"
        meas_dir.mkdir(exist_ok=True)
        safe_name = _safe_upload_name(f.name)
        fpath = meas_dir / safe_name
        fpath.write_bytes(f.getvalue())

        points = parse_measurement_file(str(fpath))
        all_points.extend(points)
        file_stats.append({
            "Файл": safe_name,
            "Точек": len(points),
            "Опор распознано": len(set(p.get("pole_id", "") for p in points if p.get("pole_id"))),
        })

    # Статистика по файлам
    st.subheader("📁 Загруженные файлы")
    st.dataframe(pd.DataFrame(file_stats), use_container_width=True)

    progress.progress(60, text="Привязка точек к опорам...")

    # Привязка точек к опорам
    matched = match_points_to_poles(all_points, poles, cfg.match_radius_m)

    progress.progress(80, text="Расчёт отклонений...")

    # Расчёт отклонений
    results = calculate_all_deviations(matched, poles, cfg)

    progress.progress(100, text="Готово!")

    st.session_state.measurement_data = {
        "points": all_points,
        "matched": matched,
    }
    st.session_state.deviation_results = results
    st.session_state.file_stats = file_stats


def _render_deviation_table():
    """Отображает сводную таблицу отклонений с цветовой индикацией."""
    results = st.session_state.deviation_results
    if not results:
        st.warning("Нет данных об отклонениях.")
        return

    st.subheader("📊 Сводная таблица отклонений")

    df = pd.DataFrame(results)
    display_cols = {
        "pole_name": "№ опоры",
        "pole_type": "Тип",
        "height_project": "Высота проект (м)",
        "height_fact": "Высота факт (м)",
        "dx_mm": "ΔX (мм)",
        "dy_mm": "ΔY (мм)",
        "deviation_mm": "Отклонение (мм)",
        "angle_deg": "Угол (°)",
        "tolerance_mm": "Допуск (мм)",
        "status": "Статус",
    }
    available_cols = [c for c in display_cols if c in df.columns]
    df_display = df[available_cols].rename(columns=display_cols)

    styler = df_display.style
    status_subset = ["Статус"] if "Статус" in df_display.columns else []
    if status_subset:
        # pandas>=3: applymap removed on Styler; use map for elementwise styling.
        styler = styler.map(
            lambda v: (
                "background-color: #d4edda" if v == "Норма"
                else "background-color: #fff3cd" if v == "Предупреждение"
                else "background-color: #f8d7da" if v == "Превышение"
                else ""
            ),
            subset=status_subset,
        )

    st.dataframe(
        styler,
        use_container_width=True,
        height=500,
    )


# ---------------------------------------------------------------------------
# Экран 4: Генерация
# ---------------------------------------------------------------------------
def render_generation():
    st.markdown(
        '<div class="main-header">Генерация исполнительных листов</div>',
        unsafe_allow_html=True,
    )

    if st.session_state.deviation_results is None:
        st.info("Сначала загрузите замеры и выполните расчёт отклонений.")
        return

    results = st.session_state.deviation_results
    pdata = st.session_state.project_data
    cfg = get_cfg()

    st.markdown(
        f'<div class="sub-header">'
        f"Проект: {pdata['name']} • Опор с отклонениями: {len(results)}"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Выбор опор
    pole_names = [r["pole_name"] for r in results]
    selected = st.multiselect(
        "Выберите опоры для генерации",
        options=pole_names,
        default=pole_names,
        key="gen_poles_select",
    )

    # Загрузка шаблона DXF (опционально)
    template_dxf = st.file_uploader(
        "📐 DXF-шаблон (опционально — для DXF-выхода)",
        type=["dxf"],
        key="template_upload",
    )

    st.divider()

    col1, col2 = st.columns(2)
    with col1:
        generate_pdf = st.checkbox("📄 Генерировать PDF", value=True)
    with col2:
        generate_dxf = st.checkbox("📐 Генерировать DXF", value=False)

    col_gen, col_preview = st.columns([2, 1])
    with col_gen:
        if st.button("🚀 Создать исполнительные листы", type="primary", use_container_width=True):
            if not selected:
                st.error("Выберите хотя бы одну опору")
                return
            selected_results = [r for r in results if r["pole_name"] in selected]
            _generate_documents(selected_results, pdata, cfg, generate_pdf, generate_dxf, template_dxf)

    with col_preview:
        if st.button("👁️ Предпросмотр (первая опора)", use_container_width=True):
            if selected:
                _preview_single(results, selected[0], pdata, cfg)

    # Если уже есть сгенерированные файлы — показываем кнопку скачивания
    if st.session_state.generated_files is not None:
        st.divider()
        st.download_button(
            label="📥 Скачать ZIP-архив (повторно)",
            data=st.session_state.generated_files,
            file_name=f"ИС_{pdata['name'].replace(' ', '_')}_{datetime.now():%Y%m%d}.zip",
            mime="application/zip",
            use_container_width=True,
        )


def _generate_documents(results, pdata, cfg, gen_pdf, gen_dxf, template_dxf):
    """Генерация PDF и/или DXF для выбранных опор."""
    from pdf_exporter import generate_pole_pdf, generate_summary_excel, create_zip_archive

    try:
        progress = st.progress(0, text="Подготовка...")
        total = len(results)
        pdf_buffers = []
        dxf_buffers = []

        for i, result in enumerate(results):
            pole_name = result["pole_name"]
            progress.progress(
                int((i / total) * 90),
                text=f"Генерация листа {pole_name} ({i + 1}/{total})...",
            )

            if gen_pdf:
                pdf_buf = generate_pole_pdf(result, pdata, cfg)
                pdf_buffers.append((f"ИС_{pole_name}.pdf", pdf_buf))

            if gen_dxf and template_dxf is not None:
                from dxf_generator import generate_pole_dxf
                dxf_buf = generate_pole_dxf(result, pdata, cfg, template_dxf.getvalue())
                if dxf_buf:
                    dxf_buffers.append((f"ИС_{pole_name}.dxf", dxf_buf))

        progress.progress(92, text="Формирование сводного Excel...")
        excel_buf = generate_summary_excel(results, pdata)

        progress.progress(95, text="Создание ZIP-архива...")
        all_files = pdf_buffers + dxf_buffers + [("Сводная_таблица.xlsx", excel_buf)]
        zip_buf = create_zip_archive(all_files)

        progress.progress(100, text="Готово!")
        st.session_state.generated_files = zip_buf

        st.success(f"Создано {len(pdf_buffers)} PDF + {len(dxf_buffers)} DXF + 1 Excel")

        st.download_button(
            label="📥 Скачать ZIP-архив",
            data=zip_buf,
            file_name=f"ИС_{pdata['name'].replace(' ', '_')}_{datetime.now():%Y%m%d}.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True,
        )

    except Exception as e:
        st.error(f"Ошибка генерации: {e}")
        import traceback
        st.expander("Подробности ошибки").code(traceback.format_exc())


def _preview_single(results, pole_name, pdata, cfg):
    """Генерирует и показывает предпросмотр PDF для одной опоры."""
    from pdf_exporter import generate_pole_pdf
    import base64

    target = next((r for r in results if r["pole_name"] == pole_name), None)
    if target is None:
        st.error(f"Опора {pole_name} не найдена")
        return

    with st.spinner(f"Генерация предпросмотра {pole_name}..."):
        pdf_bytes = generate_pole_pdf(target, pdata, cfg)

    st.subheader(f"Предпросмотр: опора {pole_name}")
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    st.markdown(
        f'<iframe src="data:application/pdf;base64,{b64}" '
        f'width="100%" height="600" type="application/pdf"></iframe>',
        unsafe_allow_html=True,
    )

    st.download_button(
        label=f"📥 Скачать ИС_{pole_name}.pdf",
        data=pdf_bytes,
        file_name=f"ИС_{pole_name}.pdf",
        mime="application/pdf",
    )


# ---------------------------------------------------------------------------
# Маршрутизация
# ---------------------------------------------------------------------------
SCREENS = {
    "new_project": render_new_project,
    "saved_projects": render_saved_projects,
    "project_overview": render_project_overview,
    "measurements": render_measurements,
    "generation": render_generation,
}


def main():
    render_sidebar()
    screen_fn = SCREENS.get(st.session_state.current_step, render_new_project)
    screen_fn()


if __name__ == "__main__":
    main()
