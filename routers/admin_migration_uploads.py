from __future__ import annotations

import socket
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Header, HTTPException
from pydantic import BaseModel, Field
import httpx

import settings
from services.upload_migration_service import migrate_uploads_from_source


router = APIRouter(prefix="/admin/migration/uploads", tags=["admin-migration"])


class PullUploadsRequest(BaseModel):
    overwrite: bool = Field(default=False)
    limit: int | None = Field(default=None, ge=1)
    dry_run: bool = Field(default=False)

@router.get("/diagnose-source")
def diagnose_source(timeout_sec: float = 5.0):
    """
    (임시) B 서버에서 A 서버로 네트워크가 가능한지 진단합니다.
    - DNS resolve 결과
    - A 루트(/) 요청 시도
    - A 파일목록 API 요청 시도
    """
    source_base = (settings.SMARTGAUGE_SOURCE_BASE_URL or "").rstrip("/")
    files_ep = (settings.SMARTGAUGE_SOURCE_FILES_ENDPOINT or "").strip()
    if files_ep and not files_ep.startswith("/"):
        files_ep = "/" + files_ep
    source_files_url = f"{source_base}{files_ep}"

    # DNS 진단
    parsed = urlparse(source_base)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    dns_addrs: list[str] = []
    dns_error: str | None = None
    if host:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            dns_addrs = sorted({info[4][0] for info in infos if info and info[4]})
        except Exception as e:
            dns_error = str(e) or e.__class__.__name__

    headers: dict[str, str] = {}
    token = (settings.SMARTGAUGE_SOURCE_MIGRATION_TOKEN or "").strip()
    if token:
        headers["x_migration_token"] = token

    # HTTP 진단(짧은 timeout)
    t = max(0.1, float(timeout_sec))
    timeout = httpx.Timeout(timeout=t, connect=t, read=t, write=t, pool=t)
    result: dict[str, object] = {
        "source_base_url": settings.SMARTGAUGE_SOURCE_BASE_URL,
        "source_files_endpoint": settings.SMARTGAUGE_SOURCE_FILES_ENDPOINT,
        "source_files_url": source_files_url,
        "dns": {"host": host, "port": port, "addrs": dns_addrs, "error": dns_error},
        "timeout_sec": t,
    }

    def _probe(url: str) -> dict[str, object]:
        started = time.time()
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                r = client.get(url, headers=headers)
            elapsed_ms = int((time.time() - started) * 1000)
            return {
                "ok": True,
                "status_code": r.status_code,
                "elapsed_ms": elapsed_ms,
                "content_type": r.headers.get("content-type", ""),
                "body_prefix": (r.text or "")[:200],
            }
        except Exception as e:
            elapsed_ms = int((time.time() - started) * 1000)
            return {
                "ok": False,
                "elapsed_ms": elapsed_ms,
                "error": e.__class__.__name__,
                "message": str(e)[:300],
            }

    # 1) base URL probe
    result["probe_base"] = _probe(source_base + "/")
    # 2) files API probe
    result["probe_files"] = _probe(source_files_url)
    return result


@router.post("/pull-from-smartgauge")
def pull_from_smartgauge(
    payload: PullUploadsRequest = Body(default=PullUploadsRequest()),
    x_migration_token: str | None = Header(default=None),
):
    """
    (임시) A 서버 업로드 파일을 B 서버 /data/uploads(STATIC_DIR)로 당겨오는 1회성 API
    - 순차 다운로드
    - stream download
    - overwrite/limit/dry_run 지원

    보안:
    - 환경변수 MIGRATION_TOKEN이 설정된 경우, 요청 헤더 x_migration_token 값이 일치해야 합니다.
    """
    token = (getattr(settings, "MIGRATION_TOKEN", "") or "").strip()
    if token and x_migration_token != token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    source_base = (settings.SMARTGAUGE_SOURCE_BASE_URL or "").rstrip("/")
    files_ep = (settings.SMARTGAUGE_SOURCE_FILES_ENDPOINT or "").strip()
    if files_ep and not files_ep.startswith("/"):
        files_ep = "/" + files_ep
    source_files_url = f"{source_base}{files_ep}"

    try:
        return migrate_uploads_from_source(
            source_base_url=settings.SMARTGAUGE_SOURCE_BASE_URL,
            source_files_endpoint=settings.SMARTGAUGE_SOURCE_FILES_ENDPOINT,
            source_uploads_base_url=settings.SMARTGAUGE_SOURCE_UPLOADS_BASE_URL,
            migration_token=settings.SMARTGAUGE_SOURCE_MIGRATION_TOKEN,
            overwrite=payload.overwrite,
            limit=payload.limit,
            dry_run=payload.dry_run,
            timeout_sec=settings.SMARTGAUGE_HTTP_TIMEOUT_SEC,
        )
    except httpx.TimeoutException as e:
        raise HTTPException(
            status_code=504,
            detail={
                "error": "timeout",
                "message": str(e) or "timeout",
                "source_files_url": source_files_url,
            },
        ) from e
    except (ValueError, RuntimeError) as e:
        # 소스 목록 실패/파싱 실패 등을 5xx로 노출 (500으로 죽지 않게)
        msg = str(e) or e.__class__.__name__
        is_timeout = "timeout" in msg.lower()
        raise HTTPException(
            status_code=504 if is_timeout else 502,
            detail={
                "error": e.__class__.__name__,
                "message": msg,
                "source_base_url": settings.SMARTGAUGE_SOURCE_BASE_URL,
                "source_files_endpoint": settings.SMARTGAUGE_SOURCE_FILES_ENDPOINT,
                "source_files_url": source_files_url,
                "source_uploads_base_url": settings.SMARTGAUGE_SOURCE_UPLOADS_BASE_URL,
                "hint": "B 서버에서 A로 HTTPS(443) 아웃바운드가 가능한지 확인하고, SMARTGAUGE_SOURCE_FILES_ENDPOINT가 '/admin/migration/uploads/files'인지 확인하세요. 빠른 진단: GET /admin/migration/uploads/diagnose-source",
            },
        ) from e

