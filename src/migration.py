from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from .api import EIoTClient, SIoTColdStoreClient, SIoTIndicatorClient
from .config import Settings
from .db import MigrationDB


ProgressCallback = Callable[[int, int, str], None]


def _noop_progress(current: int, total: int, message: str) -> None:
    return None


def normalize_technical_object(value: Any) -> str:
    text = str(value).strip()
    if text.startswith("E_") or text.startswith("F_"):
        return text.split("_", 1)[1]
    return text


def normalize_position_id(value: Any) -> str:
    return str(value or "").strip().removeprefix("P_")


def source_position_for_export(position_id: str) -> str:
    value = normalize_position_id(position_id)
    return f"P_{value}"


def parse_technical_objects(text: str) -> list[str]:
    raw = text.replace(";", "\n").replace(",", "\n").splitlines()
    values = [normalize_technical_object(item) for item in raw if item.strip()]
    return list(dict.fromkeys(values))


def parse_excel_technical_objects(content: bytes) -> tuple[list[str], dict[str, Any]]:
    xls = pd.ExcelFile(io.BytesIO(content))
    candidates = {
        "objectidapm",
        "technicalobject",
        "technicalobjectid",
        "technicalobjectnumber",
        "to",
    }

    def norm_header(value: Any) -> str:
        return "".join(ch for ch in str(value).lower() if ch.isalnum())

    for sheet in xls.sheet_names:
        for header_row in range(8):
            try:
                frame = pd.read_excel(xls, sheet_name=sheet, header=header_row)
            except Exception:
                continue
            header_map = {norm_header(c): c for c in frame.columns}
            match = next((header_map[k] for k in candidates if k in header_map), None)
            if match is None:
                continue
            series = frame[match].dropna()
            values: list[str] = []
            for item in series.tolist():
                if isinstance(item, float) and item.is_integer():
                    item = int(item)
                text = normalize_technical_object(item)
                if text and text.lower() not in {"nan", "none", "#ref!"}:
                    values.append(text)
            if values:
                unique = list(dict.fromkeys(values))
                return unique, {
                    "sheet": sheet,
                    "column": str(match),
                    "rows_with_to": len(values),
                    "unique_technical_objects": len(unique),
                    "duplicates_removed": len(values) - len(unique),
                }
    raise ValueError(
        "Could not find a Technical Object column. Expected a header such as "
        "'Object ID (APM)' or 'Technical Object'."
    )


def generate_date_slices(start: date, end: date, slice_days: int) -> list[tuple[date, date]]:
    if start > end:
        raise ValueError("Start date must be on or before end date.")
    result: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        slice_end = min(end, cursor + timedelta(days=max(1, slice_days) - 1))
        result.append((cursor, slice_end))
        cursor = slice_end + timedelta(days=1)
    return result


def _safe_part(value: str) -> str:
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.+"
    return "".join(c if c in keep else "_" for c in str(value))


def _data_kind(data_type: Any) -> str | None:
    value = str(data_type or "").strip().upper().replace("-", "_").replace(" ", "_")
    if value.startswith("NUM") or value in {
        "DEC", "DECIMAL", "FLOAT", "DOUBLE", "INT", "INTEGER",
        "NUMBER", "BOOLEAN", "BOOL",
    }:
        return "numeric"
    if "DATE" in value:
        return "date"
    return None


def _human_error(value: str | None) -> str:
    text = str(value or "").strip()
    low = text.lower()
    if not text:
        return "No additional detail was returned."
    if "too much data" in low or "too large" in low or "payload too large" in low:
        return "SAP reported that this date range is too large. The app can retry it as smaller date slices."
    if "no data" in low or "data not found" in low:
        return "SAP reported no source time-series data for this position and date range."
    if "badzipfile" in low or "not a zip file" in low:
        return "The downloaded Cold Store archive could not be opened as a ZIP file. Retry the download."
    if "timed out" in low or "timeout" in low:
        return "The request timed out. Retry the selected item; completed items are preserved."
    if "connection" in low:
        return "The network connection failed while contacting SAP. Retry the selected item."
    # Keep operator-facing text concise while retaining the raw detail under Advanced details.
    return text[:280]


@dataclass(frozen=True)
class RunPaths:
    root: Path
    extracted: Path
    transformed: Path
    validation: Path
    ready: Path
    reports: Path
    report_parts: Path

    @classmethod
    def build(cls, data_dir: Path, run_id: str) -> "RunPaths":
        root = data_dir / "runs" / run_id
        obj = cls(
            root=root,
            extracted=root / "extracted",
            transformed=root / "transformed",
            validation=root / "validation",
            ready=root / "ready",
            reports=root / "reports",
            report_parts=root / "reports" / "parts",
        )
        for folder in (
            obj.root, obj.extracted, obj.transformed, obj.validation,
            obj.ready, obj.reports, obj.report_parts,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        return obj


class MigrationService:
    def __init__(self, settings: Settings, db: MigrationDB):
        self.settings = settings
        self.db = db
        self.siot_indicators = SIoTIndicatorClient(settings)
        self.eiot = EIoTClient(settings)
        self.cold_store = SIoTColdStoreClient(settings)

    def paths(self, run_id: str) -> RunPaths:
        return RunPaths.build(self.settings.data_dir, run_id)

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------
    def find_overlapping_runs(
        self,
        technical_objects: Iterable[str],
        date_from: date,
        date_to: date,
    ) -> list[dict[str, Any]]:
        """Return existing local runs whose TO/date scope overlaps the requested scope.

        This is a safety guardrail, not a claim about EIoT server-side upsert semantics.
        Aborted/deleted runs are absent because they have already been removed from SQLite.
        """
        requested = {
            normalize_technical_object(v)
            for v in technical_objects
            if str(v).strip()
        }
        if not requested or date_from > date_to:
            return []
        overlaps: list[dict[str, Any]] = []
        for existing in self.db.list_runs():
            existing_from = date.fromisoformat(existing["date_from"])
            existing_to = date.fromisoformat(existing["date_to"])
            overlap_from = max(date_from, existing_from)
            overlap_to = min(date_to, existing_to)
            if overlap_from > overlap_to:
                continue
            common = sorted(requested.intersection(existing["technical_objects"]))
            if not common:
                continue
            overlaps.append({
                "run_id": existing["id"],
                "technical_objects": common,
                "technical_object_count": len(common),
                "overlap_from": overlap_from.isoformat(),
                "overlap_to": overlap_to.isoformat(),
                "existing_load_status": existing.get("load_status") or "NOT_STARTED",
                "created_at": existing.get("created_at"),
            })
        return overlaps

    def create_run(
        self,
        technical_objects: list[str],
        date_from: date,
        date_to: date,
        *,
        input_mode: str = "MANUAL",
        source_name: str | None = None,
        allow_overlap: bool = False,
    ) -> str:
        tos = list(dict.fromkeys(normalize_technical_object(v) for v in technical_objects if str(v).strip()))
        if not tos:
            raise ValueError("Enter at least one Technical Object.")
        if date_from > date_to:
            raise ValueError("Start date must be on or before end date.")
        overlaps = self.find_overlapping_runs(tos, date_from, date_to)
        if overlaps and not allow_overlap:
            raise ValueError(
                "This scope overlaps a previous local migration. Review the overlap warning and explicitly allow intentional re-migration."
            )
        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
        self.db.create_run(
            run_id,
            tos,
            date_from.isoformat(),
            date_to.isoformat(),
            input_mode=input_mode,
            source_name=source_name,
        )
        self.db.replace_discovery_items(run_id, tos)
        self.paths(run_id)
        self.db.add_event(
            run_id,
            "SCOPE",
            "Migration run created",
            f"{len(tos)} unique Technical Object(s), {date_from.isoformat()} to {date_to.isoformat()}.",
        )
        if overlaps:
            overlap_tos = sum(item["technical_object_count"] for item in overlaps)
            self.db.add_event(
                run_id,
                "SCOPE",
                "Intentional overlapping migration approved",
                f"{len(overlaps)} previous run(s) overlap this scope; {overlap_tos} overlapping TO occurrence(s) were acknowledged by the operator.",
                level="WARN",
            )
        return run_id

    def pause_run(self, run_id: str) -> None:
        self._require_run(run_id)
        self.db.update_run(run_id, paused=True)
        self.db.add_event(run_id, "RUN", "Run paused", "Automatic polling is suspended until the run is resumed.")

    def resume_run(self, run_id: str) -> None:
        self._require_run(run_id)
        self.db.update_run(run_id, paused=False)
        self.db.add_event(run_id, "RUN", "Run resumed")

    def abort_and_delete_run(self, run_id: str) -> dict[str, Any]:
        self._require_run(run_id)
        paths = self.paths(run_id)
        uploads = self.db.list_uploads(run_id)
        remote_file_ids = [u.get("file_id") for u in uploads if u.get("file_id")]
        root = paths.root
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        self.db.delete_run(run_id)
        return {
            "deleted_local_folder": str(root),
            "remote_uploads_already_submitted": len(remote_file_ids),
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def discover_run(
        self,
        run_id: str,
        item_ids: Iterable[int] | None = None,
        progress: ProgressCallback = _noop_progress,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Discover SIoT metadata with per-TO checkpoints.

        Every TO is persisted independently in SQLite, so a process/browser restart
        never forces already successful TOs to be discovered again.
        """
        run = self._require_run(run_id)
        if run["paused"]:
            raise RuntimeError("Resume the run before starting discovery.")
        paths = self.paths(run_id)
        items = self.db.list_discovery_items(run_id)
        if not items:
            # Compatibility for databases created before V4.
            self.db.replace_discovery_items(run_id, run["technical_objects"])
            items = self.db.list_discovery_items(run_id)

        wanted = set(int(v) for v in item_ids) if item_ids is not None else None
        selected = [
            item for item in items
            if (wanted is None or item["id"] in wanted)
            and item["status"] in {"PENDING", "FAILED", "NO_INDICATORS", "SUCCESS_WITH_ISSUES"}
        ]
        if not selected:
            raise RuntimeError("Select at least one Technical Object that still needs discovery or retry.")

        self.db.update_run(run_id, discovery_status="RUNNING", message=None)
        self.db.add_event(run_id, "DISCOVERY", "Source discovery started", f"{len(selected)} selected TO(s).")

        def fetch(item: dict[str, Any]):
            technical_object = item["technical_object"]
            try:
                indicators = self.siot_indicators.get_indicators(technical_object)
                return item, indicators, None
            except Exception as exc:
                return item, None, str(exc)[:4000]

        completed = 0
        with ThreadPoolExecutor(max_workers=min(self.settings.discovery_workers, len(selected))) as pool:
            futures = [pool.submit(fetch, item) for item in selected]
            for future in as_completed(futures):
                item, indicators, request_error = future.result()
                technical_object = item["technical_object"]
                rows: list[dict[str, Any]] = []
                issues: list[dict[str, Any]] = []

                if request_error:
                    issues.append({
                        "TechnicalObject": technical_object,
                        "Reason": "IndicatorService request failed",
                        "Details": request_error,
                    })
                    self.db.update_discovery_item(
                        item["id"],
                        status="FAILED",
                        indicator_count=0,
                        position_count=0,
                        result_json=json.dumps({"rows": rows, "issues": issues}),
                        error=request_error,
                    )
                elif not indicators:
                    issues.append({
                        "TechnicalObject": technical_object,
                        "Reason": "No SIoT indicators found",
                        "Details": "",
                    })
                    self.db.update_discovery_item(
                        item["id"],
                        status="NO_INDICATORS",
                        indicator_count=0,
                        position_count=0,
                        result_json=json.dumps({"rows": rows, "issues": issues}),
                        error="No SIoT indicators found",
                    )
                else:
                    for indicator in indicators:
                        position = indicator.get("positionDetails") or {}
                        characteristic = indicator.get("characteristics") or {}
                        position_id = indicator.get("positionDetails_ID") or position.get("ID")
                        if not position_id:
                            issues.append({
                                "TechnicalObject": technical_object,
                                "Reason": "Source indicator has no position ID",
                                "Details": str(indicator.get("ID") or ""),
                            })
                            continue
                        rows.append({
                            "TechnicalObject": normalize_technical_object(technical_object),
                            "TechnicalObjectType": indicator.get("technicalObject_type"),
                            "SourceSSID": indicator.get("technicalObject_SSID"),
                            "SourceIndicatorId": indicator.get("ID"),
                            "SourcePositionId": normalize_position_id(position_id),
                            "ExtractionId": source_position_for_export(str(position_id)),
                            "PositionName": position.get("name"),
                            "Category": indicator.get("category_name"),
                            "CharacteristicId": indicator.get("characteristics_characteristicsInternalId"),
                            "CharacteristicName": characteristic.get("characteristicsName"),
                            "DataType": characteristic.get("dataType"),
                            "UnitOfMeasure": characteristic.get("unitOfMeasure"),
                        })
                    position_count = len({row["ExtractionId"] for row in rows if row.get("ExtractionId")})
                    status = "SUCCESS_WITH_ISSUES" if issues else "SUCCESS"
                    self.db.update_discovery_item(
                        item["id"],
                        status=status,
                        indicator_count=len(rows),
                        position_count=position_count,
                        result_json=json.dumps({"rows": rows, "issues": issues}),
                        error=(issues[0]["Reason"] if issues else None),
                    )

                completed += 1
                progress(completed, len(selected), f"Discovering SIoT metadata ({completed}/{len(selected)})")

        # Rebuild reports from every persisted TO checkpoint, including TOs that
        # succeeded in earlier sessions.
        all_rows: list[dict[str, Any]] = []
        all_issues: list[dict[str, Any]] = []
        refreshed_items = self.db.list_discovery_items(run_id)
        for item in refreshed_items:
            raw = item.get("result_json")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            all_rows.extend(payload.get("rows") or [])
            all_issues.extend(payload.get("issues") or [])

        discovery_df = pd.DataFrame(all_rows)
        if not discovery_df.empty:
            discovery_df = discovery_df.drop_duplicates()
        issues_df = pd.DataFrame(all_issues)
        discovery_df.to_csv(paths.reports / "discovery_report.csv", index=False)
        issues_df.to_csv(paths.reports / "discovery_issues.csv", index=False)

        if not discovery_df.empty:
            positions = sorted(discovery_df["ExtractionId"].dropna().astype(str).unique())
            slices = generate_date_slices(
                date.fromisoformat(run["date_from"]),
                date.fromisoformat(run["date_to"]),
                self.settings.export_slice_days,
            )
            jobs = [
                {"position_id": p, "date_from": start.isoformat(), "date_to": end.isoformat()}
                for p in positions
                for start, end in slices
            ]
            existing_jobs = self.db.list_extraction_jobs(run_id)
            if existing_jobs and any(j["status"] != "PLANNED" for j in existing_jobs):
                self.db.insert_extraction_jobs(run_id, jobs)
            else:
                self.db.replace_extraction_jobs(run_id, jobs)
        else:
            positions = []
            jobs = []

        statuses = [item["status"] for item in refreshed_items]
        pending = any(s in {"PENDING", "RUNNING"} for s in statuses)
        attention = any(s in {"FAILED", "NO_INDICATORS", "SUCCESS_WITH_ISSUES"} for s in statuses)
        success = any(s in {"SUCCESS", "SUCCESS_WITH_ISSUES"} for s in statuses)
        if pending and success:
            run_status = "PARTIAL"
        elif pending:
            run_status = "RUNNING"
        elif attention and success:
            run_status = "COMPLETE_WITH_ISSUES"
        elif attention and not success:
            run_status = "FAILED"
        else:
            run_status = "COMPLETE"
        self.db.update_run(run_id, discovery_status=run_status, message=None)
        self.db.add_event(
            run_id,
            "DISCOVERY",
            "Discovery checkpoint updated",
            f"{sum(s in {'SUCCESS', 'SUCCESS_WITH_ISSUES'} for s in statuses)} of {len(statuses)} TO(s) discovered; "
            f"{len(positions)} unique source position(s), {len(jobs)} planned Cold Store job(s).",
        )
        return discovery_df, issues_df

    def discovery_metrics(self, run_id: str) -> dict[str, int]:
        items = self.db.list_discovery_items(run_id)
        return {
            "total": len(items),
            "success": sum(i["status"] in {"SUCCESS", "SUCCESS_WITH_ISSUES"} for i in items),
            "pending": sum(i["status"] in {"PENDING", "RUNNING"} for i in items),
            "failed": sum(i["status"] in {"FAILED", "NO_INDICATORS"} for i in items),
            "attention": sum(i["status"] in {"FAILED", "NO_INDICATORS", "SUCCESS_WITH_ISSUES"} for i in items),
            "indicators": sum(int(i.get("indicator_count") or 0) for i in items),
            "positions": sum(int(i.get("position_count") or 0) for i in items),
        }

    # ------------------------------------------------------------------
    # Extraction — selected jobs only, adaptive splitting, resumable
    # ------------------------------------------------------------------
    def start_extraction(
        self,
        run_id: str,
        job_ids: Iterable[int] | None = None,
        progress: ProgressCallback = _noop_progress,
    ) -> None:
        run = self._require_run(run_id)
        if run["paused"]:
            raise RuntimeError("Resume the run before starting extraction.")
        if not str(run["discovery_status"]).startswith("COMPLETE"):
            raise RuntimeError("Discovery must be complete before extraction.")

        all_jobs = self.db.list_extraction_jobs(run_id)
        if not all_jobs:
            raise RuntimeError("No extraction jobs were planned.")
        requested_ids = set(int(v) for v in (job_ids or [j["id"] for j in all_jobs]))
        if not requested_ids:
            raise RuntimeError("Select at least one extraction row.")

        self.db.update_run(run_id, extraction_status="RUNNING", message=None)
        self.db.add_event(
            run_id,
            "EXTRACTION",
            "Selected extraction jobs started",
            f"{len(requested_ids)} selected row(s).",
        )

        # target_ids grows when a selected job is adaptively split. Only descendants
        # of selected rows are auto-started; unselected rows remain untouched.
        target_ids = set(requested_ids)
        round_no = 0
        while True:
            round_no += 1
            jobs = self.db.list_extraction_jobs(run_id)
            planned = [
                j for j in jobs
                if j["id"] in target_ids and j["status"] in {"PLANNED", "FAILED"}
            ]
            if not planned:
                break

            def initiate(job: dict[str, Any]):
                try:
                    request_id = self.cold_store.initiate_export(
                        job["position_id"], job["date_from"], job["date_to"]
                    )
                    return job, request_id, None
                except requests.HTTPError as exc:
                    text = exc.response.text if exc.response is not None else str(exc)
                    return job, None, text[:4000]
                except Exception as exc:
                    return job, None, str(exc)[:4000]

            results: list[tuple[dict[str, Any], str | None, str | None]] = []
            with ThreadPoolExecutor(max_workers=self.settings.export_workers) as pool:
                futures = [pool.submit(initiate, job) for job in planned]
                for index, future in enumerate(as_completed(futures), start=1):
                    results.append(future.result())
                    progress(index, len(planned), f"Starting selected Cold Store exports ({index}/{len(planned)})")

            split_parents: list[int] = []
            for job, request_id, error_text in results:
                if request_id:
                    self.db.update_extraction_job(
                        job["id"], request_id=request_id, status="Initiated", error=None, completed_at=None
                    )
                    continue

                lowered = (error_text or "").lower()
                if "no data" in lowered or "data not found" in lowered:
                    self.db.update_extraction_job(
                        job["id"], status="NO_DATA", error=None,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    continue

                too_large = any(marker in lowered for marker in (
                    "too much data", "too large", "payload too large",
                    "request entity too large", "timeframe", "time frame",
                ))
                start = date.fromisoformat(job["date_from"])
                end = date.fromisoformat(job["date_to"])
                days = (end - start).days + 1
                if too_large and days > self.settings.export_min_slice_days:
                    left_days = max(1, days // 2)
                    midpoint = min(end - timedelta(days=1), start + timedelta(days=left_days - 1))
                    children = [
                        {
                            "position_id": job["position_id"],
                            "date_from": start.isoformat(),
                            "date_to": midpoint.isoformat(),
                            "status": "PLANNED",
                            "parent_job_id": job["id"],
                            "attempt": int(job.get("attempt") or 1) + 1,
                        },
                        {
                            "position_id": job["position_id"],
                            "date_from": (midpoint + timedelta(days=1)).isoformat(),
                            "date_to": end.isoformat(),
                            "status": "PLANNED",
                            "parent_job_id": job["id"],
                            "attempt": int(job.get("attempt") or 1) + 1,
                        },
                    ]
                    self.db.update_extraction_job(
                        job["id"],
                        status="SPLIT",
                        error="SAP reported the period was too large; smaller child jobs were created automatically.",
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    self.db.insert_extraction_jobs(run_id, children)
                    split_parents.append(job["id"])
                    self.db.add_event(
                        run_id,
                        "EXTRACTION",
                        "Large export period split automatically",
                        f"{job['position_id']} · {job['date_from']} → {job['date_to']}",
                    )
                    continue

                self.db.update_extraction_job(
                    job["id"],
                    status="FAILED",
                    error=error_text or "Cold Store export could not be initiated.",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

            if split_parents:
                refreshed = self.db.list_extraction_jobs(run_id)
                for child in refreshed:
                    if child.get("parent_job_id") in split_parents:
                        target_ids.add(int(child["id"]))
            else:
                break
            if round_no > 24:
                raise RuntimeError("Adaptive extraction exceeded the safe split depth.")

        self._update_extraction_run_status(run_id)

    def refresh_extraction_status(
        self,
        run_id: str,
        progress: ProgressCallback = _noop_progress,
    ) -> list[dict[str, Any]]:
        run = self._require_run(run_id)
        if run["paused"]:
            return self.db.list_extraction_jobs(run_id)
        jobs = self.db.list_extraction_jobs(run_id)
        terminal = {
            "Ready for Download", "Downloaded", "Failed", "FAILED", "Exception",
            "Expired", "NO_DATA", "SPLIT", "PLANNED",
        }
        active = [j for j in jobs if j.get("request_id") and j["status"] not in terminal]

        def check(job: dict[str, Any]):
            try:
                return job, self.cold_store.get_export_status(job["request_id"]), None
            except Exception as exc:
                return job, None, str(exc)[:2000]

        if active:
            with ThreadPoolExecutor(max_workers=self.settings.status_workers) as pool:
                futures = [pool.submit(check, job) for job in active]
                for index, future in enumerate(as_completed(futures), start=1):
                    job, status, error = future.result()
                    progress(index, len(active), f"Checking SAP export statuses ({index}/{len(active)})")
                    if status:
                        is_terminal = status in {"Failed", "Exception", "Expired"}
                        self.db.update_extraction_job(
                            job["id"], status=status, error=None,
                            **({"completed_at": datetime.now(timezone.utc).isoformat()} if is_terminal else {}),
                        )
                    elif error:
                        # A transient status-check error does not change the export status.
                        self.db.update_extraction_job(job["id"], error=error)

        self._update_extraction_run_status(run_id)
        return self.db.list_extraction_jobs(run_id)

    def download_ready_exports(
        self,
        run_id: str,
        job_ids: Iterable[int] | None = None,
        progress: ProgressCallback = _noop_progress,
    ) -> list[dict[str, Any]]:
        run = self._require_run(run_id)
        if run["paused"]:
            raise RuntimeError("Resume the run before downloading exports.")
        paths = self.paths(run_id)
        jobs = self.db.list_extraction_jobs(run_id)
        selected = set(int(v) for v in job_ids) if job_ids is not None else None
        ready = [
            j for j in jobs
            if j["status"] == "Ready for Download" and (selected is None or j["id"] in selected)
        ]
        if not ready:
            self._update_extraction_run_status(run_id)
            return jobs

        def download(job: dict[str, Any]):
            folder_name = f"{_safe_part(job['position_id'])}+{job['date_from']}+{job['date_to']}"
            zip_path = paths.extracted / f"{folder_name}.zip"
            extract_dir = paths.extracted / folder_name
            try:
                self.cold_store.download_export(job["request_id"], zip_path)
                self._extract_cold_store_archive(zip_path, extract_dir)
                csv_files = sorted(extract_dir.rglob("*.csv"))
                return job, zip_path, extract_dir, csv_files, None
            except Exception as exc:
                return job, None, None, [], str(exc)[:3000]

        with ThreadPoolExecutor(max_workers=self.settings.download_workers) as pool:
            futures = [pool.submit(download, job) for job in ready]
            for index, future in enumerate(as_completed(futures), start=1):
                job, zip_path, extract_dir, csv_files, error = future.result()
                progress(index, len(ready), f"Downloading selected exports ({index}/{len(ready)})")
                if error:
                    self.db.update_extraction_job(job["id"], status="FAILED", error=error)
                    self.db.add_event(
                        run_id,
                        "EXTRACTION",
                        "Export download needs attention",
                        f"{job['position_id']} · {_human_error(error)}",
                        level="ERROR",
                    )
                    continue

                self.db.update_extraction_job(
                    job["id"],
                    status="Downloaded",
                    zip_path=str(zip_path),
                    extract_dir=str(extract_dir),
                    error=None,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                self.db.upsert_transform_items_bulk(
                    run_id,
                    [
                        {
                            "source_csv": str(csv_file),
                            "source_name": csv_file.name,
                            "extraction_job_id": job["id"],
                            "status": "READY",
                            "error": None,
                        }
                        for csv_file in csv_files
                    ],
                )
                self.db.add_event(
                    run_id,
                    "EXTRACTION",
                    "Export downloaded and unpacked",
                    f"{job['position_id']} · {job['date_from']} → {job['date_to']} · {len(csv_files)} CSV file(s).",
                )

        self._update_extraction_run_status(run_id)
        return self.db.list_extraction_jobs(run_id)

    def _extract_cold_store_archive(self, zip_path: Path, extract_dir: Path) -> None:
        """Extract ZIP and decompress nested .gz files in parallel.

        V2 accidentally exposed this helper as an instance method with a static-style
        signature. V3 intentionally fixes that bug and parallelises gzip expansion.
        """
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extractall(extract_dir)

        gz_files = list(extract_dir.rglob("*.gz"))
        if not gz_files:
            return

        def unpack(gz_path: Path) -> None:
            output_path = gz_path.with_suffix("")
            with gzip.open(gz_path, "rb") as source, output_path.open("wb") as target:
                shutil.copyfileobj(source, target, length=4 * 1024 * 1024)
            gz_path.unlink(missing_ok=True)

        with ThreadPoolExecutor(max_workers=min(self.settings.gzip_workers, len(gz_files))) as pool:
            futures = [pool.submit(unpack, path) for path in gz_files]
            for future in as_completed(futures):
                future.result()

    def _update_extraction_run_status(self, run_id: str) -> None:
        jobs = [j for j in self.db.list_extraction_jobs(run_id) if j["status"] != "SPLIT"]
        if not jobs:
            self.db.update_run(run_id, extraction_status="NOT_STARTED")
            return
        statuses = [j["status"] for j in jobs]
        active = any(s not in {"PLANNED", "Downloaded", "NO_DATA", "FAILED", "Failed", "Exception", "Expired"} for s in statuses)
        ready = any(s == "Ready for Download" for s in statuses)
        failures = any(s in {"FAILED", "Failed", "Exception", "Expired"} for s in statuses)
        downloaded = any(s == "Downloaded" for s in statuses)
        planned = any(s == "PLANNED" for s in statuses)

        if ready:
            status = "READY_TO_DOWNLOAD"
        elif active:
            status = "RUNNING"
        elif failures and not downloaded:
            status = "NEEDS_ATTENTION"
        elif failures:
            status = "PARTIAL_WITH_ISSUES"
        elif downloaded and planned:
            status = "PARTIAL"
        elif downloaded or all(s == "NO_DATA" for s in statuses):
            status = "COMPLETE"
        else:
            status = "NOT_STARTED"
        self.db.update_run(run_id, extraction_status=status, message=None)

    def advance_extraction_cycle(self, run_id: str) -> dict[str, int]:
        run = self._require_run(run_id)
        if run["paused"]:
            return self.extraction_metrics(run_id)
        self.refresh_extraction_status(run_id)
        if self.settings.auto_download_ready:
            self.download_ready_exports(run_id)
        return self.extraction_metrics(run_id)

    # ------------------------------------------------------------------
    # Mapping catalog — resolve once, reuse everywhere
    # ------------------------------------------------------------------
    def ensure_mapping_catalog(
        self,
        run_id: str,
        *,
        position_ids: Iterable[str] | None = None,
        progress: ProgressCallback = _noop_progress,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Resolve source→target mapping once and persist it for reuse.

        Performance strategy:
        - SIoT source metadata comes from the already-expanded discovery report.
        - EIoT IndicatorService is fetched once per Technical Object, not once per
          source indicator combination.
        - EIoT Metadata Sync is fetched once per target TO/SSID/type, with all
          indicators expanded, then matched locally.

        This preserves the exact logical match rule:
        TO + category + characteristic internal ID + position name.
        """
        paths = self.paths(run_id)
        discovery_path = paths.reports / "discovery_report.csv"
        if not discovery_path.exists():
            raise RuntimeError("Run discovery first.")

        identity_dtypes = {
            "TechnicalObject": "string",
            "SourcePositionId": "string",
            "PositionName": "string",
            "Category": "string",
            "CharacteristicId": "string",
        }
        discovery = pd.read_csv(discovery_path, dtype=identity_dtypes)
        if discovery.empty:
            raise RuntimeError("Discovery report is empty.")

        discovery["SourcePositionId"] = discovery["SourcePositionId"].map(normalize_position_id)
        discovery["TechnicalObject"] = discovery["TechnicalObject"].map(normalize_technical_object)
        discovery["Category"] = discovery["Category"].astype("string").str.strip()
        discovery["CharacteristicId"] = discovery["CharacteristicId"].astype("string").str.strip()
        discovery["PositionName"] = discovery["PositionName"].astype("string").fillna("").str.strip()

        if position_ids is not None:
            wanted = {normalize_position_id(v) for v in position_ids}
            discovery = discovery[discovery["SourcePositionId"].isin(wanted)].copy()
        if discovery.empty:
            raise RuntimeError("No discovery rows match the selected extraction positions.")

        required = ["TechnicalObject", "SourcePositionId", "PositionName", "Category", "CharacteristicId"]
        discovery = discovery.dropna(subset=["TechnicalObject", "SourcePositionId", "Category", "CharacteristicId"])
        discovery = discovery.drop_duplicates(required)

        mapping_path = paths.reports / "mapping_catalog.csv"
        skipped_path = paths.reports / "mapping_catalog_skipped.csv"
        def safe_read_csv(path: Path, **kwargs) -> pd.DataFrame:
            if not path.exists() or path.stat().st_size == 0:
                return pd.DataFrame()

            try:
                return pd.read_csv(path, **kwargs)
            except pd.errors.EmptyDataError:
                return pd.DataFrame()


        existing_map = safe_read_csv(
            mapping_path,
            dtype=identity_dtypes,
        )

        existing_skip = safe_read_csv(
            skipped_path,
            dtype=identity_dtypes,
        )

        def text(value: Any) -> str:
            if value is None or (isinstance(value, float) and pd.isna(value)):
                return ""
            return str(value).strip()

        def key_from_values(to: Any, pos: Any, cat: Any, char: Any) -> tuple[str, str, str, str]:
            return (
                normalize_technical_object(to),
                normalize_position_id(pos),
                text(cat),
                text(char),
            )

        def logical_key(category: Any, characteristic: Any, position_name: Any) -> tuple[str, str, str]:
            return (text(category), text(characteristic), text(position_name))

        def base_row(source: dict[str, Any]) -> dict[str, Any]:
            characteristic_id = text(source.get("CharacteristicId"))
            return {
                "TechnicalObject": normalize_technical_object(source.get("TechnicalObject")),
                "SourcePositionId": normalize_position_id(source.get("SourcePositionId")),
                "PositionName": text(source.get("PositionName")),
                "Category": text(source.get("Category")),
                "CharacteristicId": characteristic_id,
                "Characteristic": f"C_{characteristic_id}",
                "CharacteristicName": source.get("CharacteristicName"),
                "SourceDataType": source.get("DataType"),
                "SourceUnitOfMeasure": source.get("UnitOfMeasure"),
            }

        done_keys: set[tuple[str, str, str, str]] = set()
        # Deterministic results are reusable. Transient API failures are retried.
        for frame_name, frame in (("mapped", existing_map), ("skipped", existing_skip)):
            if frame.empty:
                continue
            for row in frame.itertuples(index=False):
                try:
                    if frame_name == "skipped":
                        reason = text(getattr(row, "Reason", ""))
                        if reason.startswith("Mapping API error:"):
                            continue
                    done_keys.add(key_from_values(
                        getattr(row, "TechnicalObject"),
                        getattr(row, "SourcePositionId"),
                        getattr(row, "Category"),
                        getattr(row, "CharacteristicId"),
                    ))
                except AttributeError:
                    continue

        todo_rows: list[dict[str, Any]] = []
        for row in discovery.to_dict("records"):
            key = key_from_values(
                row["TechnicalObject"], row["SourcePositionId"],
                row["Category"], row["CharacteristicId"],
            )
            if key not in done_keys:
                todo_rows.append(row)

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in todo_rows:
            grouped.setdefault(normalize_technical_object(row.get("TechnicalObject")), []).append(row)

        def resolve_to(technical_object: str, source_rows: list[dict[str, Any]]):
            mapped_rows: list[dict[str, Any]] = []
            skipped_rows: list[dict[str, Any]] = []

            # Fetch the complete EIoT indicator shape once for this TO and match locally.
            try:
                target_indicators = self.eiot.get_indicators(technical_object)
            except Exception as exc:
                reason = f"Mapping API error: EIoT IndicatorService: {exc}"
                return [], [{**base_row(source), "Reason": reason} for source in source_rows]

            target_index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
            for indicator in target_indicators:
                position = indicator.get("positionDetails") or {}
                key = logical_key(
                    indicator.get("category_name"),
                    indicator.get("characteristics_characteristicsInternalId"),
                    position.get("name"),
                )
                target_index.setdefault(key, []).append(indicator)

            # First perform the exact logical match and collect the target TO variants
            # (normally one SSID/type pair per TO) whose metadata we need.
            candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
            metadata_groups: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]] = {}
            for source in source_rows:
                base = base_row(source)
                if not base["PositionName"]:
                    skipped_rows.append({**base, "Reason": "SIoT position name missing"})
                    continue

                matches = target_index.get(
                    logical_key(base["Category"], base["CharacteristicId"], base["PositionName"]),
                    [],
                )
                if len(matches) != 1:
                    skipped_rows.append({
                        **base,
                        "Reason": f"EIoT mapping not unique (matches={len(matches)})",
                    })
                    continue

                indicator = matches[0]
                target_position = text(indicator.get("positionDetails_ID"))
                target_ssid = text(indicator.get("technicalObject_SSID"))
                target_type = text(indicator.get("technicalObject_type"))
                if not target_position:
                    skipped_rows.append({**base, "Reason": "EIoT target positionDetails_ID missing"})
                    continue
                if not target_ssid or not target_type:
                    skipped_rows.append({**base, "Reason": "EIoT target SSID/type missing"})
                    continue

                candidate = (source, base, indicator)
                candidates.append(candidate)
                metadata_groups.setdefault((target_ssid, target_type), []).append(candidate)

            # One Metadata Sync request per TO/SSID/type, then local exact lookup by
            # target position + category + characteristic. This removes thousands of
            # small HTTP calls from large migrations.
            metadata_payloads: dict[tuple[str, str], dict[str, Any] | Exception] = {}
            for (target_ssid, target_type) in metadata_groups:
                try:
                    metadata_payloads[(target_ssid, target_type)] = self.eiot.get_technical_object_metadata(
                        number=technical_object,
                        ssid=target_ssid,
                        to_type=target_type,
                    )
                except Exception as exc:
                    metadata_payloads[(target_ssid, target_type)] = exc

            for source, base, indicator in candidates:
                target_ssid = text(indicator.get("technicalObject_SSID"))
                target_type = text(indicator.get("technicalObject_type"))
                target_position = text(indicator.get("positionDetails_ID"))
                payload = metadata_payloads[(target_ssid, target_type)]
                bulk_metadata = not isinstance(payload, Exception)
                if not bulk_metadata:
                    # Compatibility fallback: the filtered metadata form is the API
                    # shape already proven in the original migration notebooks. Some
                    # tenants may restrict an unfiltered indicators expansion.
                    try:
                        payload = self.eiot.get_indicator_metadata(
                            number=technical_object,
                            ssid=target_ssid,
                            to_type=target_type,
                            position_details_id=target_position,
                            category_name=base["Category"],
                            characteristics_internal_id=base["CharacteristicId"],
                        )
                    except Exception as exc:
                        skipped_rows.append({
                            **base,
                            "Reason": f"Mapping API error: EIoT Metadata Sync: {exc}",
                        })
                        continue

                object_sync = payload.get("technicalObjectSyncStatus")
                if object_sync and object_sync != "SYNCED":
                    skipped_rows.append({
                        **base,
                        "Reason": f"EIoT technical object is not SYNCED ({object_sync})",
                    })
                    continue

                meta_matches = []
                for meta in payload.get("indicators", []) or []:
                    meta_position = text(
                        meta.get("positionDetailsId")
                        or meta.get("positionDetails_ID")
                        or meta.get("positionDetailsID")
                    )
                    if (
                        normalize_position_id(meta_position) == normalize_position_id(target_position)
                        and text(meta.get("categoryName")) == base["Category"]
                        and text(meta.get("characteristicsInternalId")) == base["CharacteristicId"]
                    ):
                        meta_matches.append(meta)

                if len(meta_matches) != 1:
                    skipped_rows.append({
                        **base,
                        "Reason": f"EIoT metadata not unique (matches={len(meta_matches)})",
                    })
                    continue

                meta = meta_matches[0]
                indicator_sync = meta.get("syncStatus")
                if indicator_sync and indicator_sync != "SYNCED":
                    skipped_rows.append({
                        **base,
                        "Reason": f"EIoT indicator is not SYNCED ({indicator_sync})",
                    })
                    continue
                data_type = meta.get("dataType")
                if _data_kind(data_type) is None:
                    skipped_rows.append({**base, "Reason": f"Unsupported data type: {data_type}"})
                    continue

                managed_object_id = payload.get("managedObjectId")
                measuring_node_id = meta.get("measuringNodeId")
                if not managed_object_id or not measuring_node_id:
                    skipped_rows.append({
                        **base,
                        "Reason": "EIoT metadata missing managedObjectId/measuringNodeId",
                    })
                    continue

                target_position_details = indicator.get("positionDetails") or {}
                characteristic_details = indicator.get("characteristics") or {}
                mapped_rows.append({
                    **base,
                    "TechnicalObjectType": target_type,
                    "TargetSSID": target_ssid,
                    "TargetIndicatorId": indicator.get("ID"),
                    "TargetPositionId": normalize_position_id(target_position),
                    "TargetPositionName": target_position_details.get("name"),
                    "TargetCharacteristicName": characteristic_details.get("characteristicsName"),
                    "ManagedObjectId": managed_object_id,
                    "MeasuringNodeId": measuring_node_id,
                    "DataType": data_type,
                    "UnitOfMeasure": meta.get("unitOfMeasure"),
                    "CharcLength": meta.get("charcLength"),
                    "CharcDecimals": meta.get("charcDecimals"),
                    "TechnicalGroupId": meta.get("technicalGroupId"),
                })

            return mapped_rows, skipped_rows

        new_map: list[dict[str, Any]] = []
        new_skip: list[dict[str, Any]] = []
        if grouped:
            workers = min(self.settings.mapping_workers, len(grouped))
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {
                    pool.submit(resolve_to, technical_object, rows): technical_object
                    for technical_object, rows in grouped.items()
                }
                for index, future in enumerate(as_completed(futures), start=1):
                    mapped_for_to, skipped_for_to = future.result()
                    new_map.extend(mapped_for_to)
                    new_skip.extend(skipped_for_to)
                    progress(
                        index, len(futures),
                        f"Resolving EIoT mappings by Technical Object ({index}/{len(futures)})",
                    )

        mapping = (
            pd.concat([existing_map, pd.DataFrame(new_map)], ignore_index=True)
            if not existing_map.empty else pd.DataFrame(new_map)
        )
        skipped = (
            pd.concat([existing_skip, pd.DataFrame(new_skip)], ignore_index=True)
            if not existing_skip.empty else pd.DataFrame(new_skip)
        )
        key_columns = ["TechnicalObject", "SourcePositionId", "Category", "CharacteristicId"]
        if not mapping.empty:
            mapping = mapping.drop_duplicates(key_columns, keep="last")
        if not skipped.empty:
            skipped = skipped.drop_duplicates(key_columns, keep="last")

        # A successful retry wins over an earlier skipped/API-error row.
        map_keys = {
            key_from_values(r.TechnicalObject, r.SourcePositionId, r.Category, r.CharacteristicId)
            for r in mapping.itertuples(index=False)
        } if not mapping.empty else set()
        if not skipped.empty and map_keys:
            mask = [
                key_from_values(r.TechnicalObject, r.SourcePositionId, r.Category, r.CharacteristicId) not in map_keys
                for r in skipped.itertuples(index=False)
            ]
            skipped = skipped.loc[mask].copy()

        mapping.to_csv(mapping_path, index=False)
        skipped.to_csv(skipped_path, index=False)
        mapping.to_csv(paths.reports / "mapping_report.csv", index=False)
        skipped.to_csv(paths.reports / "skipped_mapping_report.csv", index=False)
        return mapping, skipped

    # ------------------------------------------------------------------
    # Fast single-pass transformation
    # ------------------------------------------------------------------
    def transform_run(
        self,
        run_id: str,
        item_ids: Iterable[int] | None = None,
        progress: ProgressCallback = _noop_progress,
    ) -> dict[str, Any]:
        run = self._require_run(run_id)
        if run["paused"]:
            raise RuntimeError("Resume the run before transformation.")
        items = self.db.list_transform_items(run_id)
        if item_ids is not None:
            wanted_ids = {int(v) for v in item_ids}
            items = [i for i in items if i["id"] in wanted_ids]
        items = [i for i in items if i["status"] in {"READY", "FAILED", "NO_MAPPINGS"}]
        if not items:
            raise RuntimeError("Select at least one downloaded CSV that is ready to transform.")

        selected_positions = {normalize_position_id(i.get("position_id")) for i in items if i.get("position_id")}
        self.db.update_run(run_id, transform_status="RUNNING", message=None)
        self.db.add_event(run_id, "TRANSFORM", "Selected transformation started", f"{len(items)} CSV file(s).")

        # Key performance change: resolve target mappings once from the already-expanded
        # SIoT discovery report. V2 re-queried SIoT during each CSV transformation.
        mapping_df, skipped_df = self.ensure_mapping_catalog(
            run_id,
            position_ids=selected_positions or None,
            progress=progress,
        )
        if mapping_df.empty:
            raise RuntimeError("None of the selected source combinations has a valid EIoT mapping.")

        selected_tos = set(run["technical_objects"])
        results: list[dict[str, Any]] = []

        def transform_item(item: dict[str, Any]):
            self.db.update_transform_item(item["id"], status="RUNNING", error=None)
            try:
                result = self._transform_one_csv_fast(
                    run_id=run_id,
                    item=item,
                    mapping_df=mapping_df,
                    selected_tos=selected_tos,
                )
                self.db.update_transform_item(
                    item["id"],
                    output_parquet=result.get("output_file"),
                    status=result["status"],
                    source_rows=result["source_rows_scanned"],
                    selected_rows=result["selected_source_rows"],
                    mapped_measurements=result["mapped_measurements"],
                    target_rows=result["parquet_rows"],
                    mapped_combinations=result["mapped_combinations"],
                    skipped_combinations=result["failed_combinations"],
                    error=None,
                )
                return result
            except Exception as exc:
                self.db.update_transform_item(item["id"], status="FAILED", error=str(exc)[:3000])
                return {
                    "csv_file": item["source_csv"],
                    "output_file": None,
                    "source_rows_scanned": 0,
                    "selected_source_rows": 0,
                    "mapped_measurements": 0,
                    "parquet_rows": 0,
                    "mapped_combinations": 0,
                    "failed_combinations": 0,
                    "status": "FAILED",
                    "error": str(exc),
                }

        with ThreadPoolExecutor(max_workers=min(self.settings.transform_workers, len(items))) as pool:
            futures = [pool.submit(transform_item, item) for item in items]
            for index, future in enumerate(as_completed(futures), start=1):
                results.append(future.result())
                progress(index, len(items), f"Transforming selected CSV files ({index}/{len(items)})")

        self._rebuild_transform_reports(run_id, skipped_df)
        self._update_transform_run_status(run_id)
        run_after = self._require_run(run_id)
        if any(r["status"] == "FAILED" for r in results):
            self.db.add_event(run_id, "TRANSFORM", "Transformation completed with items needing attention", level="ERROR")
        else:
            self.db.add_event(run_id, "TRANSFORM", "Selected transformation completed", f"{len(results)} CSV file(s).")

        overall_path = self.paths(run_id).reports / "transformation_overall.json"
        if overall_path.exists():
            return json.loads(overall_path.read_text(encoding="utf-8"))
        return {"run_id": run_id, "status": run_after["transform_status"]}

    def _transform_one_csv_fast(
        self,
        *,
        run_id: str,
        item: dict[str, Any],
        mapping_df: pd.DataFrame,
        selected_tos: set[str],
    ) -> dict[str, Any]:
        paths = self.paths(run_id)
        csv_file = Path(item["source_csv"])
        if not csv_file.exists():
            raise RuntimeError(f"Source CSV no longer exists: {csv_file.name}")
        try:
            relative = csv_file.relative_to(paths.extracted)
        except ValueError:
            relative = Path(csv_file.name)
        output_file = paths.transformed / relative.with_suffix(".parquet")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        temp_output = output_file.with_suffix(output_file.suffix + ".tmp")
        temp_output.unlink(missing_ok=True)

        header = pd.read_csv(csv_file, nrows=0).columns.tolist()
        required = ["_TIME", "TechnicalObject", "Position", "CategoryId"]
        missing = [c for c in required if c not in header]
        if missing:
            raise RuntimeError(f"Missing required CSV columns: {missing}")

        file_characteristics = [c for c in header if c.startswith("C_")]
        if not file_characteristics:
            return self._summary_result(csv_file, None, 0, 0, 0, 0, 0, 0, "NO_DATA")

        local_map = mapping_df.copy()
        local_map["TechnicalObject"] = local_map["TechnicalObject"].map(normalize_technical_object)
        local_map["SourcePositionId"] = local_map["SourcePositionId"].map(normalize_position_id)
        local_map["Category"] = local_map["Category"].astype(str).str.strip()
        local_map["Characteristic"] = local_map["Characteristic"].astype(str)
        local_map = local_map[
            local_map["TechnicalObject"].isin(selected_tos)
            & local_map["Characteristic"].isin(file_characteristics)
        ].copy()
        if local_map.empty:
            return self._summary_result(csv_file, None, 0, 0, 0, 0, 0, 0, "NO_MAPPINGS")

        mapped_characteristics = sorted(local_map["Characteristic"].unique())
        schema = self._build_schema(local_map, mapped_characteristics)
        char_kind = {
            row.Characteristic: _data_kind(row.DataType)
            for row in local_map[["Characteristic", "DataType"]].drop_duplicates("Characteristic").itertuples(index=False)
        }
        # Mapping table is tiny compared with CSV data; split by characteristic to avoid
        # a wide melt that would materialise millions of null rows.
        mapping_by_char: dict[str, pd.DataFrame] = {}
        for characteristic in mapped_characteristics:
            part = local_map.loc[
                local_map["Characteristic"] == characteristic,
                ["TechnicalObject", "SourcePositionId", "Category", "ManagedObjectId", "MeasuringNodeId"],
            ].rename(columns={
                "TechnicalObject": "_TOKey",
                "SourcePositionId": "_PositionKey",
                "Category": "CategoryId",
                "ManagedObjectId": "managedObjectId",
                "MeasuringNodeId": "measuringNodeId",
            }).drop_duplicates()
            mapping_by_char[characteristic] = part

        writer: pq.ParquetWriter | None = None
        source_rows = 0
        selected_rows = 0
        mapped_measurements = 0
        target_rows = 0
        used_mapping_keys: set[tuple[str, str, str, str]] = set()
        seen_target_hashes: set[int] = set()

        usecols = required + mapped_characteristics
        try:
            writer = pq.ParquetWriter(
                temp_output,
                schema,
                compression=self.settings.transform_parquet_compression,
                use_dictionary=True,
            )
            for chunk in pd.read_csv(
                csv_file,
                usecols=usecols,
                chunksize=self.settings.transform_chunk_rows,
                low_memory=False,
            ):
                source_rows += len(chunk)
                chunk["TechnicalObject"] = chunk["TechnicalObject"].astype(str).str.strip()
                chunk["Position"] = chunk["Position"].astype(str).str.strip()
                chunk["CategoryId"] = chunk["CategoryId"].astype(str).str.strip()
                chunk["_TOKey"] = chunk["TechnicalObject"].map(normalize_technical_object)
                chunk["_PositionKey"] = chunk["Position"].map(normalize_position_id)
                chunk = chunk.loc[chunk["_TOKey"].isin(selected_tos)].copy()
                selected_rows += len(chunk)
                if chunk.empty:
                    continue

                pieces: list[pd.DataFrame] = []
                for characteristic in mapped_characteristics:
                    mask = chunk[characteristic].notna()
                    if not mask.any():
                        continue
                    part = chunk.loc[
                        mask,
                        ["_TIME", "_TOKey", "_PositionKey", "CategoryId", characteristic],
                    ].rename(columns={characteristic: "value"})
                    part["characteristic"] = characteristic
                    part = part.merge(
                        mapping_by_char[characteristic],
                        on=["_TOKey", "_PositionKey", "CategoryId"],
                        how="inner",
                        validate="many_to_one",
                    )
                    if part.empty:
                        continue
                    mapped_measurements += len(part)
                    for to_key, position_key, category_id in part[["_TOKey", "_PositionKey", "CategoryId"]].drop_duplicates().itertuples(index=False, name=None):
                        used_mapping_keys.add((str(to_key), str(position_key), str(category_id), characteristic))
                    pieces.append(part)

                if not pieces:
                    continue
                long_df = pd.concat(pieces, ignore_index=True)
                long_df["_time"] = pd.to_datetime(long_df["_TIME"], unit="ms", utc=True, errors="coerce")
                long_df = long_df.dropna(subset=["_time"])
                if long_df.empty:
                    continue

                pivot = (
                    long_df.pivot_table(
                        index=["managedObjectId", "_time", "measuringNodeId"],
                        columns="characteristic",
                        values="value",
                        aggfunc="first",
                    )
                    .reset_index()
                )
                pivot.columns.name = None
                for characteristic in mapped_characteristics:
                    if characteristic not in pivot.columns:
                        pivot[characteristic] = None
                    kind = char_kind.get(characteristic)
                    if kind == "numeric":
                        pivot[characteristic] = pd.to_numeric(pivot[characteristic], errors="coerce").astype("float64")
                    elif kind == "date":
                        pivot[characteristic] = pd.to_datetime(pivot[characteristic], errors="coerce").dt.date
                pivot = pivot.reindex(columns=schema.names)

                # Preserve V2's safety property, but only for actual target rows. The
                # single-pass pipeline still detects cross-chunk duplicate target keys.
                hashes = pd.util.hash_pandas_object(
                    pivot[["managedObjectId", "_time", "measuringNodeId"]], index=False
                )
                current = {int(v) for v in hashes}
                overlap = current & seen_target_hashes
                if overlap:
                    raise RuntimeError(
                        f"Found {len(overlap)} duplicate target key(s) spanning CSV chunks; transformation stopped safely."
                    )
                seen_target_hashes.update(current)

                table = pa.Table.from_pandas(pivot, schema=schema, preserve_index=False, safe=False)
                writer.write_table(table)
                target_rows += len(pivot)

            writer.close()
            writer = None
            if target_rows == 0:
                temp_output.unlink(missing_ok=True)
                output: Path | None = None
                status = "NO_DATA"
            else:
                os.replace(temp_output, output_file)
                check = pq.ParquetFile(output_file)
                if check.metadata.num_rows != target_rows:
                    raise RuntimeError("Parquet row-count validation failed")
                output = output_file
                status = "SUCCESS"
        except Exception:
            if writer is not None:
                writer.close()
            temp_output.unlink(missing_ok=True)
            raise

        return self._summary_result(
            csv_file,
            output,
            source_rows,
            selected_rows,
            mapped_measurements,
            target_rows,
            len(used_mapping_keys),
            0,
            status,
        )

    @staticmethod
    def _build_schema(mapping_df: pd.DataFrame, mapped_characteristics: list[str]) -> pa.Schema:
        fields: list[pa.Field] = [
            pa.field("managedObjectId", pa.string()),
            pa.field("measuringNodeId", pa.string()),
            pa.field("_time", pa.timestamp("ns", tz="UTC")),
        ]
        for characteristic in mapped_characteristics:
            types = mapping_df.loc[
                mapping_df["Characteristic"] == characteristic, "DataType"
            ].dropna().astype(str).unique()
            kinds = {_data_kind(v) for v in types}
            kinds.discard(None)
            if len(kinds) != 1:
                raise RuntimeError(f"Characteristic {characteristic} has conflicting target data types: {types.tolist()}")
            kind = next(iter(kinds))
            fields.append(pa.field(characteristic, pa.float64() if kind == "numeric" else pa.date32()))
        return pa.schema(fields)

    @staticmethod
    def _summary_result(
        csv_file: Path,
        output_file: Path | None,
        source_rows_scanned: int,
        selected_source_rows: int,
        mapped_measurements: int,
        parquet_rows: int,
        mapped_combinations: int,
        failed_combinations: int,
        status: str,
    ) -> dict[str, Any]:
        return {
            "csv_file": str(csv_file),
            "output_file": str(output_file) if output_file else None,
            "source_rows_scanned": int(source_rows_scanned),
            "selected_source_rows": int(selected_source_rows),
            "mapped_measurements": int(mapped_measurements),
            "parquet_rows": int(parquet_rows),
            "mapped_combinations": int(mapped_combinations),
            "failed_combinations": int(failed_combinations),
            "status": status,
        }

    def _rebuild_transform_reports(self, run_id: str, skipped_df: pd.DataFrame | None = None) -> None:
        paths = self.paths(run_id)
        items = self.db.list_transform_items(run_id)
        summary_rows = [{
            "csv_file": i["source_csv"],
            "output_file": i.get("output_parquet"),
            "source_rows_scanned": i.get("source_rows") or 0,
            "selected_source_rows": i.get("selected_rows") or 0,
            "mapped_measurements": i.get("mapped_measurements") or 0,
            "parquet_rows": i.get("target_rows") or 0,
            "mapped_combinations": i.get("mapped_combinations") or 0,
            "failed_combinations": i.get("skipped_combinations") or 0,
            "status": i.get("status"),
            "error": i.get("error"),
        } for i in items]
        summary = pd.DataFrame(summary_rows)
        summary.to_csv(paths.reports / "transformation_summary.csv", index=False)

        mapping_path = paths.reports / "mapping_catalog.csv"
        if mapping_path.exists():
            shutil.copy2(mapping_path, paths.reports / "mapping_report.csv")
        if skipped_df is None:
            skipped_path = paths.reports / "mapping_catalog_skipped.csv"
            try:
                skipped_df = (
                    pd.read_csv(skipped_path)
                    if skipped_path.exists() and skipped_path.stat().st_size
                    else pd.DataFrame()
                )
            except pd.errors.EmptyDataError:
                skipped_df = pd.DataFrame()
                skipped_df.to_csv(paths.reports / "skipped_mapping_report.csv", index=False)

        mapping_df = pd.read_csv(mapping_path) if mapping_path.exists() and mapping_path.stat().st_size else pd.DataFrame()
        overall = {
            "run_id": run_id,
            "source_files_registered": len(items),
            "successful_files": int(sum(i.get("status") == "SUCCESS" for i in items)),
            "failed_files": int(sum(i.get("status") == "FAILED" for i in items)),
            "pending_files": int(sum(i.get("status") in {"READY", "RUNNING"} for i in items)),
            "selected_source_rows": int(sum(int(i.get("selected_rows") or 0) for i in items)),
            "mapped_measurements": int(sum(int(i.get("mapped_measurements") or 0) for i in items)),
            "target_rows": int(sum(int(i.get("target_rows") or 0) for i in items)),
            "mapped_combinations": int(len(mapping_df)),
            "skipped_combinations": int(len(skipped_df)),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        (paths.reports / "transformation_overall.json").write_text(json.dumps(overall, indent=2), encoding="utf-8")

    def _update_transform_run_status(self, run_id: str) -> None:
        items = self.db.list_transform_items(run_id)
        if not items:
            self.db.update_run(run_id, transform_status="NOT_STARTED")
            return
        statuses = [i["status"] for i in items]
        if any(s == "RUNNING" for s in statuses):
            status = "RUNNING"
        elif all(s in {"SUCCESS", "NO_DATA"} for s in statuses):
            status = "COMPLETE"
        elif any(s in {"FAILED", "NO_MAPPINGS"} for s in statuses) and any(s in {"SUCCESS", "NO_DATA"} for s in statuses):
            status = "PARTIAL_WITH_ISSUES"
        elif any(s in {"FAILED", "NO_MAPPINGS"} for s in statuses):
            status = "NEEDS_ATTENTION"
        elif any(s in {"SUCCESS", "NO_DATA"} for s in statuses):
            status = "PARTIAL"
        else:
            status = "READY"
        self.db.update_run(run_id, transform_status=status, message=None)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def create_validation_sample(
        self,
        run_id: str,
        rows: int = 10,
        item_ids: Iterable[int] | None = None,
    ) -> tuple[Path, pd.DataFrame]:
        run = self._require_run(run_id)
        if run["paused"]:
            raise RuntimeError("Resume the run before creating a validation sample.")
        if rows < 1:
            raise ValueError("Validation rows must be at least 1.")
        items = [i for i in self.db.list_transform_items(run_id) if i["status"] == "SUCCESS" and i.get("output_parquet")]
        if item_ids is not None:
            wanted = {int(v) for v in item_ids}
            items = [i for i in items if i["id"] in wanted]
        if not items:
            raise RuntimeError("Select at least one successfully transformed file.")

        paths = self.paths(run_id)
        existing = self.db.list_uploads(run_id, "VALIDATION")
        if any(u.get("file_id") for u in existing):
            raise RuntimeError("A validation sample has already been uploaded for this run.")
        self.db.delete_unstarted_uploads(run_id, "VALIDATION")
        for old in paths.validation.glob("customer_validation_*_rows.parquet"):
            old.unlink(missing_ok=True)

        # Small validation sample: pandas is intentional here because it aligns
        # different characteristic columns across multiple selected Parquets.
        pieces: list[pd.DataFrame] = []
        remaining = int(rows)
        per_file = max(1, (rows + len(items) - 1) // len(items))
        for item in items:
            if remaining <= 0:
                break
            parquet_path = Path(item["output_parquet"])
            if not parquet_path.exists():
                continue
            parquet = pq.ParquetFile(parquet_path)
            take = min(per_file, remaining, parquet.metadata.num_rows)
            if take <= 0:
                continue
            frame = parquet.read().slice(0, take).to_pandas()
            if not frame.empty:
                pieces.append(frame)
                remaining -= len(frame)
        if not pieces:
            raise RuntimeError("The selected transformed files contain no rows.")

        sample_df = pd.concat(pieces, ignore_index=True, sort=False).head(rows)
        sample_path = paths.validation / f"customer_validation_{len(sample_df)}_rows.parquet"
        pq.write_table(
            pa.Table.from_pandas(sample_df, preserve_index=False),
            sample_path,
            compression=self.settings.upload_parquet_compression,
        )

        key_columns = ["managedObjectId", "measuringNodeId", "_time"]
        characteristic_columns = [c for c in sample_df.columns if c.startswith("C_")]
        preview = sample_df.melt(
            id_vars=key_columns,
            value_vars=characteristic_columns,
            var_name="Characteristic",
            value_name="Value",
        ).dropna(subset=["Value"])

        mapping_path = paths.reports / "mapping_report.csv"
        if mapping_path.exists() and mapping_path.stat().st_size:
            mapping = pd.read_csv(mapping_path).rename(columns={
                "ManagedObjectId": "managedObjectId",
                "MeasuringNodeId": "measuringNodeId",
            })
            wanted_cols = [
                "managedObjectId", "measuringNodeId", "Characteristic",
                "TechnicalObject", "TechnicalObjectType", "TargetSSID",
                "SourcePositionId", "PositionName", "TargetPositionId",
                "Category", "CharacteristicId", "CharacteristicName",
                "DataType", "UnitOfMeasure",
            ]
            available = [c for c in wanted_cols if c in mapping.columns]
            mapping = mapping[available].drop_duplicates()
            if all(c in mapping.columns for c in ["managedObjectId", "measuringNodeId", "Characteristic"]):
                preview = preview.merge(
                    mapping,
                    on=["managedObjectId", "measuringNodeId", "Characteristic"],
                    how="left",
                )

        preview.to_csv(paths.validation / "validation_preview.csv", index=False)
        self.db.upsert_upload(
            run_id,
            "VALIDATION",
            str(sample_path),
            sample_path.name,
            status="READY",
            number_of_records=len(sample_df),
        )
        self.db.update_run(run_id, validation_status="READY", customer_approved=False, production_approved=False)
        self.db.add_event(run_id, "VALIDATION", "Validation sample created", f"{len(sample_df)} row(s) from {len(pieces)} selected transformed file(s).")
        return sample_path, preview

    def upload_validation_sample(self, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        if run["paused"]:
            raise RuntimeError("Resume the run before uploading validation data.")
        uploads = self.db.list_uploads(run_id, "VALIDATION")
        ready = [u for u in uploads if u["status"] in {"READY", "Processing Failed", "FAILED"}]
        if not ready:
            if uploads:
                return uploads[-1]
            raise RuntimeError("Create a validation sample first.")
        upload = ready[-1]
        response = self.eiot.upload_file(upload["file_path"])
        self.db.upsert_upload(
            run_id,
            "VALIDATION",
            upload["file_path"],
            upload["file_name"],
            file_id=response.get("fileId"),
            status="UPLOADED",
            description="File accepted by EIoT; waiting for processing.",
            number_of_records=upload.get("number_of_records"),
            uploaded_time=response.get("uploadedTime"),
            error=None,
            last_checked_at=datetime.now(timezone.utc).isoformat(),
        )
        self.db.update_run(run_id, validation_status="PROCESSING")
        self.db.add_event(run_id, "VALIDATION", "Validation sample uploaded", str(response.get("fileId") or ""))
        return response

    def refresh_upload_statuses(
        self,
        run_id: str,
        phase: str,
        progress: ProgressCallback = _noop_progress,
    ) -> list[dict[str, Any]]:
        run = self._require_run(run_id)
        if run["paused"]:
            return self.db.list_uploads(run_id, phase)
        uploads = self.db.list_uploads(run_id, phase)
        active = [
            u for u in uploads
            if u.get("file_id") and u["status"] not in {"Processed", "Processing Failed"}
        ]

        def check(upload: dict[str, Any]):
            try:
                return upload, self.eiot.get_file_status(upload["file_id"]), None
            except Exception as exc:
                return upload, None, str(exc)[:2500]

        if active:
            with ThreadPoolExecutor(max_workers=self.settings.status_workers) as pool:
                futures = [pool.submit(check, u) for u in active]
                for index, future in enumerate(as_completed(futures), start=1):
                    upload, payload, error = future.result()
                    progress(index, len(active), f"Checking EIoT file statuses ({index}/{len(active)})")
                    if payload:
                        new_status = str(payload.get("status") or payload.get("Status") or upload["status"])
                        status_map = {
                            "processed": "Processed",
                            "processing failed": "Processing Failed",
                            "processing": "Processing",
                        }

                        new_status = status_map.get(
                            new_status.strip().lower(),
                            new_status.strip(),
                        )
                        self.db.update_upload_by_file_id(
                            upload["file_id"],
                            status=new_status,
                            description=payload.get("description") or payload.get("Description"),
                            number_of_records=payload.get("numberOfRecords") or payload.get("Number of Records") or upload.get("number_of_records"),
                            processing_end_time=payload.get("processingEndTime") or payload.get("Processing End Time"),
                            error=None,
                            last_checked_at=datetime.now(timezone.utc).isoformat(),
                        )
                    elif error:
                        self.db.update_upload_by_file_id(
                            upload["file_id"], error=error,
                            last_checked_at=datetime.now(timezone.utc).isoformat(),
                        )

        updated = self.db.list_uploads(run_id, phase)
        if phase == "VALIDATION":
            statuses = {u["status"] for u in updated}
            if statuses and statuses <= {"Processed"}:
                self.db.update_run(run_id, validation_status="PROCESSED", message=None)
            elif "Processing Failed" in statuses or "FAILED" in statuses:
                self.db.update_run(run_id, validation_status="NEEDS_ATTENTION")
            else:
                self.db.update_run(run_id, validation_status="PROCESSING")
        else:
            self._update_load_status(run_id)
            self._write_load_report(run_id)
        return updated

    def mark_customer_approved(self, run_id: str) -> None:
        uploads = self.db.list_uploads(run_id, "VALIDATION")
        if not uploads or any(u["status"] != "Processed" for u in uploads):
            raise RuntimeError("The validation upload must reach EIoT Processed before customer approval.")
        self.db.update_run(run_id, customer_approved=True, validation_status="APPROVED")
        self.db.add_event(run_id, "VALIDATION", "Customer validation approval recorded")

    # ------------------------------------------------------------------
    # Production preparation and upload — selected files only
    # ------------------------------------------------------------------
    def prepare_production_chunks(
        self,
        run_id: str,
        item_ids: Iterable[int] | None = None,
        progress: ProgressCallback = _noop_progress,
    ) -> pd.DataFrame:
        run = self._require_run(run_id)
        if run["paused"]:
            raise RuntimeError("Resume the run before preparing production files.")
        if not run["customer_approved"]:
            raise RuntimeError("Customer validation approval is required first.")

        existing = self.db.list_uploads(run_id, "PRODUCTION")
        if any(u.get("file_id") for u in existing):
            raise RuntimeError("Production upload has already started. The prepared scope can no longer be rebuilt safely.")

        items = [i for i in self.db.list_transform_items(run_id) if i["status"] == "SUCCESS" and i.get("output_parquet")]
        if item_ids is not None:
            wanted = {int(v) for v in item_ids}
            items = [i for i in items if i["id"] in wanted]
        if not items:
            raise RuntimeError("Select at least one successfully transformed file.")

        paths = self.paths(run_id)
        self.db.delete_unstarted_uploads(run_id, "PRODUCTION")
        if paths.ready.exists():
            shutil.rmtree(paths.ready)
        paths.ready.mkdir(parents=True, exist_ok=True)

        def prepare_one(item: dict[str, Any]) -> list[dict[str, Any]]:
            source = Path(item["output_parquet"])
            try:
                relative = source.relative_to(paths.transformed)
            except ValueError:
                relative = Path(source.name)
            source_hash = hashlib.sha1(str(relative).encode("utf-8")).hexdigest()[:10]
            parquet = pq.ParquetFile(source)
            created: list[dict[str, Any]] = []
            chunk_number = 0
            for batch in parquet.iter_batches(batch_size=self.settings.upload_max_rows):
                table = pa.Table.from_batches([batch], schema=parquet.schema_arrow)
                written = self._write_size_safe_table(
                    table=table,
                    output_dir=paths.ready,
                    stem=f"{_safe_part(source.stem)}_{source_hash}",
                    starting_index=chunk_number,
                    max_bytes=self.settings.upload_max_bytes,
                )
                chunk_number += len(written)
                for path, rows, size_bytes in written:
                    created.append({
                        "ready_file": str(path),
                        "file_name": path.name,
                        "source_file": str(relative),
                        "rows": rows,
                        "size_bytes": size_bytes,
                        "size_mb": round(size_bytes / 1024 / 1024, 3),
                    })
            return created

        manifest: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(self.settings.prepare_workers, len(items))) as pool:
            futures = [pool.submit(prepare_one, item) for item in items]
            for index, future in enumerate(as_completed(futures), start=1):
                manifest.extend(future.result())
                progress(index, len(items), f"Preparing selected production files ({index}/{len(items)})")

        manifest_df = pd.DataFrame(manifest)
        if manifest_df.empty:
            raise RuntimeError("The selected transformed files produced no upload rows.")
        manifest_df = manifest_df.sort_values(["source_file", "file_name"]).reset_index(drop=True)
        manifest_df.to_csv(paths.reports / "upload_manifest.csv", index=False)
        self.db.upsert_uploads_bulk(
            run_id,
            "PRODUCTION",
            [
                {
                    "file_path": item["ready_file"],
                    "file_name": item["file_name"],
                    "status": "READY",
                    "number_of_records": item["rows"],
                    "source_file": item["source_file"],
                    "error": None,
                }
                for item in manifest
            ],
        )
        self.db.update_run(run_id, load_status="READY", production_approved=False, message=None)
        self.db.add_event(
            run_id,
            "LOAD",
            "Selected production files prepared",
            f"{len(manifest_df)} upload-safe file(s), {int(manifest_df['rows'].sum()):,} row(s).",
        )
        return manifest_df

    def _write_size_safe_table(
        self,
        *,
        table: pa.Table,
        output_dir: Path,
        stem: str,
        starting_index: int,
        max_bytes: int,
    ) -> list[tuple[Path, int, int]]:
        if table.num_rows == 0:
            return []

        def write_piece(piece: pa.Table, index_seed: int) -> list[tuple[Path, int, int]]:
            candidate = output_dir / f"{stem}_chunk_{index_seed:05d}.parquet"
            pq.write_table(
                piece,
                candidate,
                compression=self.settings.upload_parquet_compression,
                use_dictionary=True,
            )
            size = candidate.stat().st_size
            if size <= max_bytes:
                return [(candidate, piece.num_rows, size)]
            candidate.unlink(missing_ok=True)
            if piece.num_rows <= 1:
                raise RuntimeError(f"A single-row upload file exceeds {self.settings.upload_max_mb} MB.")

            # Proportional split generally converges in one rewrite and avoids the
            # repeated 50/50 recompression used by V2.
            ratio = max(0.1, min(0.9, (max_bytes / max(size, 1)) * 0.92))
            left_rows = max(1, min(piece.num_rows - 1, int(piece.num_rows * ratio)))
            left = piece.slice(0, left_rows)
            right = piece.slice(left_rows)
            left_written = write_piece(left, index_seed)
            right_written = write_piece(right, index_seed + len(left_written))
            return left_written + right_written

        return write_piece(table, starting_index)

    def approve_production_load(self, run_id: str) -> None:
        run = self._require_run(run_id)
        if not run["customer_approved"]:
            raise RuntimeError("Customer approval is required.")
        if not self.db.list_uploads(run_id, "PRODUCTION"):
            raise RuntimeError("Prepare production upload files first.")
        self.db.update_run(run_id, production_approved=True, load_status="APPROVED")
        self.db.add_event(run_id, "LOAD", "Production load approved")

    def upload_production_batch(
        self,
        run_id: str,
        batch_size: int = 10,
        upload_ids: Iterable[int] | None = None,
        progress: ProgressCallback = _noop_progress,
    ) -> list[dict[str, Any]]:
        run = self._require_run(run_id)
        if run["paused"]:
            raise RuntimeError("Resume the run before uploading production files.")
        if not run["production_approved"]:
            raise RuntimeError("Explicit production approval is required.")
        if batch_size < 1:
            raise ValueError("Batch size must be at least 1.")

        uploads = self.db.list_uploads(run_id, "PRODUCTION")
        # Any submitted file that has not reached a terminal EIoT state locks the
        # next batch. This is deliberately conservative even if SAP introduces a
        # new intermediate status name that the UI has not seen before.
        if any(
            u.get("file_id") and u["status"] not in {"Processed", "Processing Failed"}
            for u in uploads
        ):
            raise RuntimeError("The current batch is still being processed by EIoT.")

        wanted = set(int(v) for v in upload_ids) if upload_ids is not None else None
        pending = [
            u for u in uploads
            if u["status"] == "READY" and (wanted is None or u["id"] in wanted)
        ][:batch_size]
        if not pending:
            raise RuntimeError("Select at least one file that is ready to upload.")

        batch_no = self.db.next_batch_no(run_id)
        self.db.add_event(run_id, "LOAD", f"Production batch {batch_no} started", f"{len(pending)} selected file(s).")

        def upload_one(upload: dict[str, Any]):
            try:
                return upload, self.eiot.upload_file(upload["file_path"]), None
            except Exception as exc:
                return upload, None, str(exc)[:3000]

        with ThreadPoolExecutor(max_workers=min(self.settings.upload_workers, len(pending))) as pool:
            futures = [pool.submit(upload_one, u) for u in pending]
            for index, future in enumerate(as_completed(futures), start=1):
                upload, response, error = future.result()
                progress(index, len(pending), f"Uploading selected files ({index}/{len(pending)})")
                if response:
                    self.db.upsert_upload(
                        run_id,
                        "PRODUCTION",
                        upload["file_path"],
                        upload["file_name"],
                        file_id=response.get("fileId"),
                        status="UPLOADED",
                        description="File accepted by EIoT; waiting for processing.",
                        number_of_records=upload.get("number_of_records"),
                        uploaded_time=response.get("uploadedTime"),
                        error=None,
                        batch_no=batch_no,
                        attempt=int(upload.get("attempt") or 1),
                        source_file=upload.get("source_file"),
                        last_checked_at=datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    self.db.upsert_upload(
                        run_id,
                        "PRODUCTION",
                        upload["file_path"],
                        upload["file_name"],
                        status="FAILED",
                        error=error,
                        batch_no=batch_no,
                        attempt=int(upload.get("attempt") or 1),
                        source_file=upload.get("source_file"),
                    )
        self._update_load_status(run_id)
        self._write_load_report(run_id)
        return self.db.list_uploads(run_id, "PRODUCTION")

    def retry_failed_production(self, run_id: str, upload_ids: Iterable[int] | None = None) -> int:
        uploads = self.db.list_uploads(run_id, "PRODUCTION")
        wanted = set(int(v) for v in upload_ids) if upload_ids is not None else None
        failed = [
            u for u in uploads
            if u["status"] in {"Processing Failed", "FAILED"}
            and (wanted is None or u["id"] in wanted)
        ]
        for u in failed:
            self.db.upsert_upload(
                run_id,
                "PRODUCTION",
                u["file_path"],
                u["file_name"],
                file_id=None,
                status="READY",
                description="Ready for retry",
                error=None,
                batch_no=None,
                attempt=int(u.get("attempt") or 1) + 1,
                source_file=u.get("source_file"),
                processing_end_time=None,
                last_checked_at=None,
            )
        self._update_load_status(run_id)
        self._write_load_report(run_id)
        return len(failed)

    def _update_load_status(self, run_id: str) -> None:
        uploads = self.db.list_uploads(run_id, "PRODUCTION")
        if not uploads:
            self.db.update_run(run_id, load_status="NOT_STARTED")
            return
        statuses = [u["status"] for u in uploads]
        if all(s == "Processed" for s in statuses):
            status = "COMPLETE"
        elif any(s in {"Processing Failed", "FAILED"} for s in statuses):
            status = "ATTENTION_REQUIRED"
        elif any(s in {"UPLOADED", "Received", "Scanned", "In Process"} for s in statuses):
            status = "IN_PROGRESS"
        elif all(s == "READY" for s in statuses):
            status = "READY"
        else:
            status = "PARTIAL"
        self.db.update_run(run_id, load_status=status, message=None)

    def _write_load_report(self, run_id: str) -> None:
        paths = self.paths(run_id)
        uploads = self.db.list_uploads(run_id, "PRODUCTION")
        frame = pd.DataFrame(uploads)
        frame.to_csv(paths.reports / "load_report.csv", index=False)
        friendly_columns = [
            "file_name", "batch_no", "number_of_records", "file_id", "status",
            "description", "uploaded_time", "processing_end_time", "source_file",
            "attempt", "error",
        ]
        if frame.empty:
            pd.DataFrame(columns=friendly_columns).to_csv(paths.reports / "successfully_migrated.csv", index=False)
            pd.DataFrame(columns=friendly_columns).to_csv(paths.reports / "failed_uploads.csv", index=False)
            return
        available = [c for c in friendly_columns if c in frame.columns]
        frame.loc[frame["status"] == "Processed", available].to_csv(
            paths.reports / "successfully_migrated.csv", index=False
        )
        frame.loc[frame["status"].isin(["Processing Failed", "FAILED"]), available].to_csv(
            paths.reports / "failed_uploads.csv", index=False
        )

        transform_path = paths.reports / "transformation_overall.json"
        transform = {}
        if transform_path.exists():
            try:
                transform = json.loads(transform_path.read_text(encoding="utf-8"))
            except Exception:
                transform = {}
        run = self._require_run(run_id)
        total = len(frame)
        processed = int((frame["status"] == "Processed").sum())
        failed = int(frame["status"].isin(["Processing Failed", "FAILED"]).sum())
        final = {
            "run_id": run_id,
            "technical_objects_requested": len(run["technical_objects"]),
            "date_from": run["date_from"],
            "date_to": run["date_to"],
            "transformed_target_rows": transform.get("target_rows"),
            "mapped_measurements": transform.get("mapped_measurements"),
            "mapped_combinations": transform.get("mapped_combinations"),
            "skipped_combinations": transform.get("skipped_combinations"),
            "production_files_prepared": total,
            "successfully_processed_files": processed,
            "failed_files": failed,
            "remaining_or_processing_files": total - processed - failed,
            "migration_status": (
                "COMPLETED" if total > 0 and processed == total
                else "COMPLETED_WITH_ITEMS_REQUIRING_REVIEW" if failed > 0 and processed + failed == total
                else "IN_PROGRESS"
            ),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        (paths.reports / "final_migration_summary.json").write_text(
            json.dumps(final, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Metrics / reporting / cleanup
    # ------------------------------------------------------------------
    def extraction_metrics(self, run_id: str) -> dict[str, int]:
        jobs = [j for j in self.db.list_extraction_jobs(run_id) if j["status"] != "SPLIT"]
        counts: dict[str, int] = {}
        for j in jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1
        terminal = {"Downloaded", "NO_DATA"}
        active = sum(
            1 for j in jobs
            if j["status"] not in {"PLANNED", "Downloaded", "NO_DATA", "FAILED", "Failed", "Exception", "Expired", "Ready for Download"}
        )
        return {
            "total": len(jobs),
            "planned": counts.get("PLANNED", 0),
            "downloaded": counts.get("Downloaded", 0),
            "ready": counts.get("Ready for Download", 0),
            "no_data": counts.get("NO_DATA", 0),
            "failed": sum(counts.get(s, 0) for s in ("FAILED", "Failed", "Exception", "Expired")),
            "preparing": active,
            "complete": sum(1 for j in jobs if j["status"] in terminal),
        }

    def transform_metrics(self, run_id: str) -> dict[str, int]:
        items = self.db.list_transform_items(run_id)
        return {
            "total": len(items),
            "ready": sum(i["status"] == "READY" for i in items),
            "running": sum(i["status"] == "RUNNING" for i in items),
            "success": sum(i["status"] == "SUCCESS" for i in items),
            "no_data": sum(i["status"] == "NO_DATA" for i in items),
            "failed": sum(i["status"] in {"FAILED", "NO_MAPPINGS"} for i in items),
            "target_rows": sum(int(i.get("target_rows") or 0) for i in items),
        }

    def load_metrics(self, run_id: str) -> dict[str, Any]:
        uploads = self.db.list_uploads(run_id, "PRODUCTION")
        counts: dict[str, int] = {}
        for u in uploads:
            counts[u["status"]] = counts.get(u["status"], 0) + 1
        active = sum(
            1 for u in uploads
            if u.get("file_id") and u["status"] not in {"Processed", "Processing Failed"}
        )
        batches = [int(u["batch_no"]) for u in uploads if u.get("batch_no") is not None]
        current_batch = max(batches) if batches else 0
        return {
            "total": len(uploads),
            "processed": counts.get("Processed", 0),
            "active": active,
            "failed": counts.get("Processing Failed", 0) + counts.get("FAILED", 0),
            "ready": counts.get("READY", 0),
            "current_batch": current_batch,
            "current_batch_files": sum(1 for u in uploads if u.get("batch_no") == current_batch) if current_batch else 0,
            "current_batch_rows": sum(int(u.get("number_of_records") or 0) for u in uploads if u.get("batch_no") == current_batch) if current_batch else 0,
            "counts": counts,
        }

    def scope_metrics(self, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        days = (date.fromisoformat(run["date_to"]) - date.fromisoformat(run["date_from"])).days + 1
        discovery_path = self.paths(run_id).reports / "discovery_report.csv"
        unique_positions = None
        if discovery_path.exists() and discovery_path.stat().st_size:
            try:
                df = pd.read_csv(discovery_path)
                unique_positions = int(df["ExtractionId"].dropna().nunique()) if "ExtractionId" in df.columns else None
            except pd.errors.EmptyDataError:
                pass
        jobs = [j for j in self.db.list_extraction_jobs(run_id) if j["status"] != "SPLIT"]
        risk_points = 0
        if len(run["technical_objects"]) >= self.settings.large_scope_to_count:
            risk_points += 1
        if days >= self.settings.large_scope_days:
            risk_points += 1
        if jobs and len(jobs) >= self.settings.large_scope_jobs:
            risk_points += 1
        return {
            "technical_objects": len(run["technical_objects"]),
            "days": days,
            "unique_positions": unique_positions,
            "estimated_jobs": len(jobs) if jobs else None,
            "risk": "HIGH" if risk_points >= 2 else "MEDIUM" if risk_points == 1 else "STANDARD",
        }

    def disk_usage_bytes(self, run_id: str) -> int:
        root = self.paths(run_id).root
        total = 0
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass
        return total

    def cleanup_run(self, run_id: str, *, require_complete: bool = True) -> dict[str, Any]:
        run = self._require_run(run_id)
        if require_complete and run["load_status"] != "COMPLETE":
            raise RuntimeError("Safe cleanup is enabled after the production load completes. Use Abort & Delete Run to remove an unfinished run.")
        paths = self.paths(run_id)
        removed: list[str] = []
        for folder in (paths.extracted, paths.transformed, paths.validation, paths.ready, paths.report_parts):
            if folder.exists():
                shutil.rmtree(folder)
                removed.append(str(folder.relative_to(paths.root)))
        for folder in (paths.extracted, paths.transformed, paths.validation, paths.ready, paths.report_parts):
            folder.mkdir(parents=True, exist_ok=True)
        self.db.add_event(run_id, "CLEANUP", "Temporary migration data cleaned", ", ".join(removed))
        return {"removed": removed, "kept": ["reports", "SQLite audit history"]}

    def run_report_paths(self, run_id: str) -> dict[str, Path]:
        paths = self.paths(run_id)
        candidates = {
            "Discovery report": paths.reports / "discovery_report.csv",
            "Discovery issues": paths.reports / "discovery_issues.csv",
            "Mapping report": paths.reports / "mapping_report.csv",
            "Skipped mappings": paths.reports / "skipped_mapping_report.csv",
            "Transformation summary": paths.reports / "transformation_summary.csv",
            "Transformation overall": paths.reports / "transformation_overall.json",
            "Validation preview": paths.validation / "validation_preview.csv",
            "Prepared upload manifest": paths.reports / "upload_manifest.csv",
            "Successfully migrated": paths.reports / "successfully_migrated.csv",
            "Failed uploads": paths.reports / "failed_uploads.csv",
            "Full load audit": paths.reports / "load_report.csv",
            "Final migration summary": paths.reports / "final_migration_summary.json",
        }
        return {name: path for name, path in candidates.items() if path.exists()}

    @staticmethod
    def human_error(value: str | None) -> str:
        return _human_error(value)

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        if not run:
            raise KeyError(f"Unknown migration run: {run_id}")
        return run
