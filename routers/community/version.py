from __future__ import annotations

import os
import re
from typing import List, Literal, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

router = APIRouter()

# SOFTWARE_VERSION 환경변수가 비었을 때만 사용
HARD_VERSION = "1.1.2"


class AppVersionOut(BaseModel):
    status: int = 0
    platform: Literal["android", "ios"]
    current_version: Optional[str] = None
    latest_version: str
    min_supported_version: str
    force_update: bool
    store_url: Optional[str] = None
    message: Optional[str] = None


def _version_parts(v: str) -> List[int]:
    """'1.2.3' 형태 버전을 비교 가능한 숫자 배열로 변환."""
    s = (v or "").strip()
    if not s:
        return [0]
    nums: List[int] = []
    for p in s.split("."):
        m = re.match(r"(\d+)", (p or "").strip())
        nums.append(int(m.group(1)) if m else 0)
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return nums or [0]


def _is_version_lt(a: str, b: str) -> bool:
    pa = _version_parts(a)
    pb = _version_parts(b)
    n = max(len(pa), len(pb))
    for i in range(n):
        av = pa[i] if i < len(pa) else 0
        bv = pb[i] if i < len(pb) else 0
        if av < bv:
            return True
        if av > bv:
            return False
    return False


def _resolved_version() -> str:
    """SOFTWARE_VERSION → HARD_VERSION 순으로 최신=최소 버전 결정."""
    return (os.getenv("SOFTWARE_VERSION", "") or "").strip() or HARD_VERSION


@router.get("/community/app/version", response_model=AppVersionOut)
def community_app_version(
    platform: Literal["android", "ios"] = Query(...),
    current_version: Optional[str] = Query(None),
):
    """
    앱 시작 시 버전 체크(강제 업데이트용).
    - SOFTWARE_VERSION 이 곧 최신·최소 버전 (android/ios 공통)
    - SOFTWARE_VERSION 이 비면 HARD_VERSION 사용
    - current_version < SOFTWARE_VERSION 이면 force_update=True

    환경변수:
      - SOFTWARE_VERSION
      - APP_ANDROID_STORE_URL / APP_ANDROID_PACKAGE
      - APP_IOS_STORE_URL
      - APP_FORCE_UPDATE_MESSAGE
    """
    version = _resolved_version()
    msg = (os.getenv("APP_FORCE_UPDATE_MESSAGE", "") or "").strip() or (
        "최신 버전으로 업데이트 후 이용해 주세요."
    )

    if platform == "android":
        pkg = (os.getenv("APP_ANDROID_PACKAGE", "") or "").strip() or "com.smartgauge.bunyangpro"
        store_url = (os.getenv("APP_ANDROID_STORE_URL", "") or "").strip() or f"market://details?id={pkg}"
    else:
        store_url = (os.getenv("APP_IOS_STORE_URL", "") or "").strip() or None

    force_update = bool(current_version and _is_version_lt(current_version, version))

    return {
        "status": 0,
        "platform": platform,
        "current_version": current_version,
        "latest_version": version,
        "min_supported_version": version,
        "force_update": force_update,
        "store_url": store_url,
        "message": msg,
    }
