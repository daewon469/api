from __future__ import annotations

import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException
from pydantic import BaseModel, Field
import httpx

import settings
from services.upload_migration_service import (
    migrate_uploads_from_source,
    fetch_source_file_list,
    download_single_file,
)


router = APIRouter(prefix="/admin/migration/uploads", tags=["admin-migration"])

# ---------------------------------------------------------------------------
# 백그라운드 스캔 상태 (in-memory singleton)
# ---------------------------------------------------------------------------
_scan_lock = threading.Lock()
_scan_state: dict[str, Any] = {
    "status": "idle",   # idle | scanning | done | error
    "result": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}

# ---------------------------------------------------------------------------
# 백그라운드 자동 마이그레이션 상태
# ---------------------------------------------------------------------------
_mig_lock = threading.Lock()
_mig_state: dict[str, Any] = {
    "status": "idle",       # idle | scanning | downloading | done | error
    "total": 0,
    "downloaded": 0,
    "skipped": 0,
    "failed": 0,
    "current_file": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
    "failed_files": [],
}


class PullUploadsRequest(BaseModel):
    overwrite: bool = Field(default=False)
    limit: int | None = Field(default=None, ge=1)
    dry_run: bool = Field(default=False)


class PullSingleRequest(BaseModel):
    path: str = Field(..., description="A 서버 기준 상대 파일 경로")
    overwrite: bool = Field(default=False)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _check_migration_token(x_migration_token: str | None) -> None:
    token = (getattr(settings, "MIGRATION_TOKEN", "") or "").strip()
    if token and x_migration_token != token:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _source_files_url() -> str:
    source_base = (settings.SMARTGAUGE_SOURCE_BASE_URL or "").rstrip("/")
    files_ep = (settings.SMARTGAUGE_SOURCE_FILES_ENDPOINT or "").strip()
    if files_ep and not files_ep.startswith("/"):
        files_ep = "/" + files_ep
    return f"{source_base}{files_ep}"


# ---------------------------------------------------------------------------
# 1) 파일 목록 조회 (A -> B 비교)
# ---------------------------------------------------------------------------

@router.get("/source-files")
def list_source_files(
    x_migration_token: str | None = Header(default=None),
):
    """
    (임시) A 서버의 업로드 파일 목록을 조회하고,
    각 파일이 B 서버에 이미 존재하는지 여부를 함께 반환합니다.
    """
    _check_migration_token(x_migration_token)

    try:
        return fetch_source_file_list(
            source_base_url=settings.SMARTGAUGE_SOURCE_BASE_URL,
            source_files_endpoint=settings.SMARTGAUGE_SOURCE_FILES_ENDPOINT,
            source_uploads_base_url=settings.SMARTGAUGE_SOURCE_UPLOADS_BASE_URL,
            migration_token=settings.SMARTGAUGE_SOURCE_MIGRATION_TOKEN,
            timeout_sec=settings.SMARTGAUGE_HTTP_TIMEOUT_SEC,
        )
    except httpx.TimeoutException as e:
        raise HTTPException(status_code=504, detail={"error": "timeout", "message": str(e)}) from e
    except (ValueError, RuntimeError) as e:
        msg = str(e) or e.__class__.__name__
        raise HTTPException(
            status_code=504 if "timeout" in msg.lower() else 502,
            detail={"error": e.__class__.__name__, "message": msg},
        ) from e


# ---------------------------------------------------------------------------
# 1-b) 백그라운드 스캔 시작 + 상태 조회 (504 방지)
# ---------------------------------------------------------------------------

def _do_background_scan() -> None:
    """백그라운드 스레드에서 실행되는 스캔 작업."""
    global _scan_state
    try:
        result = fetch_source_file_list(
            source_base_url=settings.SMARTGAUGE_SOURCE_BASE_URL,
            source_files_endpoint=settings.SMARTGAUGE_SOURCE_FILES_ENDPOINT,
            source_uploads_base_url=settings.SMARTGAUGE_SOURCE_UPLOADS_BASE_URL,
            migration_token=settings.SMARTGAUGE_SOURCE_MIGRATION_TOKEN,
            timeout_sec=settings.SMARTGAUGE_HTTP_TIMEOUT_SEC,
        )
        with _scan_lock:
            _scan_state["status"] = "done"
            _scan_state["result"] = result
            _scan_state["error"] = None
            _scan_state["finished_at"] = time.time()
    except Exception as e:
        with _scan_lock:
            _scan_state["status"] = "error"
            _scan_state["result"] = None
            _scan_state["error"] = str(e) or e.__class__.__name__
            _scan_state["finished_at"] = time.time()


@router.post("/start-source-scan")
def start_source_scan(
    background_tasks: BackgroundTasks,
    x_migration_token: str | None = Header(default=None),
):
    """
    A 서버 파일 목록 스캔을 백그라운드로 시작합니다.
    - 이미 스캔 중이면 현재 상태만 반환합니다.
    - 완료/에러 후 재호출하면 새 스캔을 시작합니다.
    """
    _check_migration_token(x_migration_token)

    with _scan_lock:
        if _scan_state["status"] == "scanning":
            return {
                "status": "scanning",
                "message": "이미 스캔이 진행 중입니다.",
                "started_at": _scan_state["started_at"],
            }

        _scan_state["status"] = "scanning"
        _scan_state["result"] = None
        _scan_state["error"] = None
        _scan_state["started_at"] = time.time()
        _scan_state["finished_at"] = None

    background_tasks.add_task(_do_background_scan)
    return {
        "status": "scanning",
        "message": "스캔을 시작했습니다.",
        "started_at": _scan_state["started_at"],
    }


@router.get("/source-files-status")
def get_source_scan_status(
    x_migration_token: str | None = Header(default=None),
):
    """
    백그라운드 스캔의 현재 상태를 반환합니다.
    - status: idle | scanning | done | error
    - done일 때 result에 파일 목록이 포함됩니다.
    """
    _check_migration_token(x_migration_token)

    with _scan_lock:
        state_copy = {
            "status": _scan_state["status"],
            "started_at": _scan_state["started_at"],
            "finished_at": _scan_state["finished_at"],
        }
        if _scan_state["status"] == "done":
            state_copy["result"] = _scan_state["result"]
        elif _scan_state["status"] == "error":
            state_copy["error"] = _scan_state["error"]

    return state_copy


# ---------------------------------------------------------------------------
# 1-c) 자동 전체 마이그레이션 (목록조회 + 다운로드 일괄, 백그라운드)
# ---------------------------------------------------------------------------

def _do_auto_migration(overwrite: bool = False) -> None:
    """백그라운드 스레드: A 파일 목록 → 순차 다운로드 → 완료."""
    global _mig_state

    # --- Phase 1: 파일 목록 조회 ---
    with _mig_lock:
        _mig_state["status"] = "scanning"
        _mig_state["current_file"] = None

    print("[MIGRATION] Phase 1: A 서버 파일 목록 조회 시작…")

    try:
        result = fetch_source_file_list(
            source_base_url=settings.SMARTGAUGE_SOURCE_BASE_URL,
            source_files_endpoint=settings.SMARTGAUGE_SOURCE_FILES_ENDPOINT,
            source_uploads_base_url=settings.SMARTGAUGE_SOURCE_UPLOADS_BASE_URL,
            migration_token=settings.SMARTGAUGE_SOURCE_MIGRATION_TOKEN,
            timeout_sec=settings.SMARTGAUGE_HTTP_TIMEOUT_SEC,
        )
    except Exception as e:
        err_msg = str(e) or e.__class__.__name__
        print(f"[MIGRATION] ❌ 파일 목록 조회 실패: {err_msg}")
        with _mig_lock:
            _mig_state["status"] = "error"
            _mig_state["error"] = f"파일 목록 조회 실패: {err_msg}"
            _mig_state["finished_at"] = time.time()
        return

    files = result.get("files") or []
    total = len(files)
    # pending = exists_on_b 가 False인 파일들
    pending = [f for f in files if not f.get("exists_on_b")]
    skipped_count = total - len(pending)

    print(f"[MIGRATION] 파일 목록 조회 완료: 전체 {total}개, 이미 존재 {skipped_count}개, 이전 대상 {len(pending)}개")

    with _mig_lock:
        _mig_state["total"] = total
        _mig_state["skipped"] = skipped_count
        _mig_state["downloaded"] = 0
        _mig_state["failed"] = 0
        _mig_state["failed_files"] = []
        _mig_state["status"] = "downloading"

    if not pending:
        print("[MIGRATION] ✅ 이전할 파일 없음 — 모두 존재합니다.")
        with _mig_lock:
            _mig_state["status"] = "done"
            _mig_state["finished_at"] = time.time()
        return

    # --- Phase 2: 순차 다운로드 ---
    print(f"[MIGRATION] Phase 2: {len(pending)}개 파일 다운로드 시작…")

    for i, finfo in enumerate(pending, 1):
        rel_path = finfo.get("path", "")
        with _mig_lock:
            _mig_state["current_file"] = rel_path

        print(f"[MIGRATION]  [{i}/{len(pending)}] 다운로드: {rel_path}")

        try:
            dl_result = download_single_file(
                relative_path=rel_path,
                source_uploads_base_url=settings.SMARTGAUGE_SOURCE_UPLOADS_BASE_URL,
                migration_token=settings.SMARTGAUGE_SOURCE_MIGRATION_TOKEN,
                overwrite=overwrite,
                timeout_sec=settings.SMARTGAUGE_HTTP_TIMEOUT_SEC,
            )
            action = dl_result.get("action", "")
            if action == "skipped":
                print(f"[MIGRATION]  [{i}/{len(pending)}] ⏭ 스킵(이미 존재): {rel_path}")
                with _mig_lock:
                    _mig_state["skipped"] += 1
            elif action == "downloaded":
                print(f"[MIGRATION]  [{i}/{len(pending)}] ✅ 완료: {rel_path}")
                with _mig_lock:
                    _mig_state["downloaded"] += 1
            else:
                reason = dl_result.get("reason", action)
                print(f"[MIGRATION]  [{i}/{len(pending)}] ❌ 실패: {rel_path} — {reason}")
                with _mig_lock:
                    _mig_state["failed"] += 1
                    _mig_state["failed_files"].append({"path": rel_path, "reason": reason})
        except Exception as e:
            reason = str(e) or e.__class__.__name__
            print(f"[MIGRATION]  [{i}/{len(pending)}] ❌ 예외: {rel_path} — {reason}")
            with _mig_lock:
                _mig_state["failed"] += 1
                _mig_state["failed_files"].append({"path": rel_path, "reason": reason})

    with _mig_lock:
        _mig_state["status"] = "done"
        _mig_state["current_file"] = None
        _mig_state["finished_at"] = time.time()

    dl = _mig_state["downloaded"]
    sk = _mig_state["skipped"]
    fa = _mig_state["failed"]
    print(f"[MIGRATION] ========== 마이그레이션 완료 ==========")
    print(f"[MIGRATION]   전체: {total}  다운로드: {dl}  스킵: {sk}  실패: {fa}")
    print(f"[MIGRATION] =======================================")


@router.post("/start-auto-migration")
def start_auto_migration(
    background_tasks: BackgroundTasks,
    overwrite: bool = False,
    x_migration_token: str | None = Header(default=None),
):
    """
    A→B 전체 자동 마이그레이션을 백그라운드로 시작합니다.
    - 파일 목록 조회 → 미존재 파일 순차 다운로드
    - 진행 상태는 GET /auto-migration-status 로 폴링
    """
    _check_migration_token(x_migration_token)

    with _mig_lock:
        if _mig_state["status"] in ("scanning", "downloading"):
            return {
                "status": _mig_state["status"],
                "message": "이미 마이그레이션이 진행 중입니다.",
                "started_at": _mig_state["started_at"],
            }

        _mig_state.update({
            "status": "scanning",
            "total": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "current_file": None,
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
            "failed_files": [],
        })

    background_tasks.add_task(_do_auto_migration, overwrite)
    return {
        "status": "scanning",
        "message": "마이그레이션을 시작했습니다. GET /auto-migration-status 로 상태를 확인하세요.",
        "started_at": _mig_state["started_at"],
    }


@router.get("/auto-migration-status")
def get_auto_migration_status(
    x_migration_token: str | None = Header(default=None),
):
    """
    자동 마이그레이션 진행 상태를 반환합니다.
    응답은 가볍게(파일 목록 없이) 유지하여 타임아웃을 방지합니다.
    """
    _check_migration_token(x_migration_token)

    with _mig_lock:
        return {
            "status": _mig_state["status"],
            "total": _mig_state["total"],
            "downloaded": _mig_state["downloaded"],
            "skipped": _mig_state["skipped"],
            "failed": _mig_state["failed"],
            "current_file": _mig_state["current_file"],
            "error": _mig_state["error"],
            "started_at": _mig_state["started_at"],
            "finished_at": _mig_state["finished_at"],
            "failed_count": len(_mig_state["failed_files"]),
        }


# ---------------------------------------------------------------------------
# 2) 단일 파일 이전
# ---------------------------------------------------------------------------

@router.post("/pull-single")
def pull_single_file(
    payload: PullSingleRequest = Body(...),
    x_migration_token: str | None = Header(default=None),
):
    """
    (임시) A 서버에서 파일 1개를 다운로드하여 B 서버 /data/uploads 에 저장합니다.
    - 요청 1건 = 파일 1개이므로 타임아웃 위험이 거의 없습니다.
    """
    _check_migration_token(x_migration_token)

    try:
        return download_single_file(
            relative_path=payload.path,
            source_uploads_base_url=settings.SMARTGAUGE_SOURCE_UPLOADS_BASE_URL,
            migration_token=settings.SMARTGAUGE_SOURCE_MIGRATION_TOKEN,
            overwrite=payload.overwrite,
            timeout_sec=settings.SMARTGAUGE_HTTP_TIMEOUT_SEC,
        )
    except httpx.TimeoutException as e:
        raise HTTPException(status_code=504, detail={"error": "timeout", "message": str(e), "path": payload.path}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": "invalid_path", "message": str(e), "path": payload.path}) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": e.__class__.__name__, "message": str(e), "path": payload.path}) from e


# ---------------------------------------------------------------------------
# 3) 진단
# ---------------------------------------------------------------------------

@router.get("/diagnose-source")
def diagnose_source(timeout_sec: float = 5.0):
    """
    (임시) B 서버에서 A 서버로 네트워크가 가능한지 진단합니다.
    - DNS resolve 결과
    - A 루트(/) 요청 시도
    - A 파일목록 API 요청 시도
    """
    source_base = (settings.SMARTGAUGE_SOURCE_BASE_URL or "").rstrip("/")
    source_files_url = _source_files_url()

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
        headers["x-migration-token"] = token  # 하이픈 사용

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


# ---------------------------------------------------------------------------
# 4) 기존 일괄 마이그레이션 (유지)
# ---------------------------------------------------------------------------

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
    _check_migration_token(x_migration_token)

    source_files_url = _source_files_url()

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

