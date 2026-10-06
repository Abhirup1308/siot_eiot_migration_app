from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
import streamlit as st

from src.config import Settings
from src.db import MigrationDB
from src.migration import MigrationService, parse_excel_technical_objects, parse_technical_objects
from src.ui import (
    activity_timeline,
    app_header,
    empty_state,
    format_bytes,
    friendly_extraction,
    friendly_transform,
    friendly_upload,
    inject_theme,
    metric_card,
    phase_header,
    progress_panel,
    run_summary_banner,
    stage_strip,
)


APP_VERSION = "5.0"

st.set_page_config(
    page_title="SIoT → EIoT Migration Studio",
    page_icon="↗",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_theme()


@st.cache_resource
def get_context():
    settings = Settings.from_env()
    settings.ensure_directories()
    db = MigrationDB(settings.db_path)
    service = MigrationService(settings, db)
    return settings, db, service


settings, db, service = get_context()


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------
def progress_callback(label: str):
    bar = st.progress(0.0, text=label)

    def update(current: int, total: int, message: str):
        bar.progress(min(current / max(total, 1), 1.0), text=message)

    return bar, update


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8-sig")


def _xlsx_bytes(frame: pd.DataFrame, sheet_name: str = "Report") -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name=sheet_name[:31])
        sheet = writer.sheets[sheet_name[:31]]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column_cells in sheet.columns:
            length = max(len(str(cell.value or "")) for cell in column_cells)
            sheet.column_dimensions[column_cells[0].column_letter].width = min(max(length + 2, 12), 55)
    return buffer.getvalue()


def dataframe_download(
    label: str,
    frame: pd.DataFrame,
    filename: str,
    key: str,
    *,
    xlsx: bool = False,
    primary: bool = False,
    use_container_width: bool = True,
):
    if xlsx:
        data = _xlsx_bytes(frame, "Migration Report")
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        data = _csv_bytes(frame)
        mime = "text/csv"
    st.download_button(
        label,
        data=data,
        file_name=filename,
        mime=mime,
        key=key,
        type="primary" if primary else "secondary",
        use_container_width=use_container_width,
    )


def report_button(label: str, path: Path, key: str, *, primary: bool = False):
    if not path.exists():
        st.button(label, key=key, disabled=True, use_container_width=True)
        return
    suffix = path.suffix.lower()
    mime = {
        ".json": "application/json",
        ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".parquet": "application/octet-stream",
    }.get(suffix, "application/octet-stream")
    st.download_button(
        label,
        data=path.read_bytes(),
        file_name=path.name,
        mime=mime,
        key=key,
        type="primary" if primary else "secondary",
        use_container_width=True,
    )


def show_file(path: Path, max_rows: int = 250, columns: list[str] | None = None):
    if not path.exists():
        st.info("This report has not been created yet.")
        return
    if path.suffix.lower() == ".json":
        st.json(json.loads(path.read_text(encoding="utf-8")))
        return
    try:
        frame = pd.read_csv(path, nrows=max_rows)
    except pd.errors.EmptyDataError:
        st.info("This report is empty.")
        return
    if columns:
        visible = [c for c in columns if c in frame.columns]
        if visible:
            frame = frame[visible]
    if frame.empty:
        st.info("This report is empty.")
    else:
        st.dataframe(frame, use_container_width=True, hide_index=True)
        if len(frame) >= max_rows:
            st.caption(f"Showing the first {max_rows:,} rows. Download the report for the complete file.")


def selectable_editor(
    frame: pd.DataFrame,
    *,
    id_col: str,
    key: str,
    eligible_ids: Iterable[int] | None = None,
    height: int = 340,
    disabled_columns: list[str] | None = None,
) -> list[int]:
    """All actionable rows are selected by default; users can deselect any row."""
    if frame.empty:
        return []
    frame = frame.copy()
    all_ids = [int(v) for v in frame[id_col].tolist()]
    eligible = set(all_ids if eligible_ids is None else [int(v) for v in eligible_ids])
    state_key = f"{key}__selected"
    known_key = f"{key}__known"
    version_key = f"{key}__version"
    selected = set(int(v) for v in st.session_state.get(state_key, []))
    known = set(int(v) for v in st.session_state.get(known_key, []))
    selected = (selected & eligible) | (eligible - known)
    st.session_state[state_key] = list(selected)
    st.session_state[known_key] = list(set(all_ids))
    st.session_state.setdefault(version_key, 0)

    c1, c2, spacer, c3 = st.columns([1, 1, 3, 2])
    if c1.button("Select all", key=f"{key}_all", use_container_width=True):
        selected = set(eligible)
        st.session_state[state_key] = list(selected)
        st.session_state[version_key] += 1
    if c2.button("Clear selection", key=f"{key}_none", use_container_width=True):
        selected = set()
        st.session_state[state_key] = []
        st.session_state[version_key] += 1
    with c3:
        st.caption(f"{len(selected):,} of {len(eligible):,} actionable rows selected")

    display = frame.set_index(id_col)
    display.insert(0, "Select", [idx in selected and idx in eligible for idx in display.index])
    disabled = disabled_columns or [c for c in display.columns if c != "Select"]
    edited = st.data_editor(
        display,
        key=f"{key}_editor_{st.session_state[version_key]}",
        hide_index=True,
        use_container_width=True,
        height=height,
        disabled=disabled,
        column_config={
            "Select": st.column_config.CheckboxColumn(
                "Select",
                help="Only selected actionable rows are used by the action buttons below.",
            ),
        },
    )
    selected_now = {
        int(idx)
        for idx, flag in edited["Select"].items()
        if bool(flag) and int(idx) in eligible
    }
    st.session_state[state_key] = list(selected_now)
    return sorted(selected_now)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _format_time(value: Any) -> str:
    dt = _parse_dt(value)
    if not dt:
        return "—"
    return dt.astimezone().strftime("%d %b %H:%M:%S")


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"~{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"~{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"~{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"~{days}d {hours}h"


def estimate_eta(run_id: str, stage: str, completed: int, total: int) -> str:
    if total <= 0:
        return "—"
    if completed >= total:
        return "Complete"
    if completed <= 0:
        return "Calculating after first completion"
    events = db.list_events(run_id, 500)
    relevant = [_parse_dt(e.get("created_at")) for e in events if e.get("stage") == stage]
    relevant = [v for v in relevant if v]
    run = db.get_run(run_id)
    start = min(relevant) if relevant else _parse_dt(run.get("created_at") if run else None)
    if not start:
        return "Calculating"
    elapsed = (datetime.now(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
    if elapsed <= 0:
        return "Calculating"
    seconds_per_item = elapsed / max(completed, 1)
    return _format_duration(seconds_per_item * max(total - completed, 0))


def latest_timestamp(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> str:
    values: list[datetime] = []
    for row in rows:
        for field in fields:
            dt = _parse_dt(row.get(field))
            if dt:
                values.append(dt)
    return _format_time(max(values) if values else None)


def split_status_frame(
    frame: pd.DataFrame,
    *,
    status_col: str,
    complete_values: set[str],
    attention_values: set[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if frame.empty:
        return frame.copy(), frame.copy(), frame.copy()
    attention_values = attention_values or set()
    done = frame[frame[status_col].isin(complete_values)].copy()
    attention = frame[frame[status_col].isin(attention_values)].copy()
    pending = frame[~frame[status_col].isin(complete_values | attention_values)].copy()
    return done, pending, attention


def status_downloads(
    title: str,
    all_frame: pd.DataFrame,
    done: pd.DataFrame,
    pending: pd.DataFrame,
    attention: pd.DataFrame,
    *,
    run_id: str,
    phase: str,
):
    st.caption(title)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        dataframe_download(
            f"Download completed ({len(done)})",
            done,
            f"{run_id}_{phase}_completed.csv",
            f"{phase}_done_{run_id}",
        )
    with c2:
        dataframe_download(
            f"Download pending ({len(pending)})",
            pending,
            f"{run_id}_{phase}_pending.csv",
            f"{phase}_pending_{run_id}",
        )
    with c3:
        dataframe_download(
            f"Download attention ({len(attention)})",
            attention,
            f"{run_id}_{phase}_attention.csv",
            f"{phase}_attention_{run_id}",
        )
    with c4:
        dataframe_download(
            f"Download all ({len(all_frame)})",
            all_frame,
            f"{run_id}_{phase}_all.csv",
            f"{phase}_all_{run_id}",
        )


def phase_progress_map(run_id: str) -> dict[str, float]:
    run = db.get_run(run_id)
    if not run:
        return {}
    dm = service.discovery_metrics(run_id)
    scope_pct = (dm["success"] / dm["total"]) if dm["total"] else 0.0

    em = service.extraction_metrics(run_id)
    extraction_pct = (em["complete"] / em["total"]) if em["total"] else 0.0

    tm = service.transform_metrics(run_id)
    transform_good = tm["success"] + tm["no_data"]
    transform_pct = (transform_good / tm["total"]) if tm["total"] else 0.0

    validation_uploads = db.list_uploads(run_id, "VALIDATION")
    if run.get("customer_approved"):
        validation_pct = 1.0
    elif validation_uploads:
        status = validation_uploads[-1].get("status")
        validation_pct = 0.85 if status == "Processed" else 0.55 if status not in {"READY", "FAILED", "Processing Failed"} else 0.30
    else:
        validation_pct = 0.0

    lm = service.load_metrics(run_id)
    production_pct = (lm["processed"] / lm["total"]) if lm["total"] else 0.0
    return {
        "Scope": scope_pct,
        "Extraction": extraction_pct,
        "Transform": transform_pct,
        "Validation": validation_pct,
        "Production": production_pct,
    }


def overall_progress(run_id: str) -> float:
    values = list(phase_progress_map(run_id).values())
    return sum(values) / len(values) if values else 0.0


def run_snapshot(run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id) or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run": {
            "id": run_id,
            "created_at": run.get("created_at"),
            "technical_objects": run.get("technical_objects"),
            "date_from": run.get("date_from"),
            "date_to": run.get("date_to"),
            "paused": run.get("paused"),
            "discovery_status": run.get("discovery_status"),
            "extraction_status": run.get("extraction_status"),
            "transform_status": run.get("transform_status"),
            "validation_status": run.get("validation_status"),
            "load_status": run.get("load_status"),
            "customer_approved": run.get("customer_approved"),
            "production_approved": run.get("production_approved"),
        },
        "progress": phase_progress_map(run_id),
        "extraction": service.extraction_metrics(run_id),
        "transform": service.transform_metrics(run_id),
        "load": service.load_metrics(run_id),
        "disk_usage_bytes": service.disk_usage_bytes(run_id),
    }


# -----------------------------------------------------------------------------
# Background monitor / dialogs
# -----------------------------------------------------------------------------
@st.fragment(run_every=f"{settings.auto_poll_seconds}s")
def background_monitor(run_id: str, enabled: bool):
    if not enabled:
        return
    current = db.get_run(run_id)
    if not current or current.get("paused"):
        return
    before = (
        current.get("extraction_status"),
        current.get("validation_status"),
        current.get("load_status"),
        tuple((j["id"], j["status"]) for j in db.list_extraction_jobs(run_id)),
        tuple((u["id"], u["status"]) for u in db.list_uploads(run_id)),
    )
    try:
        if current.get("extraction_status") not in {"NOT_STARTED", "COMPLETE"}:
            service.advance_extraction_cycle(run_id)
        validation = db.list_uploads(run_id, "VALIDATION")
        if any(u.get("file_id") and u["status"] not in {"Processed", "Processing Failed"} for u in validation):
            service.refresh_upload_statuses(run_id, "VALIDATION")
        production = db.list_uploads(run_id, "PRODUCTION")
        if any(u.get("file_id") and u["status"] not in {"Processed", "Processing Failed"} for u in production):
            service.refresh_upload_statuses(run_id, "PRODUCTION")
    except Exception:
        return
    updated = db.get_run(run_id)
    after = (
        updated.get("extraction_status") if updated else None,
        updated.get("validation_status") if updated else None,
        updated.get("load_status") if updated else None,
        tuple((j["id"], j["status"]) for j in db.list_extraction_jobs(run_id)),
        tuple((u["id"], u["status"]) for u in db.list_uploads(run_id)),
    )
    if after != before:
        st.rerun()


@st.dialog("Abort and delete migration run")
def delete_run_dialog(run_id: str):
    st.markdown(
        "This permanently removes the local run, its SQLite records and its local extraction, transformation, "
        "validation and upload-preparation files."
    )
    st.warning("This cannot undo Cold Store requests already submitted to SAP or data/files already submitted to EIoT.")
    confirmation = st.text_input("Type DELETE to confirm", key=f"delete_confirm_{run_id}")
    if st.button("Delete this local run", type="primary", disabled=confirmation != "DELETE", use_container_width=True):
        service.abort_and_delete_run(run_id)
        for key in list(st.session_state):
            if run_id in key:
                del st.session_state[key]
        st.session_state.pop("selected_run_id", None)
        st.rerun()


@st.dialog("Create migration", width="large")
def new_migration_dialog():
    st.markdown(
        '<div class="modal-intro"><strong>Define the migration scope</strong>'
        '<span>Choose the source objects and history period. Discovery, position de-duplication, adaptive extraction slicing and checkpoints are handled automatically.</span></div>',
        unsafe_allow_html=True,
    )

    mode = st.radio(
        "Input method",
        ["Paste Technical Objects", "Upload Excel"],
        horizontal=True,
        help="Use the customer workbook for larger migrations; manual entry is convenient for testing or smaller scopes.",
    )
    technical_objects: list[str] = []
    source_name: str | None = None
    input_mode = "MANUAL"

    left, right = st.columns([1.35, 1], gap="large")
    with left:
        with st.container(border=True):
            st.markdown("### 1 · Migration scope")
            st.caption("All discovered rows remain user-selectable before extraction and later phases.")
            if mode == "Paste Technical Objects":
                raw_tos = st.text_area(
                    "Technical Objects",
                    placeholder="One Technical Object per line\n\n10270976\n10270814\n3161-04-008-01-02",
                    height=190,
                    key="dialog_tos",
                )
                technical_objects = parse_technical_objects(raw_tos)
            else:
                uploaded = st.file_uploader(
                    "Customer migration workbook",
                    type=["xlsx", "xlsm", "xls"],
                    key="dialog_excel",
                )
                if uploaded is not None:
                    try:
                        technical_objects, excel_summary = parse_excel_technical_objects(uploaded.getvalue())
                        source_name = uploaded.name
                        input_mode = "EXCEL"
                        st.success(
                            f"{excel_summary['unique_technical_objects']:,} unique TOs found from "
                            f"{excel_summary['rows_with_to']:,} workbook rows."
                        )
                    except Exception as exc:
                        st.error(str(exc))

            if technical_objects:
                st.caption(f"{len(technical_objects):,} unique Technical Object(s) selected")
                preview = pd.DataFrame({"Technical Object": technical_objects[:200]})
                st.dataframe(preview, use_container_width=True, hide_index=True, height=180)
                if len(technical_objects) > 200:
                    st.caption(f"Showing first 200 of {len(technical_objects):,} TOs.")
            else:
                st.info("Add at least one Technical Object to continue.")

    with right:
        with st.container(border=True):
            st.markdown("### 2 · Migration period")
            st.caption("The requested period is automatically divided into safe Cold Store slices.")
            from_date = st.date_input("From", value=date.today() - timedelta(days=90), key="dialog_from")
            to_date = st.date_input("To", value=date.today(), key="dialog_to")
            days = (to_date - from_date).days + 1 if to_date >= from_date else 0

            m1, m2 = st.columns(2)
            with m1:
                st.metric("History", f"{days:,} days" if days > 0 else "Invalid")
            with m2:
                st.metric("Objects", f"{len(technical_objects):,}")

            if technical_objects and days > 0:
                large = len(technical_objects) >= settings.large_scope_to_count or days >= settings.large_scope_days
                if large:
                    st.warning("Large migration scope · checkpointed and batched execution will be used.")
                else:
                    st.success("Standard migration scope")

    overlaps = service.find_overlapping_runs(technical_objects, from_date, to_date) if technical_objects and days > 0 else []
    allow_overlap = False
    if overlaps:
        overlap_frame = pd.DataFrame([
            {
                "Previous run": o["run_id"],
                "Overlapping TOs": o["technical_object_count"],
                "Overlap from": o["overlap_from"],
                "Overlap to": o["overlap_to"],
                "Previous production status": o["existing_load_status"],
            }
            for o in overlaps
        ])
        st.warning(
            "Overlapping migration detected. Accidental duplicate migration is blocked by default. "
            "Intentional re-migration requires explicit confirmation."
        )
        st.dataframe(overlap_frame, use_container_width=True, hide_index=True)
        allow_overlap = st.checkbox(
            "I understand this scope overlaps previous local migrations and I intentionally want to re-migrate it.",
            key="dialog_allow_overlap",
        )
        st.caption(
            "Within a run, duplicate target keys are protected. The final overwrite/upsert behavior for an existing EIoT key is controlled by EIoT."
        )

    large_scope = bool(
        technical_objects
        and days > 0
        and (
            len(technical_objects) >= settings.large_scope_to_count
            or days >= settings.large_scope_days
        )
    )
    large_ack = True
    if large_scope:
        large_ack = st.checkbox(
            "I understand this is a large migration and want to continue with checkpointed, batched processing.",
            key="dialog_large_ack",
        )

    can_create = bool(
        technical_objects
        and days > 0
        and not settings.missing_required()
        and large_ack
        and (not overlaps or allow_overlap)
    )

    st.markdown("---")
    b1, b2 = st.columns([2.2, 1])
    with b1:
        if technical_objects and days > 0:
            st.caption(
                f"Ready to create · {len(technical_objects):,} TOs · {days:,} days · "
                "discovery begins as a resumable checkpointed phase."
            )
    with b2:
        if st.button(
            "Create migration →",
            type="primary",
            disabled=not can_create,
            use_container_width=True,
        ):
            try:
                run_id = service.create_run(
                    technical_objects,
                    from_date,
                    to_date,
                    input_mode=input_mode,
                    source_name=source_name,
                    allow_overlap=allow_overlap,
                )
                st.session_state["selected_run_id"] = run_id
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


# -----------------------------------------------------------------------------
# Sidebar
# -----------------------------------------------------------------------------
missing = settings.missing_required()
runs = db.list_runs()

with st.sidebar:
    st.markdown(
        '<div class="sidebar-kicker">Migration control</div>'
        '<div class="sidebar-brand">Migration Studio</div>'
        f'<div class="sidebar-subtitle">SIoT → EIoT · Version {APP_VERSION}</div>',
        unsafe_allow_html=True,
    )
    if missing:
        st.error("Configuration needs attention")
        with st.expander("Missing settings"):
            for item in missing:
                st.write(f"• {item}")
    else:
        st.markdown('<div class="connection-ok">Systems configured</div>', unsafe_allow_html=True)

    st.markdown("---")
    if st.button("＋ New migration", type="primary", use_container_width=True):
        new_migration_dialog()

    if runs:
        labels = {
            r["id"]: f"{r['id']} · {len(r['technical_objects'])} TO · {r['date_from']} → {r['date_to']}"
            for r in runs
        }
        run_ids = [r["id"] for r in runs]
        default = st.session_state.get("selected_run_id")
        index = run_ids.index(default) if default in run_ids else 0
        selected_run_id = st.selectbox(
            "Current migration",
            run_ids,
            index=index,
            format_func=lambda value: labels[value],
        )
        st.session_state["selected_run_id"] = selected_run_id
        current_run = db.get_run(selected_run_id)

        pct = overall_progress(selected_run_id)
        st.progress(pct, text=f"Overall progress · {pct*100:.0f}%")
        st.caption("Progress is checkpointed in SQLite and survives browser/app restarts.")

        auto_manage = st.toggle(
            "Live status refresh",
            value=True,
            help=f"Checks active SAP / EIoT work approximately every {settings.auto_poll_seconds} seconds.",
        )
        st.caption(f"Refresh interval: {settings.auto_poll_seconds}s")

        snapshot = json.dumps(run_snapshot(selected_run_id), indent=2).encode("utf-8")
        st.download_button(
            "Download current run snapshot",
            data=snapshot,
            file_name=f"{selected_run_id}_run_snapshot.json",
            mime="application/json",
            use_container_width=True,
        )

        with st.expander("Run actions"):
            if current_run:
                if current_run["paused"]:
                    if st.button("Resume migration", type="primary", use_container_width=True):
                        service.resume_run(selected_run_id)
                        st.rerun()
                else:
                    if st.button("Pause migration", use_container_width=True):
                        service.pause_run(selected_run_id)
                        st.rerun()
                if st.button("Abort & delete migration", use_container_width=True):
                    delete_run_dialog(selected_run_id)
    else:
        selected_run_id = None
        auto_manage = True

    with st.expander("Advanced"):
        st.markdown("##### Performance & safety")
        st.json(settings.public_summary())
        st.markdown("##### APIs")
        st.markdown(
            """
**SIoT**
- IndicatorService — source discovery
- Cold Store — historical export

**EIoT**
- IndicatorService — logical target matching
- Metadata Sync — `managedObjectId` / `measuringNodeId`
- FileUploadService — validation and production ingestion
            """
        )


# -----------------------------------------------------------------------------
# Header / empty state
# -----------------------------------------------------------------------------
app_header(APP_VERSION)

runs = db.list_runs()
selected_run_id = st.session_state.get("selected_run_id")
if not selected_run_id and runs:
    selected_run_id = runs[0]["id"]
    st.session_state["selected_run_id"] = selected_run_id

if not selected_run_id:
    empty_state()
    c1, c2, c3 = st.columns([1, 1.35, 1])
    with c2:
        if st.button("＋ Create first migration", type="primary", use_container_width=True):
            new_migration_dialog()
    st.stop()

run = db.get_run(selected_run_id)
if not run:
    st.rerun()
paths = service.paths(selected_run_id)
background_monitor(selected_run_id, auto_manage)
run = db.get_run(selected_run_id) or run

if run["paused"]:
    st.markdown(
        '<div class="warning-box"><b>This run is paused.</b> State is preserved. Resume from the sidebar when you want automatic polling and new actions to continue.</div>',
        unsafe_allow_html=True,
    )

# Compact run identity card
run_summary_banner(
    selected_run_id,
    len(run["technical_objects"]),
    f"{run['date_from']} → {run['date_to']}",
    format_bytes(service.disk_usage_bytes(selected_run_id)),
)

stage_strip(run, phase_progress_map(selected_run_id))

scope_tab, extract_tab, transform_tab, validation_tab, load_tab, reports_tab = st.tabs([
    "Scope",
    "Extraction",
    "Transform",
    "Validation",
    "Production",
    "Reports",
])


# -----------------------------------------------------------------------------
# 1. Scope / discovery
# -----------------------------------------------------------------------------
with scope_tab:
    phase_header("Stage 01", "Scope & source discovery", "Review Technical Objects, discover SIoT positions and characteristics, and choose exactly what should continue.", tone="cyan", chip="Source metadata")
    scope = service.scope_metrics(selected_run_id)
    discovery_items = db.list_discovery_items(selected_run_id)
    if not discovery_items:
        db.replace_discovery_items(selected_run_id, run["technical_objects"])
        discovery_items = db.list_discovery_items(selected_run_id)
    dm = service.discovery_metrics(selected_run_id)

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        metric_card("Technical Objects", scope["technical_objects"], "Unique requested TOs")
    with c2:
        metric_card("Discovered", dm["success"], "Checkpointed TOs")
    with c3:
        metric_card("Pending", dm["pending"], "Not yet discovered")
    with c4:
        metric_card("Needs attention", dm["attention"], "Retryable / review")
    with c5:
        metric_card("Scope risk", scope["risk"], f"{scope['days']} day history")

    progress_panel(
        "Discovery progress",
        dm["success"],
        dm["total"],
        eta=estimate_eta(selected_run_id, "DISCOVERY", dm["success"], dm["total"]),
        last_updated=latest_timestamp(discovery_items, ("updated_at",)),
        note=f"Up to {settings.discovery_workers} TOs queried concurrently",
    )

    st.markdown(
        '<div class="info-box"><b>Resumable discovery</b><br>Each TO is checkpointed independently. If the app or network stops halfway through hundreds of TOs, successful TOs stay complete and only pending/failed rows need to be selected again.</div>',
        unsafe_allow_html=True,
    )

    discovery_rows = []
    for item in discovery_items:
        backend = item["status"]
        if backend == "SUCCESS":
            label, meaning = "Discovered", "Source positions and characteristics are checkpointed."
        elif backend == "SUCCESS_WITH_ISSUES":
            label, meaning = "Discovered with notes", "Usable metadata was found, but some source indicators need review."
        elif backend == "NO_INDICATORS":
            label, meaning = "Needs attention", "No SIoT indicators were returned for this TO."
        elif backend == "FAILED":
            label, meaning = "Needs attention", "IndicatorService request failed; select this TO to retry."
        elif backend == "RUNNING":
            label, meaning = "Discovering", "SIoT metadata request is running."
        else:
            label, meaning = "Not started", "Select this TO when you want to discover it."
        discovery_rows.append({
            "id": item["id"],
            "Technical Object": item["technical_object"],
            "Status": label,
            "BackendStatus": backend,
            "Indicators": int(item.get("indicator_count") or 0),
            "Positions": int(item.get("position_count") or 0),
            "What it means": meaning,
            "Updated": _format_time(item.get("updated_at")),
            "Issue": service.human_error(item.get("error")) if item.get("error") else "",
        })
    discovery_state = pd.DataFrame(discovery_rows)
    actionable_discovery = [
        item["id"] for item in discovery_items
        if item["status"] in {"PENDING", "FAILED", "NO_INDICATORS", "SUCCESS_WITH_ISSUES"}
    ]
    selected_discovery_ids = selectable_editor(
        discovery_state.drop(columns=["BackendStatus"]),
        id_col="id",
        key=f"discovery_{selected_run_id}",
        eligible_ids=actionable_discovery,
        height=360,
    )

    a1, a2 = st.columns([1.2, 3])
    if a1.button(
        f"Discover selected ({len(selected_discovery_ids)})",
        type="primary",
        disabled=run["paused"] or not selected_discovery_ids,
        use_container_width=True,
    ):
        bar, callback = progress_callback("Discovering SIoT metadata")
        try:
            service.discover_run(selected_run_id, selected_discovery_ids, callback)
            bar.empty()
            st.rerun()
        except Exception as exc:
            bar.empty()
            st.error(str(exc))
    if a2.button("Refresh page", use_container_width=False):
        st.rerun()

    done, pending, attention = split_status_frame(
        discovery_state,
        status_col="BackendStatus",
        complete_values={"SUCCESS"},
        attention_values={"FAILED", "NO_INDICATORS", "SUCCESS_WITH_ISSUES"},
    )
    status_downloads(
        "Download discovery status at any time:",
        discovery_state,
        done,
        pending,
        attention,
        run_id=selected_run_id,
        phase="discovery",
    )

    discovery_path = paths.reports / "discovery_report.csv"
    issues_path = paths.reports / "discovery_issues.csv"
    if discovery_path.exists():
        st.markdown("#### Discovered source indicators")
        show_file(
            discovery_path,
            columns=[
                "TechnicalObject", "PositionName", "SourcePositionId", "Category",
                "CharacteristicId", "CharacteristicName", "DataType", "UnitOfMeasure",
            ],
        )
        d1, d2 = st.columns(2)
        with d1:
            report_button("Download discovery report", discovery_path, f"disc_{selected_run_id}", primary=True)
        with d2:
            report_button("Download discovery issues", issues_path, f"disc_issues_{selected_run_id}")
        st.markdown(
            '<div class="success-box"><b>Target match rule</b><br>EIoT is matched by <b>Technical Object + Category + Characteristic Internal ID + Position Name</b>. Source/target position IDs and SSIDs are allowed to differ.</div>',
            unsafe_allow_html=True,
        )


# -----------------------------------------------------------------------------
# 2. Extraction
# -----------------------------------------------------------------------------
with extract_tab:
    phase_header("Stage 02", "Cold Store extraction", "Start only selected position/date jobs, monitor SAP preparation live and download completed exports without repeating finished work.", tone="teal", chip="SIoT Cold Store")
    jobs = [j for j in db.list_extraction_jobs(selected_run_id) if j["status"] != "SPLIT"]
    metrics = service.extraction_metrics(selected_run_id)

    last_update = latest_timestamp(jobs, ("updated_at", "completed_at"))
    progress_panel(
        "Extraction progress",
        metrics["complete"],
        metrics["total"],
        eta=estimate_eta(selected_run_id, "EXTRACTION", metrics["complete"], metrics["total"]),
        last_updated=last_update,
        note=f"Auto-refresh every {settings.auto_poll_seconds}s while active",
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        metric_card("Jobs", metrics["total"], "Unique position/date jobs")
    with c2:
        metric_card("Downloaded", metrics["downloaded"], "CSV available locally")
    with c3:
        metric_card("SAP preparing", metrics["preparing"], "No action required")
    with c4:
        metric_card("Ready", metrics["ready"], "Ready to download")
    with c5:
        metric_card("Needs attention", metrics["failed"], "Retry only selected rows")

    st.markdown(
        '<div class="info-box"><b>Row-level control</b><br>All actionable rows are selected by default. Deselect anything you do not want to start, download or retry. Completed work remains checkpointed and is never repeated automatically.</div>',
        unsafe_allow_html=True,
    )

    if not jobs:
        st.info("Run discovery first. Extraction rows will appear after source positions are discovered.")
    else:
        raw_rows: list[dict[str, Any]] = []
        for job in jobs:
            label, _tone, note = friendly_extraction(job["status"])
            raw_rows.append({
                "id": job["id"],
                "Position": job["position_id"],
                "From": job["date_from"],
                "To": job["date_to"],
                "Status": label,
                "BackendStatus": job["status"],
                "What it means": note,
                "RequestId": job.get("request_id") or "",
                "Updated": _format_time(job.get("updated_at")),
                "Issue": service.human_error(job.get("error")) if job.get("error") else "",
            })
        job_frame = pd.DataFrame(raw_rows)
        display_frame = job_frame.drop(columns=["BackendStatus"])
        actionable = [
            j["id"] for j in jobs
            if j["status"] in {"PLANNED", "FAILED", "Failed", "Exception", "Expired", "Ready for Download"}
        ]
        selected_job_ids = selectable_editor(
            display_frame,
            id_col="id",
            key=f"extract_{selected_run_id}",
            eligible_ids=actionable,
            height=390,
        )
        selected_jobs = [j for j in jobs if j["id"] in set(selected_job_ids)]
        startable = [j for j in selected_jobs if j["status"] in {"PLANNED", "FAILED", "Failed", "Exception", "Expired"}]
        ready_selected = [j for j in selected_jobs if j["status"] == "Ready for Download"]
        active = [
            j for j in jobs
            if j.get("request_id") and j["status"] not in {
                "Ready for Download", "Downloaded", "FAILED", "Failed", "Exception", "Expired", "NO_DATA"
            }
        ]

        a1, a2, a3 = st.columns(3)
        if a1.button(
            f"Start / retry selected ({len(startable)})",
            type="primary",
            disabled=run["paused"] or not startable,
            use_container_width=True,
        ):
            bar, callback = progress_callback("Starting selected export jobs")
            try:
                service.start_extraction(selected_run_id, [j["id"] for j in startable], callback)
                bar.empty()
                st.rerun()
            except Exception as exc:
                bar.empty()
                st.error(str(exc))
        if a2.button("Refresh SAP now", disabled=run["paused"] or not active, use_container_width=True):
            bar, callback = progress_callback("Checking SAP")
            try:
                service.refresh_extraction_status(selected_run_id, callback)
                bar.empty()
                st.rerun()
            except Exception as exc:
                bar.empty()
                st.error(str(exc))
        if a3.button(
            f"Download selected ready ({len(ready_selected)})",
            disabled=run["paused"] or not ready_selected,
            use_container_width=True,
        ):
            bar, callback = progress_callback("Downloading selected exports")
            try:
                service.download_ready_exports(selected_run_id, [j["id"] for j in ready_selected], callback)
                bar.empty()
                st.rerun()
            except Exception as exc:
                bar.empty()
                st.error(str(exc))

        done, pending, attention = split_status_frame(
            job_frame,
            status_col="BackendStatus",
            complete_values={"Downloaded", "NO_DATA"},
            attention_values={"FAILED", "Failed", "Exception", "Expired"},
        )
        status_downloads(
            "Download the extraction state at any time:",
            job_frame,
            done,
            pending,
            attention,
            run_id=selected_run_id,
            phase="extraction",
        )

        with st.expander("Status meanings"):
            st.markdown(
                """
- **Not started** — not submitted to Cold Store yet.
- **SAP preparing** — SAP accepted the request and is assembling the export.
- **Ready to download** — SAP finished preparing the export.
- **Downloaded** — export downloaded and unpacked into CSV files locally.
- **No source data** — SAP reported no data for this source position/date range.
- **Needs attention** — only the selected failed rows are retried.
                """
            )
        raw_errors = [j for j in jobs if j.get("error")]
        if raw_errors:
            with st.expander("Advanced technical details"):
                st.dataframe(
                    pd.DataFrame([
                        {
                            "Position": j["position_id"],
                            "From": j["date_from"],
                            "To": j["date_to"],
                            "Backend status": j["status"],
                            "Raw error": j.get("error"),
                        }
                        for j in raw_errors
                    ]),
                    use_container_width=True,
                    hide_index=True,
                )


# -----------------------------------------------------------------------------
# 3. Transform
# -----------------------------------------------------------------------------
with transform_tab:
    phase_header("Stage 03", "Transform & map", "Resolve target identities once, transform source CSVs in parallel and preserve both successful and skipped mappings for audit.", tone="violet", chip="Mapping + Parquet")
    items = db.list_transform_items(selected_run_id)
    metrics = service.transform_metrics(selected_run_id)
    good_transform = metrics["success"] + metrics["no_data"]
    last_update = latest_timestamp(items, ("updated_at",))
    progress_panel(
        "Transformation progress",
        good_transform,
        metrics["total"],
        eta=estimate_eta(selected_run_id, "TRANSFORM", good_transform, metrics["total"]),
        last_updated=last_update,
        note=f"Up to {settings.transform_workers} CSV files transform concurrently",
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        metric_card("CSV files", metrics["total"], "Registered from downloaded exports")
    with c2:
        metric_card("Ready", metrics["ready"], "Available for selection")
    with c3:
        metric_card("Transformed", metrics["success"], "Validated Parquet outputs")
    with c4:
        metric_card("Needs attention", metrics["failed"], "Can be selected for retry")
    with c5:
        metric_card("Target rows", f"{metrics['target_rows']:,}", "Across completed files")

    st.markdown(
        '<div class="info-box"><b>Performance</b><br>Mappings are resolved once per TO and cached. CSVs are scanned once, sparse characteristics avoid a full wide melt, and multiple files transform in parallel.</div>',
        unsafe_allow_html=True,
    )

    if not items:
        st.info("No CSV files are available yet. Download at least one Cold Store export first.")
    else:
        rows = []
        for item in items:
            label, _tone, note = friendly_transform(item["status"])
            rows.append({
                "id": item["id"],
                "CSV file": item["source_name"],
                "Position": item.get("position_id") or "",
                "From": item.get("date_from") or "",
                "To": item.get("date_to") or "",
                "Status": label,
                "BackendStatus": item["status"],
                "What it means": note,
                "Source rows": int(item.get("source_rows") or 0),
                "Target rows": int(item.get("target_rows") or 0),
                "Updated": _format_time(item.get("updated_at")),
                "Issue": service.human_error(item.get("error")) if item.get("error") else "",
            })
        transform_frame = pd.DataFrame(rows)
        display = transform_frame.drop(columns=["BackendStatus"])
        eligible = [i["id"] for i in items if i["status"] in {"READY", "FAILED", "NO_MAPPINGS"}]
        selected_item_ids = selectable_editor(
            display,
            id_col="id",
            key=f"transform_{selected_run_id}",
            eligible_ids=eligible,
            height=400,
        )
        if st.button(
            f"Transform selected ({len(selected_item_ids)})",
            type="primary",
            disabled=run["paused"] or not selected_item_ids,
        ):
            bar, callback = progress_callback("Resolving mappings and transforming")
            try:
                service.transform_run(selected_run_id, selected_item_ids, callback)
                bar.empty()
                st.rerun()
            except Exception as exc:
                bar.empty()
                st.error(str(exc))

        done, pending, attention = split_status_frame(
            transform_frame,
            status_col="BackendStatus",
            complete_values={"SUCCESS", "NO_DATA"},
            attention_values={"FAILED", "NO_MAPPINGS"},
        )
        status_downloads(
            "Download the transformation state at any time:",
            transform_frame,
            done,
            pending,
            attention,
            run_id=selected_run_id,
            phase="transform",
        )

        map_path = paths.reports / "mapping_report.csv"
        skip_path = paths.reports / "skipped_mapping_report.csv"
        if map_path.exists():
            r1, r2 = st.columns(2)
            with r1:
                report_button("Download mapped combinations", map_path, f"map_{selected_run_id}", primary=True)
            with r2:
                report_button("Download skipped mappings", skip_path, f"skip_{selected_run_id}")
        with st.expander("Transformation technical summary"):
            show_file(paths.reports / "transformation_summary.csv")


# -----------------------------------------------------------------------------
# 4. Validation
# -----------------------------------------------------------------------------
with validation_tab:
    phase_header("Stage 04", "Customer validation", "Create a representative sample, download complete validation details and wait for EIoT processing plus customer approval.", tone="amber", chip="Human checkpoint")
    success_items = [
        i for i in db.list_transform_items(selected_run_id)
        if i["status"] == "SUCCESS" and i.get("output_parquet")
    ]
    validation_uploads = db.list_uploads(selected_run_id, "VALIDATION")
    run = db.get_run(selected_run_id) or run

    if run.get("customer_approved"):
        validation_done = 1
    elif validation_uploads and validation_uploads[-1].get("status") == "Processed":
        validation_done = 1
    else:
        validation_done = 0
    progress_panel(
        "Validation progress",
        validation_done,
        1,
        eta="Waiting for customer approval" if validation_uploads and validation_uploads[-1].get("status") == "Processed" and not run.get("customer_approved") else "Complete" if run.get("customer_approved") else "Depends on EIoT processing",
        last_updated=latest_timestamp(validation_uploads, ("last_checked_at", "processing_end_time", "uploaded_time")),
        note="Validation must be processed by EIoT before production is unlocked",
    )

    st.markdown(
        '<div class="warning-box"><b>Human checkpoint</b><br>Create a representative sample from selected transformed files. The customer can download the full validation details—including complete managedObjectId and measuringNodeId values—rather than relying on screenshots.</div>',
        unsafe_allow_html=True,
    )

    if success_items and not any(u.get("file_id") for u in validation_uploads):
        validation_frame = pd.DataFrame([
            {
                "id": i["id"],
                "Transformed file": Path(i["output_parquet"]).name,
                "Position": i.get("position_id") or "",
                "Target rows": int(i.get("target_rows") or 0),
            }
            for i in success_items
        ])
        selected_validation_ids = selectable_editor(
            validation_frame,
            id_col="id",
            key=f"validation_sources_{selected_run_id}",
            eligible_ids=[i["id"] for i in success_items],
            height=280,
        )
        sample_rows = st.number_input("Validation sample rows", min_value=1, max_value=1000, value=10, step=1)
        if st.button(
            f"Create sample from selected ({len(selected_validation_ids)})",
            type="primary",
            disabled=run["paused"] or not selected_validation_ids,
        ):
            try:
                service.create_validation_sample(selected_run_id, int(sample_rows), selected_validation_ids)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    elif not success_items:
        st.info("Transform at least one CSV successfully before creating a validation sample.")

    preview_path = paths.validation / "validation_preview.csv"
    if preview_path.exists():
        try:
            preview = pd.read_csv(preview_path)
        except pd.errors.EmptyDataError:
            preview = pd.DataFrame()
        st.markdown("#### Customer validation details")
        visible_cols = [
            "TechnicalObject", "PositionName", "Category", "CharacteristicId", "CharacteristicName",
            "_time", "Value", "managedObjectId", "measuringNodeId",
        ]
        visible = [c for c in visible_cols if c in preview.columns]
        st.dataframe(preview[visible] if visible else preview, use_container_width=True, hide_index=True, height=390)
        d1, d2 = st.columns(2)
        with d1:
            dataframe_download(
                "Download customer validation report (Excel)",
                preview,
                f"{selected_run_id}_customer_validation.xlsx",
                f"validation_xlsx_{selected_run_id}",
                xlsx=True,
                primary=True,
            )
        with d2:
            dataframe_download(
                "Download customer validation report (CSV)",
                preview,
                f"{selected_run_id}_customer_validation.csv",
                f"validation_csv_{selected_run_id}",
            )

    validation_uploads = db.list_uploads(selected_run_id, "VALIDATION")
    if validation_uploads:
        validation_frame = pd.DataFrame([
            {
                "File": u.get("file_name"),
                "Status": friendly_upload(u.get("status"))[0],
                "BackendStatus": u.get("status"),
                "EIoT File ID": u.get("file_id") or "",
                "Records": int(u.get("number_of_records") or 0),
                "Result": u.get("description") or service.human_error(u.get("error")) if u.get("error") else u.get("description") or "",
                "Last checked": _format_time(u.get("last_checked_at")),
            }
            for u in validation_uploads
        ])
        u = validation_uploads[-1]
        label, _tone, note = friendly_upload(u["status"])
        st.markdown(f'<div class="info-box"><b>{label}</b><br>{note}</div>', unsafe_allow_html=True)
        v1, v2 = st.columns(2)
        can_upload = u["status"] in {"READY", "FAILED", "Processing Failed"}
        if v1.button("Upload validation sample", type="primary", disabled=run["paused"] or not can_upload, use_container_width=True):
            try:
                service.upload_validation_sample(selected_run_id)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        active_validation = bool(u.get("file_id")) and u["status"] not in {"Processed", "Processing Failed"}
        if v2.button("Refresh EIoT status", disabled=run["paused"] or not active_validation, use_container_width=True):
            try:
                service.refresh_upload_statuses(selected_run_id, "VALIDATION")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        done, pending, attention = split_status_frame(
            validation_frame,
            status_col="BackendStatus",
            complete_values={"Processed"},
            attention_values={"Processing Failed", "FAILED"},
        )
        status_downloads(
            "Download validation status at any time:",
            validation_frame,
            done,
            pending,
            attention,
            run_id=selected_run_id,
            phase="validation_status",
        )

        run = db.get_run(selected_run_id) or run
        validation_uploads = db.list_uploads(selected_run_id, "VALIDATION")
        if validation_uploads and validation_uploads[-1]["status"] == "Processed" and not run["customer_approved"]:
            st.markdown(
                '<div class="success-box"><b>EIoT processed the validation file successfully.</b><br>Record customer approval only after the customer has checked the downloadable validation details in their UI.</div>',
                unsafe_allow_html=True,
            )
            approved = st.checkbox("The customer has checked the sample and approved production migration.")
            if st.button("Record customer approval", type="primary", disabled=not approved):
                try:
                    service.mark_customer_approved(selected_run_id)
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        elif run["customer_approved"]:
            st.markdown('<div class="success-box"><b>Customer approval recorded.</b> Production preparation is unlocked.</div>', unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# 5. Production
# -----------------------------------------------------------------------------
with load_tab:
    phase_header("Stage 05", "Production load", "Prepare safe upload files, select what moves, submit controlled batches and track every EIoT processing result.", tone="coral", chip="Controlled batches")
    run = db.get_run(selected_run_id) or run
    if not run["customer_approved"]:
        st.markdown(
            '<div class="warning-box"><b>Production is locked.</b> Complete validation and record customer approval first.</div>',
            unsafe_allow_html=True,
        )
    else:
        success_items = [
            i for i in db.list_transform_items(selected_run_id)
            if i["status"] == "SUCCESS" and i.get("output_parquet")
        ]
        production_uploads = db.list_uploads(selected_run_id, "PRODUCTION")
        upload_started = any(u.get("file_id") for u in production_uploads)

        if success_items and not upload_started:
            st.markdown("#### Choose transformed outputs for production")
            prep_frame = pd.DataFrame([
                {
                    "id": i["id"],
                    "Transformed file": Path(i["output_parquet"]).name,
                    "Position": i.get("position_id") or "",
                    "Target rows": int(i.get("target_rows") or 0),
                }
                for i in success_items
            ])
            selected_prep_ids = selectable_editor(
                prep_frame,
                id_col="id",
                key=f"prepare_{selected_run_id}",
                eligible_ids=[i["id"] for i in success_items],
                height=300,
            )
            button_label = "Rebuild prepared production scope" if production_uploads else "Prepare selected production files"
            if st.button(
                f"{button_label} ({len(selected_prep_ids)})",
                type="primary",
                disabled=run["paused"] or not selected_prep_ids,
            ):
                bar, callback = progress_callback("Preparing upload-safe Parquet files")
                try:
                    service.prepare_production_chunks(selected_run_id, selected_prep_ids, callback)
                    bar.empty()
                    st.rerun()
                except Exception as exc:
                    bar.empty()
                    st.error(str(exc))

        production_uploads = db.list_uploads(selected_run_id, "PRODUCTION")
        if production_uploads:
            metrics = service.load_metrics(selected_run_id)
            last_update = latest_timestamp(production_uploads, ("last_checked_at", "processing_end_time", "uploaded_time"))
            progress_panel(
                "Production processing progress",
                metrics["processed"],
                metrics["total"],
                eta=estimate_eta(selected_run_id, "LOAD", metrics["processed"], metrics["total"]),
                last_updated=last_update,
                note=f"Auto-refresh every {settings.auto_poll_seconds}s while EIoT work is active",
            )

            manifest_path = paths.reports / "upload_manifest.csv"
            if manifest_path.exists():
                manifest = pd.read_csv(manifest_path)
                c1, c2, c3 = st.columns(3)
                with c1:
                    metric_card("Prepared files", len(manifest), "Upload-safe Parquet files")
                with c2:
                    metric_card("Prepared records", f"{int(manifest['rows'].sum()):,}", "Selected production scope")
                with c3:
                    metric_card("Prepared size", format_bytes(int(manifest["size_bytes"].sum())), f"≤ {settings.upload_max_mb} MB per file")

            run = db.get_run(selected_run_id) or run
            if not run["production_approved"]:
                st.markdown(
                    '<div class="danger-box"><b>Final production approval</b><br>Review the selected prepared scope before enabling real EIoT uploads.</div>',
                    unsafe_allow_html=True,
                )
                approve = st.checkbox("I have reviewed this prepared production scope and approve the upload.")
                if st.button("Approve production load", type="primary", disabled=not approve):
                    try:
                        service.approve_production_load(selected_run_id)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

            run = db.get_run(selected_run_id) or run
            if run["production_approved"]:
                production_uploads = db.list_uploads(selected_run_id, "PRODUCTION")
                metrics = service.load_metrics(selected_run_id)
                c1, c2, c3, c4, c5 = st.columns(5)
                with c1:
                    metric_card("Prepared", metrics["total"], "Files in approved scope")
                with c2:
                    metric_card("Completed", metrics["processed"], "EIoT Processed")
                with c3:
                    metric_card("Processing", metrics["active"], "Waiting on EIoT")
                with c4:
                    metric_card("Ready", metrics["ready"], "Not uploaded yet")
                with c5:
                    metric_card("Needs attention", metrics["failed"], "Select failed rows to retry")

                upload_rows = []
                for u in production_uploads:
                    label, _tone, note = friendly_upload(u["status"])
                    result = u.get("description") or (service.human_error(u.get("error")) if u.get("error") else "")
                    upload_rows.append({
                        "id": u["id"],
                        "Batch": u.get("batch_no") or "—",
                        "File": u["file_name"],
                        "Records": int(u.get("number_of_records") or 0),
                        "Status": label,
                        "BackendStatus": u["status"],
                        "What it means": note,
                        "EIoT File ID": u.get("file_id") or "",
                        "Result": result,
                        "Last checked": _format_time(u.get("last_checked_at")),
                    })
                upload_frame = pd.DataFrame(upload_rows)
                eligible_upload_ids = [
                    u["id"] for u in production_uploads
                    if u["status"] in {"READY", "FAILED", "Processing Failed"}
                ]
                selected_upload_ids = selectable_editor(
                    upload_frame.drop(columns=["BackendStatus"]),
                    id_col="id",
                    key=f"upload_{selected_run_id}",
                    eligible_ids=eligible_upload_ids,
                    height=390,
                )

                ready_selected = [
                    u for u in production_uploads
                    if u["id"] in set(selected_upload_ids) and u["status"] == "READY"
                ]
                failed_selected = [
                    u for u in production_uploads
                    if u["id"] in set(selected_upload_ids) and u["status"] in {"FAILED", "Processing Failed"}
                ]
                batch_size = st.number_input(
                    "Maximum selected files to send in this batch",
                    min_value=1,
                    max_value=100,
                    value=min(10, max(1, len(ready_selected) or 10)),
                    step=1,
                )
                a1, a2, a3 = st.columns(3)
                if a1.button(
                    f"Upload selected ({min(len(ready_selected), int(batch_size))})",
                    type="primary",
                    disabled=run["paused"] or metrics["active"] > 0 or not ready_selected,
                    use_container_width=True,
                ):
                    bar, callback = progress_callback("Uploading selected production files")
                    try:
                        service.upload_production_batch(
                            selected_run_id,
                            int(batch_size),
                            [u["id"] for u in ready_selected],
                            callback,
                        )
                        bar.empty()
                        st.rerun()
                    except Exception as exc:
                        bar.empty()
                        st.error(str(exc))
                if a2.button("Refresh EIoT now", disabled=run["paused"] or metrics["active"] == 0, use_container_width=True):
                    try:
                        service.refresh_upload_statuses(selected_run_id, "PRODUCTION")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                if a3.button(
                    f"Retry selected failed ({len(failed_selected)})",
                    disabled=run["paused"] or not failed_selected,
                    use_container_width=True,
                ):
                    try:
                        service.retry_failed_production(selected_run_id, [u["id"] for u in failed_selected])
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

                done, pending, attention = split_status_frame(
                    upload_frame,
                    status_col="BackendStatus",
                    complete_values={"Processed"},
                    attention_values={"Processing Failed", "FAILED"},
                )
                status_downloads(
                    "Download production status at any time:",
                    upload_frame,
                    done,
                    pending,
                    attention,
                    run_id=selected_run_id,
                    phase="production",
                )

                with st.expander("Production status meanings"):
                    st.markdown(
                        """
- **Ready to upload** — prepared locally but not sent.
- **Accepted by EIoT** — upload request succeeded; ingestion has not finished.
- **Validated** — file passed EIoT scan/validation.
- **Ingesting** — EIoT is writing time-series data.
- **Successfully completed** — EIoT status is `Processed`.
- **Needs attention** — select only failed rows you want to retry.
                        """
                    )


# -----------------------------------------------------------------------------
# 6. Reports
# -----------------------------------------------------------------------------
with reports_tab:
    phase_header("Audit", "Reports & migration evidence", "Download checkpoint snapshots, customer validation details and final load evidence at any point in the migration.", tone="teal", chip="Customer-ready")
    st.markdown(
        '<div class="info-box"><b>Client evidence</b><br>Use the customer validation Excel before production and <b>Successfully migrated</b> after production. These files contain complete IDs that can be copied directly into the customer UI.</div>',
        unsafe_allow_html=True,
    )
    report_paths = service.run_report_paths(selected_run_id)

    c1, c2, c3 = st.columns(3)
    with c1:
        report_button("Download successfully migrated", paths.reports / "successfully_migrated.csv", f"success_{selected_run_id}", primary=True)
        report_button("Download full load audit", paths.reports / "load_report.csv", f"load_{selected_run_id}")
    with c2:
        report_button("Download mapped combinations", paths.reports / "mapping_report.csv", f"maps_{selected_run_id}")
        report_button("Download skipped mappings", paths.reports / "skipped_mapping_report.csv", f"skips_{selected_run_id}")
    with c3:
        report_button("Download final migration summary", paths.reports / "final_migration_summary.json", f"final_{selected_run_id}")
        report_button("Download discovery report", paths.reports / "discovery_report.csv", f"disc_report_{selected_run_id}")

    preview_path = paths.validation / "validation_preview.csv"
    if preview_path.exists():
        try:
            preview = pd.read_csv(preview_path)
            dataframe_download(
                "Download customer validation Excel",
                preview,
                f"{selected_run_id}_customer_validation.xlsx",
                f"reports_validation_xlsx_{selected_run_id}",
                xlsx=True,
                primary=True,
            )
        except pd.errors.EmptyDataError:
            pass

    st.markdown("#### Current checkpoint")
    snapshot = run_snapshot(selected_run_id)
    st.json(snapshot)
    st.download_button(
        "Download checkpoint snapshot",
        data=json.dumps(snapshot, indent=2).encode("utf-8"),
        file_name=f"{selected_run_id}_checkpoint.json",
        mime="application/json",
    )

    with st.expander("All available reports"):
        for name, path in report_paths.items():
            st.write(f"**{name}** — `{path.name}`")

    st.markdown("#### Recent activity")
    activity_timeline(db.list_events(selected_run_id, 30), limit=12)

    disk = service.disk_usage_bytes(selected_run_id)
    st.markdown(f"#### Local run storage · {format_bytes(disk)}")
    run = db.get_run(selected_run_id)
    if run and run["load_status"] == "COMPLETE":
        st.markdown(
            '<div class="success-box"><b>Safe cleanup is available.</b> Final reports and SQLite audit history are retained; heavy intermediate data is removed.</div>',
            unsafe_allow_html=True,
        )
        if st.button("Clean temporary files", use_container_width=True):
            try:
                service.cleanup_run(selected_run_id)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    else:
        st.caption("To discard an unfinished run completely, use Run actions → Abort & delete migration in the sidebar.")
