from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx


logger = logging.getLogger(__name__)


def get_uploads_root_dir() -> Path:
    # 프로젝트 기존 업로드/정적 파일 루트는 STATIC_DIR(/data/uploads) 사용
    return Path(os.getenv("STATIC_DIR", "/data/uploads")).resolve()


def validate_relative_upload_path(path_str: str) -> str:
    """
    상대 경로만 허용하며, path traversal(../) 및 절대경로/드라이브 경로를 차단합니다.
    반환값은 항상 posix 스타일(relative) 경로입니다.
    """
    raw = (path_str or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("empty path")

    # 절대경로/프로토콜/드라이브(Windows) 차단
    if raw.startswith("/") or raw.startswith("//"):
        raise ValueError("absolute path not allowed")
    if ":" in PurePosixPath(raw).parts[0]:
        # e.g. C:...
        raise ValueError("drive path not allowed")

    p = PurePosixPath(raw)
    parts = p.parts
    if any(part in ("..",) for part in parts):
        raise ValueError("path traversal not allowed")
    if any(part in ("", ".",) for part in parts):
        # PurePosixPath는 중복 슬래시를 정규화하지 않으므로 방어적으로 차단
        # (예: "a//b" -> parts에 ''가 들어올 수 있음)
        raise ValueError("invalid path segment")

    normalized = p.as_posix().lstrip("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        raise ValueError("invalid normalized path")
    return normalized


def _ensure_target_path_under_root(root_dir: Path, rel_posix_path: str) -> Path:
    root = root_dir.resolve()
    candidate = (root / rel_posix_path).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("target path escapes uploads root")
    return candidate


def build_source_file_url(uploads_base_url: str, relative_path: str) -> str:
    base = (uploads_base_url or "").strip().rstrip("/")
    # 경로 세그먼트별 quote (슬래시 유지)
    quoted = "/".join(quote(seg) for seg in relative_path.split("/"))
    return f"{base}/{quoted}"


def save_stream_to_target(target_path: Path, stream_response: httpx.Response) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "wb") as f:
        for chunk in stream_response.iter_bytes():
            if chunk:
                f.write(chunk)


def migrate_uploads_from_source(
    *,
    source_base_url: str,
    source_files_endpoint: str,
    source_uploads_base_url: str,
    migration_token: str = "",
    overwrite: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """
    A 서버 파일목록을 가져와 B 서버 /data/uploads(STATIC_DIR)로 순차 다운로드 후 저장합니다.
    - stream download(httpx.stream)
    - skip 기본, overwrite 옵션 지원
    - 각 파일별 예외 처리(전체 중단 방지)
    """
    source_base = (source_base_url or "").strip().rstrip("/")
    files_ep = (source_files_endpoint or "").strip()
    if not files_ep.startswith("/"):
        files_ep = "/" + files_ep
    list_url = f"{source_base}{files_ep}"

    token = (migration_token or "").strip()
    headers: dict[str, str] = {}
    if token:
        headers["x_migration_token"] = token

    # 요청하신대로 timeout을 매우 크게(예: 1년) 늘릴 수 있도록,
    # connect/read/write/pool 모두 동일한 timeout_sec를 적용합니다.
    timeout = httpx.Timeout(
        timeout=timeout_sec,
        connect=float(timeout_sec),
        read=float(timeout_sec),
        write=float(timeout_sec),
        pool=float(timeout_sec),
    )
    target_root = get_uploads_root_dir()

    total = 0
    downloaded = 0
    skipped = 0
    failed = 0
    failed_files: list[dict[str, str]] = []

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        # 1) A 서버 파일 목록 API 호출
        try:
            r = client.get(list_url, headers=headers)
            r.raise_for_status()
            payload = r.json()
        except httpx.TimeoutException as e:
            raise RuntimeError(f"source list fetch timeout: {list_url}") from e
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = (e.response.text or "")[:500]
            except Exception:
                body = ""
            raise RuntimeError(
                f"source list fetch failed: HTTP {e.response.status_code} ({list_url}){(': ' + body) if body else ''}"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"source list fetch request error: {list_url} ({e.__class__.__name__})") from e

        files = payload.get("files") or []
        if not isinstance(files, list):
            raise ValueError("invalid files payload")

        # 지시서: A가 base_url을 내려주거나, 환경변수로 지정 가능
        uploads_base_url = (
            (payload.get("base_url") or "").strip().rstrip("/")
            or (source_uploads_base_url or "").strip().rstrip("/")
        )
        if not uploads_base_url:
            raise ValueError("missing uploads_base_url")

        # 2) limit 옵션
        if limit is not None:
            try:
                lim = int(limit)
            except Exception:
                lim = 0
            if lim > 0:
                files = files[:lim]
            else:
                files = []

        total = len(files)

        # 3) 순차 다운로드/저장
        for raw_path in files:
            try:
                if not isinstance(raw_path, str):
                    raise ValueError("path is not a string")

                rel = validate_relative_upload_path(raw_path)
                target_path = _ensure_target_path_under_root(target_root, rel)

                if target_path.exists() and not overwrite:
                    skipped += 1
                    continue

                src_url = build_source_file_url(uploads_base_url, rel)

                if dry_run:
                    downloaded += 1
                    continue

                with client.stream("GET", src_url, headers=headers) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    save_stream_to_target(target_path, resp)
                    downloaded += 1
            except Exception as e:
                failed += 1
                reason = str(e) or e.__class__.__name__
                failed_files.append({"path": str(raw_path), "reason": reason})
                # 과도한 로그는 피하고, 실패만 info 수준으로 남김
                logger.info("upload migration failed: path=%s reason=%s", raw_path, reason)

    public_base_url = os.getenv("PUBLIC_BASE_URL", "https://api.daewon469.com").strip().rstrip("/")
    return {
        "success": failed == 0,
        "source": source_base_url,
        "target": public_base_url,
        "total": total,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "failed_files": failed_files,
        "overwrite": overwrite,
        "dry_run": dry_run,
        "limit": limit,
        "source_files_url": list_url,
        "source_uploads_base_url": source_uploads_base_url,
        "target_root": str(target_root),
    }

