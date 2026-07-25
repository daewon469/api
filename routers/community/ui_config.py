from __future__ import annotations

from typing import List, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from deps import get_db
from models import Community_UI_Config
from .logic import (
    SLIDE_CARD_MAX,
    _rollover_recruit_and_slide_card_types,
)

router = APIRouter()


# ==================== Community: UI Config (banner/popup) ====================
class UIConfigBannerItem(BaseModel):
    image_url: str = Field(min_length=1, max_length=1024)
    link_url: str | None = Field(default=None, max_length=1024)
    # click action (optional)
    # - link: open link_url (default)
    # - referral_modal: open referral modal in app
    click_action: Literal["link", "referral_modal"] | None = None
    # per-banner rendering options (optional; server will normalize/fill defaults)
    width_percent: int | None = Field(default=None, ge=40, le=100)
    # px(dp) based width (preferred; if set, client can render fixed width)
    width_px: int | None = Field(default=None, ge=120, le=1200)
    height: int | None = Field(default=None, ge=60, le=260)
    resize_mode: Literal["contain", "cover", "stretch"] | None = None


class UIConfigBanner(BaseModel):
    enabled: bool = True
    interval_posts: int = Field(default=10, ge=1, le=200)
    items: List[UIConfigBannerItem] = Field(default_factory=list)
    height: int = Field(default=110, ge=60, le=260)
    resize_mode: Literal["contain", "cover", "stretch"] = "contain"


class UIConfigTopBanner(BaseModel):
    enabled: bool = True
    # 첫화면 상단 배너는 2개 슬롯만 사용
    items: List[UIConfigBannerItem] = Field(default_factory=list)
    height: int = Field(default=70, ge=60, le=260)
    resize_mode: Literal["contain", "cover", "stretch"] = "contain"


class UIConfigWebTopBanner(BaseModel):
    """웹(데스크탑) 홈 상단 배너 — 고정 1행 3개"""
    enabled: bool = True
    items: List[UIConfigBannerItem] = Field(default_factory=list)
    cols_per_row: int = Field(default=3, ge=3, le=3)
    # 구버전 클라이언트 payload 호환용. 저장 시 1로 정규화한다.
    rotation_count: int = Field(default=1, ge=1, le=10)
    height: int = Field(default=160, ge=60, le=260)
    resize_mode: Literal["contain", "cover", "stretch"] = "contain"
    auto_play_ms: int = Field(default=4000, ge=0, le=60000)


class UIConfigWebBanner(BaseModel):
    """웹(데스크탑) 피드(하단) 배너 — N줄마다 M행(각 3열) 삽입"""
    enabled: bool = True
    items: List[UIConfigBannerItem] = Field(default_factory=list)
    cols_per_row: int = Field(default=3, ge=3, le=3)
    interval_rows: int = Field(default=3, ge=1, le=100)
    rows_per_interval: int | None = Field(default=None, ge=1, le=10)
    # 구버전 클라이언트 payload 호환용 별칭.
    rotation_count: int | None = Field(default=None, ge=1, le=10)
    height: int = Field(default=160, ge=60, le=260)
    resize_mode: Literal["contain", "cover", "stretch"] = "contain"


class UIConfigPopup(BaseModel):
    enabled: bool = True
    image_url: str | None = Field(default=None, max_length=1024)
    link_url: str | None = Field(default=None, max_length=1024)
    width_percent: int = Field(default=92, ge=40, le=100)
    height: int = Field(default=360, ge=200, le=900)
    resize_mode: Literal["contain", "cover", "stretch"] = "contain"


class UIConfigTitleSearch(BaseModel):
    """
    제목검색 화면에서 상단에 노출할 추천 현장(게시글) 목록.
    - recommended_post_ids: post_id 목록
    """
    enabled: bool = True
    recommended_post_ids: List[int] = Field(default_factory=list)


class UIConfigSlidePosts(BaseModel):
    """
    첫화면 슬라이드(Postcard_S)에 노출할 구인글 목록(관리자 핀).
    - post_ids: 노출 우선 순서(최대 10). 부족분은 서버가 최신 구인글로 card_type=5 자동 채움.
    """
    post_ids: List[int] = Field(default_factory=list, max_length=10)


class UIConfigPayload(BaseModel):
    banner: UIConfigBanner = Field(default_factory=UIConfigBanner)
    top_banner: UIConfigTopBanner = Field(default_factory=UIConfigTopBanner)
    web_top_banner: UIConfigWebTopBanner = Field(default_factory=UIConfigWebTopBanner)
    web_banner: UIConfigWebBanner = Field(default_factory=UIConfigWebBanner)
    popup: UIConfigPopup = Field(default_factory=UIConfigPopup)
    ios_popup: UIConfigPopup = Field(default_factory=UIConfigPopup)
    title_search: UIConfigTitleSearch = Field(default_factory=UIConfigTitleSearch)
    slide_posts: UIConfigSlidePosts = Field(default_factory=UIConfigSlidePosts)


def _default_popup_dict() -> dict:
    return {
        "enabled": True,
        "image_url": None,
        "link_url": None,
        "width_percent": 92,
        "height": 360,
        "resize_mode": "contain",
    }


def _normalize_popup_dict(raw: dict | None, base: dict | None = None) -> dict:
    out = dict(base or _default_popup_dict())
    src = raw if isinstance(raw, dict) else {}
    out["enabled"] = bool(src.get("enabled", out["enabled"]))
    img = src.get("image_url", None)
    out["image_url"] = str(img).strip() if isinstance(img, str) and str(img).strip() else None
    link = src.get("link_url", None)
    out["link_url"] = str(link).strip() if isinstance(link, str) and str(link).strip() else None
    try:
        out["width_percent"] = int(src.get("width_percent", out["width_percent"]))
    except Exception:
        out["width_percent"] = 92
    out["width_percent"] = max(40, min(100, out["width_percent"]))
    try:
        out["height"] = int(src.get("height", out["height"]))
    except Exception:
        out["height"] = 360
    out["height"] = max(200, min(900, out["height"]))
    prm = src.get("resize_mode", out["resize_mode"])
    prm_s = str(prm).strip().lower() if isinstance(prm, str) else "contain"
    out["resize_mode"] = prm_s if prm_s in ("contain", "cover", "stretch") else "contain"
    return out


def _default_ui_config_dict() -> dict:
    return {
        "banner": {
            "enabled": True,
            "interval_posts": 10,
            "items": [],
            "height": 110,
            "resize_mode": "contain",
        },
        "top_banner": {
            "enabled": True,
            "items": [],
            "height": 70,
            "resize_mode": "contain",
        },
        "web_top_banner": {
            "enabled": True,
            "items": [],
            "cols_per_row": 3,
            "rotation_count": 1,
            "height": 160,
            "resize_mode": "contain",
            "auto_play_ms": 0,
        },
        "web_banner": {
            "enabled": True,
            "items": [],
            "cols_per_row": 3,
            "interval_rows": 3,
            "rows_per_interval": 3,
            "rotation_count": 3,
            "height": 160,
            "resize_mode": "contain",
        },
        "popup": _default_popup_dict(),
        "ios_popup": _default_popup_dict(),
        "title_search": {
            "enabled": True,
            "recommended_post_ids": [],
        },
        "slide_posts": {
            "post_ids": [],
        },
    }


def _normalize_ui_config(raw: dict | None) -> dict:
    base = _default_ui_config_dict()
    if not isinstance(raw, dict):
        return base
    banner = raw.get("banner") if isinstance(raw.get("banner"), dict) else {}
    top_banner = raw.get("top_banner") if isinstance(raw.get("top_banner"), dict) else {}
    popup = raw.get("popup") if isinstance(raw.get("popup"), dict) else {}
    ios_popup = raw.get("ios_popup") if isinstance(raw.get("ios_popup"), dict) else {}
    title_search = raw.get("title_search") if isinstance(raw.get("title_search"), dict) else {}
    slide_posts = raw.get("slide_posts") if isinstance(raw.get("slide_posts"), dict) else {}

    base["banner"]["enabled"] = bool(banner.get("enabled", base["banner"]["enabled"]))
    try:
        base["banner"]["interval_posts"] = int(banner.get("interval_posts", base["banner"]["interval_posts"]))
    except Exception:
        base["banner"]["interval_posts"] = 10
    if base["banner"]["interval_posts"] < 1:
        base["banner"]["interval_posts"] = 1
    if base["banner"]["interval_posts"] > 200:
        base["banner"]["interval_posts"] = 200

    # banner: size options
    try:
        base["banner"]["height"] = int(banner.get("height", base["banner"]["height"]))
    except Exception:
        base["banner"]["height"] = 110
    if base["banner"]["height"] < 60:
        base["banner"]["height"] = 60
    if base["banner"]["height"] > 260:
        base["banner"]["height"] = 260

    rm = banner.get("resize_mode", base["banner"]["resize_mode"])
    rm_s = str(rm).strip().lower() if isinstance(rm, str) else "contain"
    base["banner"]["resize_mode"] = rm_s if rm_s in ("contain", "cover", "stretch") else "contain"

    items = banner.get("items")
    norm_items: list[dict] = []
    if isinstance(items, list):
        for it in items[:30]:
            if not isinstance(it, dict):
                continue
            img = str(it.get("image_url") or "").strip()
            if not img:
                continue

            link = it.get("link_url", None)
            link_s = str(link).strip() if isinstance(link, str) else None

            ca = it.get("click_action", None)
            ca_s = str(ca).strip().lower() if isinstance(ca, str) else None
            click_action = ca_s if ca_s in ("link", "referral_modal") else None

            # per-item sizing (fallback to banner defaults)
            try:
                wp = int(it.get("width_percent", 100))
            except Exception:
                wp = 100
            if wp < 40:
                wp = 40
            if wp > 100:
                wp = 100

            # per-item width in px(dp): optional
            try:
                wpx_raw = it.get("width_px", None)
                wpx = int(wpx_raw) if wpx_raw is not None and str(wpx_raw).strip() != "" else None
            except Exception:
                wpx = None
            if wpx is not None:
                if wpx < 120:
                    wpx = 120
                if wpx > 1200:
                    wpx = 1200

            try:
                h = int(it.get("height", base["banner"]["height"]))
            except Exception:
                h = int(base["banner"]["height"])
            if h < 60:
                h = 60
            if h > 260:
                h = 260

            irm = it.get("resize_mode", base["banner"]["resize_mode"])
            irm_s = str(irm).strip().lower() if isinstance(irm, str) else str(base["banner"]["resize_mode"])
            irm_s = irm_s if irm_s in ("contain", "cover", "stretch") else "contain"

            norm_items.append(
                {
                    "image_url": img,
                    "link_url": link_s or None,
                    "click_action": click_action,
                    "width_percent": wp,
                    "width_px": wpx,
                    "height": h,
                    "resize_mode": irm_s,
                }
            )
    base["banner"]["items"] = norm_items

    # top_banner: first screen top 2 slots
    base["top_banner"]["enabled"] = bool(top_banner.get("enabled", base["top_banner"]["enabled"]))

    # top_banner: size options
    try:
        base["top_banner"]["height"] = int(top_banner.get("height", base["top_banner"]["height"]))
    except Exception:
        base["top_banner"]["height"] = 70
    if base["top_banner"]["height"] < 60:
        base["top_banner"]["height"] = 60
    if base["top_banner"]["height"] > 260:
        base["top_banner"]["height"] = 260

    trm = top_banner.get("resize_mode", base["top_banner"]["resize_mode"])
    trm_s = str(trm).strip().lower() if isinstance(trm, str) else "contain"
    base["top_banner"]["resize_mode"] = trm_s if trm_s in ("contain", "cover", "stretch") else "contain"

    top_items = top_banner.get("items")
    norm_top_items: list[dict] = []
    if isinstance(top_items, list):
        for it in top_items[:2]:
            if not isinstance(it, dict):
                continue
            img = str(it.get("image_url") or "").strip()
            if not img:
                continue

            link = it.get("link_url", None)
            link_s = str(link).strip() if isinstance(link, str) else None

            ca = it.get("click_action", None)
            ca_s = str(ca).strip().lower() if isinstance(ca, str) else None
            click_action = ca_s if ca_s in ("link", "referral_modal") else None

            try:
                wp = int(it.get("width_percent", 100))
            except Exception:
                wp = 100
            if wp < 40:
                wp = 40
            if wp > 100:
                wp = 100

            try:
                wpx_raw = it.get("width_px", None)
                wpx = int(wpx_raw) if wpx_raw is not None and str(wpx_raw).strip() != "" else None
            except Exception:
                wpx = None
            if wpx is not None:
                if wpx < 120:
                    wpx = 120
                if wpx > 1200:
                    wpx = 1200

            try:
                h = int(it.get("height", base["top_banner"]["height"]))
            except Exception:
                h = int(base["top_banner"]["height"])
            if h < 60:
                h = 60
            if h > 260:
                h = 260

            irm = it.get("resize_mode", base["top_banner"]["resize_mode"])
            irm_s = str(irm).strip().lower() if isinstance(irm, str) else str(base["top_banner"]["resize_mode"])
            irm_s = irm_s if irm_s in ("contain", "cover", "stretch") else "contain"

            norm_top_items.append(
                {
                    "image_url": img,
                    "link_url": link_s or None,
                    "click_action": click_action,
                    "width_percent": wp,
                    "width_px": wpx,
                    "height": h,
                    "resize_mode": irm_s,
                }
            )
    base["top_banner"]["items"] = norm_top_items

    def _norm_banner_items_list(
        raw_items: object,
        *,
        max_items: int,
        default_height: int,
        default_resize: str,
    ) -> list[dict]:
        norm: list[dict] = []
        if not isinstance(raw_items, list):
            return norm
        for it in raw_items[:max_items]:
            if not isinstance(it, dict):
                continue
            img = str(it.get("image_url") or "").strip()
            if not img:
                continue
            link = it.get("link_url", None)
            link_s = str(link).strip() if isinstance(link, str) else None
            ca = it.get("click_action", None)
            ca_s = str(ca).strip().lower() if isinstance(ca, str) else None
            click_action = ca_s if ca_s in ("link", "referral_modal") else None
            try:
                wp = int(it.get("width_percent", 100))
            except Exception:
                wp = 100
            wp = max(40, min(100, wp))
            try:
                wpx_raw = it.get("width_px", None)
                wpx = int(wpx_raw) if wpx_raw is not None and str(wpx_raw).strip() != "" else None
            except Exception:
                wpx = None
            if wpx is not None:
                wpx = max(120, min(1200, wpx))
            try:
                h = int(it.get("height", default_height))
            except Exception:
                h = default_height
            h = max(60, min(260, h))
            irm = it.get("resize_mode", default_resize)
            irm_s = str(irm).strip().lower() if isinstance(irm, str) else str(default_resize)
            irm_s = irm_s if irm_s in ("contain", "cover", "stretch") else "contain"
            norm.append(
                {
                    "image_url": img,
                    "link_url": link_s or None,
                    "click_action": click_action,
                    "width_percent": wp,
                    "width_px": wpx,
                    "height": h,
                    "resize_mode": irm_s,
                }
            )
        return norm

    def _norm_web_section(raw_section: object, base_section: dict, *, item_cap: int) -> None:
        sec = raw_section if isinstance(raw_section, dict) else {}
        base_section["enabled"] = bool(sec.get("enabled", base_section["enabled"]))
        try:
            base_section["cols_per_row"] = int(sec.get("cols_per_row", base_section["cols_per_row"]))
        except Exception:
            base_section["cols_per_row"] = 3
        if base_section["cols_per_row"] != 3:
            base_section["cols_per_row"] = 3
        try:
            base_section["height"] = int(sec.get("height", base_section["height"]))
        except Exception:
            pass
        base_section["height"] = max(60, min(260, int(base_section["height"])))
        rm = sec.get("resize_mode", base_section["resize_mode"])
        rm_s = str(rm).strip().lower() if isinstance(rm, str) else str(base_section["resize_mode"])
        base_section["resize_mode"] = rm_s if rm_s in ("contain", "cover", "stretch") else "contain"
        base_section["items"] = _norm_banner_items_list(
            sec.get("items"),
            max_items=item_cap,
            default_height=int(base_section["height"]),
            default_resize=str(base_section["resize_mode"]),
        )

    web_top_banner = raw.get("web_top_banner") if isinstance(raw.get("web_top_banner"), dict) else {}
    _norm_web_section(web_top_banner, base["web_top_banner"], item_cap=3)
    base["web_top_banner"]["rotation_count"] = 1
    base["web_top_banner"]["auto_play_ms"] = 0
    base["web_top_banner"]["items"] = base["web_top_banner"]["items"][:3]

    web_banner = raw.get("web_banner") if isinstance(raw.get("web_banner"), dict) else {}
    _norm_web_section(web_banner, base["web_banner"], item_cap=30)
    try:
        interval_rows = int(web_banner.get("interval_rows", base["web_banner"]["interval_rows"]))
    except Exception:
        interval_rows = 3
    base["web_banner"]["interval_rows"] = max(1, min(100, interval_rows))

    rows_raw = web_banner.get("rows_per_interval")
    if rows_raw is None:
        rows_raw = web_banner.get("rotation_count", base["web_banner"]["rows_per_interval"])
    try:
        rows_per_interval = int(rows_raw)
    except Exception:
        rows_per_interval = 3
    rows_per_interval = max(1, min(10, rows_per_interval))
    base["web_banner"]["rows_per_interval"] = rows_per_interval
    # 구버전 클라이언트가 계속 읽을 수 있도록 같은 값을 함께 반환한다.
    base["web_banner"]["rotation_count"] = rows_per_interval
    cap_feed = rows_per_interval * int(base["web_banner"]["cols_per_row"])
    base["web_banner"]["items"] = base["web_banner"]["items"][:cap_feed]

    base["popup"] = _normalize_popup_dict(popup, base["popup"])
    base["ios_popup"] = _normalize_popup_dict(ios_popup, base["ios_popup"])

    # title_search: recommended posts by id
    base["title_search"]["enabled"] = bool(title_search.get("enabled", base["title_search"]["enabled"]))
    ids_raw = title_search.get("recommended_post_ids", [])
    ids: list[int] = []
    if isinstance(ids_raw, list):
        for v in ids_raw:
            try:
                n = int(v)
            except Exception:
                continue
            if n > 0:
                ids.append(n)
    # uniq keep order
    seen = set()
    uniq_ids: list[int] = []
    for n in ids:
        if n in seen:
            continue
        seen.add(n)
        uniq_ids.append(n)
    base["title_search"]["recommended_post_ids"] = uniq_ids

    # slide_posts: first-screen slider pinned ids (order preserved, max 10)
    slide_ids_raw = slide_posts.get("post_ids", [])
    slide_ids: list[int] = []
    if isinstance(slide_ids_raw, list):
        for v in slide_ids_raw:
            try:
                n = int(v)
            except Exception:
                continue
            if n > 0:
                slide_ids.append(n)
    slide_seen = set()
    uniq_slide_ids: list[int] = []
    for n in slide_ids:
        if n in slide_seen:
            continue
        slide_seen.add(n)
        uniq_slide_ids.append(n)
        if len(uniq_slide_ids) >= SLIDE_CARD_MAX:
            break
    base["slide_posts"]["post_ids"] = uniq_slide_ids
    return base


@router.get("/community/ui-config")
def community_get_ui_config(db: Session = Depends(get_db)):
    """
    앱 UI 설정 조회(배너/팝업 등).
    - 인증 불필요(읽기 전용)
    """
    try:
        row = db.query(Community_UI_Config).filter(Community_UI_Config.id == 1).first()
        cfg = _normalize_ui_config(getattr(row, "config", None) if row else None)
        return {"status": 0, "config": cfg}
    except Exception:
        return {"status": 8, "config": _default_ui_config_dict()}


def _deep_merge_dict(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


@router.put("/community/admin/ui-config")
def community_update_ui_config(
    payload: UIConfigPayload,
    db: Session = Depends(get_db),
):
    """
    관리자용 UI 설정 저장.
    - 인증 없이 저장(요청사항)
    - 요청에 없는 섹션(예: ios_popup)은 기존 값을 유지
    """
    try:
        row = (
            db.query(Community_UI_Config)
            .filter(Community_UI_Config.id == 1)
            .with_for_update()
            .first()
        )
        existing = _normalize_ui_config(getattr(row, "config", None) if row else None)
        patch = payload.model_dump(exclude_unset=True)
        cfg_dict = _normalize_ui_config(_deep_merge_dict(existing, patch))
        if not row:
            row = Community_UI_Config(id=1, config=cfg_dict)
            db.add(row)
        else:
            row.config = cfg_dict
            db.add(row)
        db.flush()
        # 핀 변경 시 슬라이드(5) + 구인(1) 개수 정책을 서버에서 동기화
        if "slide_posts" in patch:
            _rollover_recruit_and_slide_card_types(db)
        db.commit()
        db.refresh(row)
        return {"status": 0, "config": _normalize_ui_config(getattr(row, "config", None))}
    except Exception:
        db.rollback()
        return {"status": 8, "config": _default_ui_config_dict()}

