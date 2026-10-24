from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import settings
from deps import get_db
from models import (
    Community_Phone_Verification,
    Community_User,
    PendingSignupReward,
    Phone,
)

from .notifications import notify_admin_acknowledged_event
from .phone import _is_valid_korean_phone, _normalize_phone
from .referral_code import assign_referral_code
from .signup_rewards import (
    REFERRAL_BONUS_REFERRED,
    REFERRAL_BONUS_REFERRER,
    SIGNUP_BONUS,
)

router = APIRouter()

SECRET_KEY = settings.SECRET_KEY
ALGORITHM = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES


class LoginRequest2(BaseModel):
    username: str
    password: str
    push_token: str | None = None


class LoginResponse(BaseModel):
    user_id: int
    token: str


class SignupRequest_C(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=2, max_length=255)
    password_confirm: str = Field(min_length=2, max_length=255)
    name: str | None = Field(default=None, max_length=50)
    phone_number: str | None = Field(default=None, max_length=20)
    phone_verification_id: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=100)
    referral_code: str | None = Field(default=None, max_length=20)
    # community_users 신규 필드(2026-01)
    marketing_consent: bool = False
    custom_industry_codes: list[str] = Field(default_factory=list)
    custom_region_codes: list[str] = Field(default_factory=list)
    # 지역현장: 선호지역(세부지역 포함)
    area_region_codes: list[str] = Field(default_factory=list)
    # 맞춤현장: 모집(총괄/본부장/팀장/팀원/기타)
    custom_role_codes: list[str] = Field(default_factory=list)


@router.get("/community/username/check")
def community_check_username(
    username: str,
    exclude: str | None = None,
    db: Session = Depends(get_db),
):
    """아이디(닉네임) 중복확인. status 0=사용가능, 1=중복, 2=형식오류."""
    name = (username or "").strip()
    if len(name) < 2 or len(name) > 50:
        return {"status": 2, "available": False, "detail": "닉네임은 2~50글자여야 합니다."}

    q = db.query(Community_User.id).filter(Community_User.username == name)
    exclude_name = (exclude or "").strip()
    if exclude_name:
        q = q.filter(Community_User.username != exclude_name)
    if q.first() is not None:
        return {"status": 1, "available": False}
    return {"status": 0, "available": True}


@router.post("/community/signup")
def community_signup(req: SignupRequest_C, db: Session = Depends(get_db)):
    if db.query(Community_User).filter(Community_User.username == req.username).first():
        return {"status": 1}

    if req.password != req.password_confirm:
        return {"status": 2}

    if req.name is None:
        return {"status": 3}

    if req.phone_number is None:
        return {"status": 4}

    # 휴대폰 인증 강제
    digits = _normalize_phone(req.phone_number)
    if not _is_valid_korean_phone(digits):
        raise HTTPException(status_code=400, detail="올바른 휴대폰 번호를 입력해 주세요.")

    if not req.phone_verification_id:
        return {"status": 9, "detail": "휴대폰 인증이 필요합니다."}
    try:
        vid = uuid.UUID(req.phone_verification_id)
    except Exception:
        return {"status": 9, "detail": "휴대폰 인증이 필요합니다."}

    vrow = db.query(Community_Phone_Verification).filter(Community_Phone_Verification.id == vid).first()
    now = datetime.now(tz=timezone.utc)
    if (
        (not vrow)
        or (vrow.phone_number != digits)
        or (vrow.verified_at is None)
        or (vrow.expires_at is not None and vrow.expires_at <= now)
    ):
        return {"status": 9, "detail": "휴대폰 인증이 필요합니다."}

    # phone 테이블 기준 "기존에 사용된 번호"인지 확인 (추천인 시스템 차단용)
    phone_already_saved = db.query(Phone.id).filter(Phone.phone == digits).first() is not None

    pw_hash = hashlib.sha256(req.password.encode()).hexdigest()

    user = Community_User(
        username=req.username,
        password_hash=pw_hash,
        name=req.name,
        phone_number=digits,
        region=req.region,
        signup_date=date.today(),
        # 정책(2026-01): 일반회원 기본 등급은 -1
        user_grade=-1,
        marketing_consent=bool(req.marketing_consent),
        custom_industry_codes=list(req.custom_industry_codes or []),
        custom_region_codes=list(req.custom_region_codes or []),
        area_region_codes=list(getattr(req, "area_region_codes", None) or []),
        custom_role_codes=list(getattr(req, "custom_role_codes", None) or []),
    )
    db.add(user)
    db.flush()

    # referral_code 생성 및 할당
    try:
        assign_referral_code(db, user, req.phone_number)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        print(f"[ERROR] referral_code 할당 중 예상치 못한 오류: {e}")
        raise HTTPException(status_code=500, detail="회원가입 처리 중 오류가 발생했습니다.")

    # phone 테이블에 번호 영구 저장 (중복이면 삽입 스킵)
    if not phone_already_saved:
        db.add(Phone(phone=digits))

    signup_bonus_amount = 0
    referral_bonus_referred_amount = 0
    referral_bonus_referrer_amount = 0

    input_code = (req.referral_code or "").strip()

    # 추천인코드 검증만 동기 수행(포인트 지급은 스케줄러로 분리해 가입 응답 부하 감소)
    if not input_code:
        signup_bonus_amount = SIGNUP_BONUS
    elif not phone_already_saved:
        if user.referral_code and input_code == user.referral_code:
            db.rollback()
            return {"status": 6, "detail": "본인 추천인코드는 사용할 수 없습니다."}

        referrer = db.query(Community_User).filter(Community_User.referral_code == input_code).first()
        if not referrer:
            db.rollback()
            return {"status": 6, "detail": "추천인코드가 올바르지 않습니다."}

        referral_bonus_referred_amount = REFERRAL_BONUS_REFERRED
        referral_bonus_referrer_amount = REFERRAL_BONUS_REFERRER

    db.add(
        PendingSignupReward(
            user_id=int(user.id),
            referral_code_input=input_code or None,
            phone_already_saved=bool(phone_already_saved),
            status="pending",
        )
    )

    db.commit()

    # 회원가입 푸쉬 알림(관리자 대상: admin_acknowledged=True) - 실패해도 회원가입 성공 처리
    try:
        notify_admin_acknowledged_event(
            db,
            title="회원가입 알림",
            body=f"새 회원 가입: {user.username}",
            data={"event": "signup", "username": user.username},
        )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print("[WARN] notify_admin_acknowledged_event(signup) failed:", e)

    return {
        "status": 0,
        "signup_bonus_amount": signup_bonus_amount,
        "referral_bonus_referred_amount": referral_bonus_referred_amount,
        "referral_bonus_referrer_amount": referral_bonus_referrer_amount,
        "reward_pending": True,
    }


@router.post("/community/login")
def community_login(req: LoginRequest2, db: Session = Depends(get_db)):
    user = db.query(Community_User).filter(Community_User.username == req.username).first()
    if not user:
        return {"status": 1, "detail": "아이디 또는 비밀번호를 확인하세요."}

    pw_hash = hashlib.sha256(req.password.encode()).hexdigest()
    if user.password_hash != pw_hash:
        return {"status": 1, "detail": "아이디 또는 비밀번호를 확인하세요."}

    if req.push_token:
        user.push_token = req.push_token
        db.commit()

    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = jwt.encode({"sub": str(user.id), "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)

    return {"status": 0, "user_id": user.id, "token": token}

