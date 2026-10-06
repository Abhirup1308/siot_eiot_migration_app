from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from .config import Settings


def _odata_escape(value: str) -> str:
    return str(value).replace("'", "''")


class ThreadLocalSessions:
    """Reusable HTTP sessions per worker thread.

    Reusing TCP/TLS connections is materially faster than requests.request() for
    workloads that call SAP APIs hundreds or thousands of times.
    """

    def __init__(self, pool_size: int = 32):
        self.pool_size = max(4, int(pool_size))
        self._local = threading.local()

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=self.pool_size,
                pool_maxsize=self.pool_size,
                max_retries=0,
                pool_block=True,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session


class OAuthClient:
    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        timeout: int = 60,
        auth_style: str = "form",
        sessions: ThreadLocalSessions | None = None,
    ):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.auth_style = auth_style.lower()
        self.sessions = sessions or ThreadLocalSessions()
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.RLock()

    def get_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._token and time.time() < self._expires_at:
                return self._token

            data: dict[str, str] = {"grant_type": "client_credentials"}
            kwargs: dict[str, Any] = {"data": data, "timeout": self.timeout}
            if self.auth_style == "basic":
                kwargs["auth"] = (self.client_id, self.client_secret)
            else:
                data["client_id"] = self.client_id
                data["client_secret"] = self.client_secret

            response = self.sessions.get().post(self.token_url, **kwargs)
            response.raise_for_status()
            payload = response.json()
            self._token = payload["access_token"]
            expires_in = int(payload.get("expires_in", 300))
            self._expires_at = time.time() + max(30, expires_in - 45)
            return self._token


class BearerAPI:
    def __init__(
        self,
        oauth: OAuthClient,
        api_key: str | None,
        timeout: int,
        sessions: ThreadLocalSessions,
    ):
        self.oauth = oauth
        self.api_key = api_key
        self.timeout = timeout
        self.sessions = sessions

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        stream: bool = False,
        max_attempts: int = 3,
    ) -> requests.Response:
        last_response: requests.Response | None = None
        for attempt in range(1, max_attempts + 1):
            headers = {
                "Authorization": f"Bearer {self.oauth.get_token()}",
                "Accept": "application/json",
            }
            if self.api_key:
                headers["x-api-key"] = self.api_key
            if json is not None:
                headers["Content-Type"] = "application/json"

            response = self.sessions.get().request(
                method,
                url,
                headers=headers,
                params=params,
                files=files,
                json=json,
                data=data,
                timeout=self.timeout,
                stream=stream,
            )
            last_response = response

            if response.status_code == 401 and attempt < max_attempts:
                self.oauth.get_token(force_refresh=True)
                continue
            if response.status_code == 429 and attempt < max_attempts:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else float(attempt * 2)
                except ValueError:
                    wait = float(attempt * 2)
                time.sleep(min(wait, 12))
                continue
            if 500 <= response.status_code < 600 and attempt < max_attempts:
                time.sleep(min(attempt * 2, 8))
                continue

            response.raise_for_status()
            return response

        assert last_response is not None
        last_response.raise_for_status()
        return last_response


class SIoTIndicatorClient(BearerAPI):
    def __init__(self, settings: Settings):
        sessions = ThreadLocalSessions(max(settings.discovery_workers, settings.mapping_workers) + 4)
        oauth = OAuthClient(
            settings.siot_indicator_token_url or "",
            settings.siot_indicator_client_id or "",
            settings.siot_indicator_client_secret or "",
            settings.request_timeout_seconds,
            settings.siot_indicator_auth_style,
            sessions=sessions,
        )
        super().__init__(oauth, settings.siot_indicator_api_key, settings.request_timeout_seconds, sessions)
        base = (settings.siot_apm_base_url or "").rstrip("/")
        self.endpoint = f"{base}/IndicatorService/v1/Indicators"

    def get_indicators(
        self,
        technical_object_number: str,
        position_details_id: str | None = None,
    ) -> list[dict[str, Any]]:
        filters = [f"technicalObject_number eq '{_odata_escape(technical_object_number)}'"]
        if position_details_id:
            position_details_id = str(position_details_id).removeprefix("P_")
            filters.append(f"positionDetails_ID eq '{_odata_escape(position_details_id)}'")
        response = self.request(
            "GET",
            self.endpoint,
            params={
                "$filter": " and ".join(filters),
                "$expand": "characteristics,positionDetails",
            },
        )
        return response.json().get("value", []) or []


class EIoTClient(BearerAPI):
    def __init__(self, settings: Settings):
        sessions = ThreadLocalSessions(max(settings.mapping_workers, settings.upload_workers, settings.status_workers) + 4)
        oauth = OAuthClient(
            settings.eiot_token_url or "",
            settings.eiot_client_id or "",
            settings.eiot_client_secret or "",
            settings.request_timeout_seconds,
            settings.eiot_auth_style,
            sessions=sessions,
        )
        super().__init__(oauth, settings.eiot_api_key, settings.request_timeout_seconds, sessions)
        base = (settings.eiot_apm_base_url or "").rstrip("/")
        self.indicator_endpoint = f"{base}/IndicatorService/v1/Indicators"
        self.metadata_base = f"{base}/EIoTMetadataSyncService/v1"
        self.file_upload_base = f"{base}/FileUploadService"

    def get_indicators(self, technical_object_number: str) -> list[dict[str, Any]]:
        """Fetch all target indicators for one TO in a single request.

        V3 uses this for bulk mapping so we do not perform one IndicatorService
        request for every source characteristic/position combination.
        """
        params = {
            "$filter": f"technicalObject_number eq '{_odata_escape(technical_object_number)}'",
            "$expand": "characteristics,positionDetails",
        }
        return self.request("GET", self.indicator_endpoint, params=params).json().get("value", []) or []

    def search_indicator(
        self,
        technical_object_number: str,
        category_name: str,
        characteristics_internal_id: str,
        position_name: str,
    ) -> list[dict[str, Any]]:
        params = {
            "$filter": (
                f"technicalObject_number eq '{_odata_escape(technical_object_number)}' "
                f"and characteristics_characteristicsInternalId eq '{_odata_escape(characteristics_internal_id)}' "
                f"and category_name eq '{_odata_escape(category_name)}' "
                f"and positionDetails/name eq '{_odata_escape(position_name)}'"
            ),
            "$expand": "characteristics,positionDetails",
        }
        return self.request("GET", self.indicator_endpoint, params=params).json().get("value", []) or []

    def get_technical_object_metadata(
        self,
        *,
        number: str,
        ssid: str,
        to_type: str,
    ) -> dict[str, Any]:
        """Fetch a target TO and all metadata indicators in one request."""
        url = (
            f"{self.metadata_base}/TechnicalObjects("
            f"number='{_odata_escape(number)}',"
            f"SSID='{_odata_escape(ssid)}',"
            f"type='{_odata_escape(to_type)}'"
            ")"
        )
        return self.request("GET", url, params={"$expand": "indicators"}).json()

    def get_indicator_metadata(
        self,
        *,
        number: str,
        ssid: str,
        to_type: str,
        position_details_id: str,
        category_name: str,
        characteristics_internal_id: str,
    ) -> dict[str, Any]:
        url = (
            f"{self.metadata_base}/TechnicalObjects("
            f"number='{_odata_escape(number)}',"
            f"SSID='{_odata_escape(ssid)}',"
            f"type='{_odata_escape(to_type)}'"
            ")"
        )
        params = {
            "$expand": (
                "indicators($filter="
                f"positionDetailsId eq '{_odata_escape(position_details_id)}' "
                f"and categoryName eq '{_odata_escape(category_name)}' "
                f"and characteristicsInternalId eq '{_odata_escape(characteristics_internal_id)}'"
                ")"
            )
        }
        return self.request("GET", url, params=params).json()

    def upload_file(self, parquet_file_and_path: str | Path) -> dict[str, Any]:
        path = Path(parquet_file_and_path)
        url = f"{self.file_upload_base}/v1/upload"
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                with path.open("rb") as handle:
                    response = self.request(
                        "POST",
                        url,
                        files={"file": (path.name, handle, "application/octet-stream")},
                        max_attempts=1,
                    )
                return response.json()
            except Exception as exc:
                last_exc = exc
                if attempt >= 3:
                    raise
                time.sleep(min(attempt * 2, 5))
        assert last_exc is not None
        raise last_exc

    def get_file_status(self, file_id: str) -> dict[str, Any]:
        url = f"{self.file_upload_base}/v1/files/status('{_odata_escape(file_id)}')"
        return self.request("GET", url).json()


class SIoTColdStoreClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions = ThreadLocalSessions(max(settings.download_workers, settings.status_workers, settings.export_workers) + 4)
        self.oauth = OAuthClient(
            settings.siot_cold_store_token_url or "",
            settings.siot_cold_store_client_id or "",
            settings.siot_cold_store_client_secret or "",
            settings.request_timeout_seconds,
            settings.siot_cold_store_auth_style,
            sessions=self.sessions,
        )
        self.timeout = settings.request_timeout_seconds
        self.base_url = (settings.siot_cold_store_base_url or "").rstrip("/")
        self.download_base_url = (
            settings.siot_cold_store_download_base_url
            or settings.siot_cold_store_base_url
            or ""
        ).rstrip("/")

    def _request(
        self,
        method: str,
        url: str,
        *,
        stream: bool = False,
        extra_headers: dict[str, str] | None = None,
        max_attempts: int = 3,
    ) -> requests.Response:
        last_response: requests.Response | None = None
        for attempt in range(1, max_attempts + 1):
            headers = {
                "Authorization": f"Bearer {self.oauth.get_token()}",
                "Accept": "application/octet-stream" if stream else "application/json",
            }
            if extra_headers:
                headers.update(extra_headers)
            response = self.sessions.get().request(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
                stream=stream,
            )
            last_response = response
            if response.status_code == 401 and attempt < max_attempts:
                self.oauth.get_token(force_refresh=True)
                continue
            if response.status_code == 429 and attempt < max_attempts:
                time.sleep(min(attempt * 2, 10))
                continue
            if 500 <= response.status_code < 600 and attempt < max_attempts:
                time.sleep(min(attempt * 2, 8))
                continue
            return response
        assert last_response is not None
        return last_response

    def initiate_export(self, indicator_group: str, start_date: str, end_date: str) -> str:
        url = (
            f"{self.base_url}/v1/InitiateDataExport/{indicator_group}"
            f"?timerange={start_date}-{end_date}"
        )
        response = self._request("POST", url)
        payload: dict[str, Any] = {}
        try:
            payload = response.json()
        except ValueError:
            pass
        if response.status_code not in {200, 202, 208}:
            response.raise_for_status()
        request_id = payload.get("RequestId") or payload.get("requestId")
        if not request_id:
            raise RuntimeError(
                f"Cold Store initiate returned HTTP {response.status_code} without RequestId: "
                f"{response.text[:1200]}"
            )
        return str(request_id)

    def get_export_status(self, request_id: str) -> str:
        url = f"{self.base_url}/v1/DataExportStatus?requestId={request_id}"
        response = self._request("GET", url)
        response.raise_for_status()
        payload = response.json()
        status = payload.get("Status") or payload.get("status")
        if status is None:
            return "Unknown"
        text = str(status).strip()
        low = text.lower()
        if text == "The file is available for download." or ("available" in low and "download" in low) or text == "Ready for Download":
            return "Ready for Download"
        if "no data" in low or "data not found" in low:
            return "NO_DATA"
        return text

    def download_export(self, request_id: str, destination: str | Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_suffix(destination.suffix + ".part")
        url = f"{self.download_base_url}/v1/DownloadData('{_odata_escape(request_id)}')"

        downloaded = temp_path.stat().st_size if temp_path.exists() else 0
        etag: str | None = None
        total_size: int | None = None

        for _attempt in range(1, 6):
            extra_headers: dict[str, str] = {}
            if downloaded:
                extra_headers["Range"] = f"bytes={downloaded}-"
                if etag:
                    extra_headers["If-Match"] = etag

            response = self._request("GET", url, stream=True, extra_headers=extra_headers)
            response.raise_for_status()
            etag = response.headers.get("ETag") or response.headers.get("Etag") or etag

            if downloaded and response.status_code == 200:
                downloaded = 0
                temp_path.unlink(missing_ok=True)

            content_range = response.headers.get("Content-Range")
            if content_range and "/" in content_range:
                try:
                    total_size = int(content_range.rsplit("/", 1)[1])
                except ValueError:
                    total_size = None
            elif response.headers.get("Content-Length"):
                try:
                    total_size = downloaded + int(response.headers["Content-Length"])
                except ValueError:
                    total_size = None

            mode = "ab" if downloaded else "wb"
            try:
                with temp_path.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=self.settings.download_chunk_bytes):
                        if chunk:
                            handle.write(chunk)
                            downloaded += len(chunk)
            except requests.RequestException:
                continue

            if total_size is None or downloaded >= total_size:
                os.replace(temp_path, destination)
                return destination

        raise RuntimeError(f"Cold Store download could not complete after resume attempts: {request_id}")
