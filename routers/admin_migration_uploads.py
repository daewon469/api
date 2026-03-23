from __future__ import annotations

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
        raise HTTPException(status_code=504, detail=str(e) or "timeout") from e
    except (ValueError, RuntimeError) as e:
        # 소스 목록 실패/파싱 실패 등을 502로 노출 (500으로 죽지 않게)
        raise HTTPException(status_code=502, detail=str(e) or e.__class__.__name__) from e

