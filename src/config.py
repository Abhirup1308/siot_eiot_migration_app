from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    # Source Cold Store
    siot_cold_store_client_id: str | None
    siot_cold_store_client_secret: str | None
    siot_cold_store_token_url: str | None
    siot_cold_store_base_url: str | None
    siot_cold_store_download_base_url: str | None
    siot_cold_store_auth_style: str

    # Source IndicatorService
    siot_indicator_client_id: str | None
    siot_indicator_client_secret: str | None
    siot_indicator_token_url: str | None
    siot_indicator_api_key: str | None
    siot_apm_base_url: str | None
    siot_indicator_auth_style: str

    # Target EIoT
    eiot_client_id: str | None
    eiot_client_secret: str | None
    eiot_token_url: str | None
    eiot_api_key: str | None
    eiot_apm_base_url: str | None
    eiot_auth_style: str

    # Runtime
    request_timeout_seconds: int
    export_slice_days: int
    export_min_slice_days: int
    transform_chunk_rows: int
    upload_max_rows: int
    upload_max_mb: int
    auto_poll_seconds: int
    auto_download_ready: bool
    download_chunk_mb: int

    # Performance / bounded concurrency
    discovery_workers: int
    export_workers: int
    status_workers: int
    download_workers: int
    gzip_workers: int
    mapping_workers: int
    transform_workers: int
    prepare_workers: int
    upload_workers: int

    # Parquet codecs
    transform_parquet_compression: str
    upload_parquet_compression: str

    # Scope guardrails
    large_scope_to_count: int
    large_scope_days: int
    large_scope_jobs: int

    # State
    data_dir: Path
    db_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(
            _first_env("MIGRATION_DATA_DIR", default=str(PROJECT_ROOT / "data"))
        ).expanduser().resolve()
        db_path = Path(
            _first_env("MIGRATION_DB_PATH", default=str(data_dir / "migration.sqlite3"))
        ).expanduser().resolve()

        cold_base = _first_env("SIOT_COLD_STORE_BASE_URL")
        return cls(
            siot_cold_store_client_id=_first_env("SIOT_COLD_STORE_CLIENT_ID", "P_SIOT_CLIENT_ID"),
            siot_cold_store_client_secret=_first_env("SIOT_COLD_STORE_CLIENT_SECRET", "P_SIOT_CLIENT_SECRET"),
            siot_cold_store_token_url=_first_env("SIOT_COLD_STORE_TOKEN_URL", "P_SIOT_TOKEN_URL"),
            siot_cold_store_base_url=cold_base,
            siot_cold_store_download_base_url=_first_env(
                "SIOT_COLD_STORE_DOWNLOAD_BASE_URL", default=cold_base
            ),
            siot_cold_store_auth_style=(_first_env("SIOT_COLD_STORE_AUTH_STYLE", default="form") or "form").lower(),

            siot_indicator_client_id=_first_env("SIOT_INDICATOR_CLIENT_ID", "P_SIOT_INDICATOR_CLIENT_ID"),
            siot_indicator_client_secret=_first_env("SIOT_INDICATOR_CLIENT_SECRET", "P_SIOT_INDICATOR_CLIENT_SECRET"),
            siot_indicator_token_url=_first_env("SIOT_INDICATOR_TOKEN_URL", "P_SIOT_INDICATOR_TOKEN_URL"),
            siot_indicator_api_key=_first_env("SIOT_INDICATOR_API_KEY", "SIOT_API_KEY", "PROD_APM_API_KEY"),
            siot_apm_base_url=_first_env(
                "SIOT_APM_BASE_URL",
                default="https://api-apm.prod.apimanagement.eu20.hana.ondemand.com",
            ),
            siot_indicator_auth_style=(_first_env("SIOT_INDICATOR_AUTH_STYLE", default="form") or "form").lower(),

            eiot_client_id=_first_env("EIOT_CLIENT_ID", "P_EIOT_CLIENT_ID"),
            eiot_client_secret=_first_env("EIOT_CLIENT_SECRET", "P_EIOT_CLIENT_SECRET"),
            eiot_token_url=_first_env("EIOT_TOKEN_URL", "P_EIOT_TOKEN_URL"),
            eiot_api_key=_first_env("EIOT_API_KEY", "PROD_APM_API_KEY"),
            eiot_apm_base_url=_first_env(
                "EIOT_APM_BASE_URL",
                default="https://api-apm.prod.apimanagement.eu20.hana.ondemand.com",
            ),
            eiot_auth_style=(_first_env("EIOT_AUTH_STYLE", default="form") or "form").lower(),

            request_timeout_seconds=max(10, _as_int("REQUEST_TIMEOUT_SECONDS", 60)),
            export_slice_days=max(1, _as_int("EXPORT_SLICE_DAYS", 31)),
            export_min_slice_days=max(1, _as_int("EXPORT_MIN_SLICE_DAYS", 1)),
            transform_chunk_rows=max(10_000, _as_int("TRANSFORM_CHUNK_ROWS", 150_000)),
            upload_max_rows=max(1_000, _as_int("UPLOAD_MAX_ROWS", 250_000)),
            upload_max_mb=max(1, _as_int("UPLOAD_MAX_MB", 30)),
            auto_poll_seconds=max(10, _as_int("AUTO_POLL_SECONDS", 10)),
            auto_download_ready=_as_bool("AUTO_DOWNLOAD_READY", True),
            download_chunk_mb=max(1, _as_int("DOWNLOAD_CHUNK_MB", 8)),

            discovery_workers=max(1, _as_int("DISCOVERY_WORKERS", 12)),
            export_workers=max(1, _as_int("EXPORT_WORKERS", 8)),
            status_workers=max(1, _as_int("STATUS_WORKERS", 16)),
            download_workers=max(1, _as_int("DOWNLOAD_WORKERS", 4)),
            gzip_workers=max(1, _as_int("GZIP_WORKERS", 4)),
            mapping_workers=max(1, _as_int("MAPPING_WORKERS", 12)),
            transform_workers=max(1, _as_int("TRANSFORM_WORKERS", 4)),
            prepare_workers=max(1, _as_int("PREPARE_WORKERS", 3)),
            upload_workers=max(1, _as_int("UPLOAD_WORKERS", 4)),

            transform_parquet_compression=(_first_env("TRANSFORM_PARQUET_COMPRESSION", default="snappy") or "snappy").lower(),
            upload_parquet_compression=(_first_env("UPLOAD_PARQUET_COMPRESSION", default="gzip") or "gzip").lower(),

            large_scope_to_count=max(1, _as_int("LARGE_SCOPE_TO_COUNT", 100)),
            large_scope_days=max(1, _as_int("LARGE_SCOPE_DAYS", 31)),
            large_scope_jobs=max(1, _as_int("LARGE_SCOPE_JOBS", 100)),

            data_dir=data_dir,
            db_path=db_path,
        )

    @property
    def upload_max_bytes(self) -> int:
        return self.upload_max_mb * 1024 * 1024

    @property
    def download_chunk_bytes(self) -> int:
        return self.download_chunk_mb * 1024 * 1024

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def missing_required(self) -> list[str]:
        required: Iterable[tuple[str, str | None]] = [
            ("SIOT_COLD_STORE_CLIENT_ID (or P_SIOT_CLIENT_ID)", self.siot_cold_store_client_id),
            ("SIOT_COLD_STORE_CLIENT_SECRET (or P_SIOT_CLIENT_SECRET)", self.siot_cold_store_client_secret),
            ("SIOT_COLD_STORE_TOKEN_URL", self.siot_cold_store_token_url),
            ("SIOT_COLD_STORE_BASE_URL", self.siot_cold_store_base_url),
            ("SIOT_COLD_STORE_DOWNLOAD_BASE_URL", self.siot_cold_store_download_base_url),
            ("SIOT_INDICATOR_CLIENT_ID (or P_SIOT_INDICATOR_CLIENT_ID)", self.siot_indicator_client_id),
            ("SIOT_INDICATOR_CLIENT_SECRET (or P_SIOT_INDICATOR_CLIENT_SECRET)", self.siot_indicator_client_secret),
            ("SIOT_INDICATOR_TOKEN_URL", self.siot_indicator_token_url),
            ("SIOT_INDICATOR_API_KEY", self.siot_indicator_api_key),
            ("SIOT_APM_BASE_URL", self.siot_apm_base_url),
            ("EIOT_CLIENT_ID (or P_EIOT_CLIENT_ID)", self.eiot_client_id),
            ("EIOT_CLIENT_SECRET (or P_EIOT_CLIENT_SECRET)", self.eiot_client_secret),
            ("EIOT_TOKEN_URL", self.eiot_token_url),
            ("EIOT_API_KEY", self.eiot_api_key),
            ("EIOT_APM_BASE_URL", self.eiot_apm_base_url),
        ]
        return [name for name, value in required if not value]

    def public_summary(self) -> dict[str, str | int | bool]:
        return {
            "SIoT APM base": self.siot_apm_base_url or "",
            "Cold Store base": self.siot_cold_store_base_url or "",
            "EIoT APM base": self.eiot_apm_base_url or "",
            "Export slice days": self.export_slice_days,
            "Minimum adaptive slice": self.export_min_slice_days,
            "CSV chunk rows": self.transform_chunk_rows,
            "Upload max rows": self.upload_max_rows,
            "Upload max MB": self.upload_max_mb,
            "Auto-poll seconds": self.auto_poll_seconds,
            "Auto-download": self.auto_download_ready,
            "Discovery workers": self.discovery_workers,
            "Export workers": self.export_workers,
            "Status workers": self.status_workers,
            "Download workers": self.download_workers,
            "Mapping workers": self.mapping_workers,
            "Transform workers": self.transform_workers,
            "Upload workers": self.upload_workers,
            "Intermediate Parquet codec": self.transform_parquet_compression,
            "Upload Parquet codec": self.upload_parquet_compression,
            "Data directory": str(self.data_dir),
            "Database": str(self.db_path),
        }
