from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from sqlalchemy import text

from database import SessionLocal, engine
from models import Community_Post, Community_User, Point

from .logic import _rollover_recruit_and_slide_card_types

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

RECRUIT_POST_BACKFILL_AMOUNT = 1000
RECRUIT_POST_BACKFILL_REASON_PREFIX = "recruit_post_backfill_"


def register_startup(app: FastAPI) -> None:
    app.add_event_handler("startup", _startup_enforce_recruit_card_limits)
    app.add_event_handler("startup", _startup_ensure_community_users_custom_columns)
    app.add_event_handler("startup", _startup_backfill_recruit_post_points)


def ensure_schema() -> None:
    """
    기존 `main.py`에서 import-time에 수행하던 스키마 보정/제약조건 제거/인덱스 생성 등을
    명시적으로 호출하기 위한 진입점입니다.
    """
    # 앱 시작 시 referral/point 테이블의 제약조건을 모두 제거
    _drop_all_constraints_on_table("referral")
    _drop_all_constraints_on_table("point")
    _drop_all_constraints_on_table("cash")

    _ensure_community_users_columns_and_indexes()
    _ensure_community_posts_columns()
    _ensure_pending_signup_rewards_table()
    _ensure_phone_table()
    _ensure_community_user_restrictions_table()


def _startup_enforce_recruit_card_limits() -> None:
    """
    서버 기동 시 구인글(post_type=1)의 card_type 개수 제한을 1회 적용.
    - 슬라이드(card_type=5) 최대 10개(핀+최신)
    - 1유형(card_type=1) 최대 30개
    """
    db = SessionLocal()
    try:
        _rollover_recruit_and_slide_card_types(db)
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print("[WARN] startup recruit/slide card rollover failed:", e)
    finally:
        db.close()


def _startup_ensure_community_users_custom_columns() -> None:
    """
    운영 편의상 서버 기동 시 '없으면 생성' 형태로 스키마를 보정합니다.
    - Base.metadata.create_all()은 신규 테이블만 생성하고 컬럼 추가는 하지 않기 때문
    - 프로젝트는 PostgreSQL(text[])를 사용하므로, postgres 환경에서만 적용합니다.
    """
    try:
        if getattr(engine, "dialect", None) is None:
            return
        if engine.dialect.name != "postgresql":
            return
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE community_users "
                    "ADD COLUMN IF NOT EXISTS custom_role_codes text[] NOT NULL DEFAULT '{}'::text[];"
                )
            )
    except Exception as e:
        # 스키마 보정 실패는 치명적이지 않게 경고만 출력
        print("[WARN] startup schema ensure(custom_role_codes) failed:", e)


def _startup_backfill_recruit_post_points() -> None:
    """
    2026-09-18(KST) 이후 신규 구인글(post_type=1)에 1000P를 1회 지급.
    - reason=recruit_post_backfill_{post_id} 로 재실행해도 중복 지급되지 않음
    - 등록 시점에 이미 recruit_post 로 지급된 글은 건너뜀
    """
    kst = ZoneInfo("Asia/Seoul") if ZoneInfo else timezone(timedelta(hours=9))
    cutoff = datetime(2026, 9, 18, 0, 0, 0, tzinfo=kst).astimezone(timezone.utc)
    db = SessionLocal()
    try:
        posts = (
            db.query(Community_Post.id, Community_Post.user_id, Community_Post.created_at)
            .filter(Community_Post.post_type == 1, Community_Post.created_at >= cutoff)
            .all()
        )
        if not posts:
            return

        existing_reasons = {
            str(r)
            for (r,) in db.query(Point.reason)
            .filter(Point.reason.like(f"{RECRUIT_POST_BACKFILL_REASON_PREFIX}%"))
            .all()
            if r
        }
        live_grants = (
            db.query(Point.user_id, Point.created_at)
            .filter(Point.reason == "recruit_post")
            .all()
        )

        def already_live_granted(user_id: int, created_at) -> bool:
            if created_at is None:
                return False
            post_at = created_at
            if getattr(post_at, "tzinfo", None) is None:
                post_at = post_at.replace(tzinfo=timezone.utc)
            start = post_at - timedelta(minutes=1)
            end = post_at + timedelta(minutes=10)
            for uid, granted_at in live_grants:
                if int(uid) != int(user_id) or granted_at is None:
                    continue
                gat = granted_at
                if getattr(gat, "tzinfo", None) is None:
                    gat = gat.replace(tzinfo=timezone.utc)
                if start <= gat <= end:
                    return True
            return False

        add_by_user: dict[int, int] = defaultdict(int)
        new_rows = 0
        for post_id, user_id, created_at in posts:
            if user_id is None:
                continue
            reason = f"{RECRUIT_POST_BACKFILL_REASON_PREFIX}{int(post_id)}"
            if reason in existing_reasons:
                continue
            if already_live_granted(int(user_id), created_at):
                continue
            db.add(
                Point(
                    user_id=int(user_id),
                    reason=reason,
                    amount=RECRUIT_POST_BACKFILL_AMOUNT,
                )
            )
            add_by_user[int(user_id)] += RECRUIT_POST_BACKFILL_AMOUNT
            existing_reasons.add(reason)
            new_rows += 1

        if new_rows == 0:
            db.rollback()
            return

        for user_id, amount in add_by_user.items():
            user = (
                db.query(Community_User)
                .filter(Community_User.id == user_id)
                .with_for_update()
                .first()
            )
            if not user:
                continue
            user.point_balance = int(user.point_balance or 0) + amount

        db.commit()
        print(
            f"[INFO] recruit post point backfill done: posts={new_rows}, users={len(add_by_user)}"
        )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print("[WARN] startup recruit post point backfill failed:", e)
    finally:
        db.close()


def _drop_all_constraints_on_table(table_name: str) -> None:
    """
    PostgreSQL에서 특정 테이블에 걸린 모든 제약조건(FK/UNIQUE/CHECK 등)을 제거합니다.
    - 사용자 요청: 제약조건 모두 없애기
    - 주의: 데이터 무결성은 애플리케이션 로직으로만 보장됩니다.
    """
    try:
        with engine.begin() as conn:
            # 테이블이 없으면 skip
            exists = conn.execute(
                text("SELECT to_regclass(:tname) IS NOT NULL"),
                {"tname": table_name},
            ).scalar()
            if not exists:
                return

            rows = conn.execute(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid = to_regclass(:tname)
                    """
                ),
                {"tname": table_name},
            ).fetchall()

            for (conname,) in rows:
                conn.execute(text(f'ALTER TABLE "{table_name}" DROP CONSTRAINT IF EXISTS "{conname}" CASCADE'))
    except Exception as e:
        # 제약조건 제거 실패해도 서버는 뜨게 하되, 로그는 남김
        print(f"[WARN] drop constraints failed for {table_name}: {e}")


def _ensure_community_users_columns_and_indexes() -> None:
    """
    Alembic 없이 운영 중인 DB 스키마를 최소한으로 동기화합니다.
    - community_users 신규 컬럼/인덱스가 없으면 생성
    - 이미 존재하면 스킵(에러 없이 idempotent)
    """
    try:
        with engine.begin() as conn:
            # 테이블이 없으면 skip (create_all에서 생성될 수 있음)
            exists = conn.execute(text("SELECT to_regclass('public.community_users') IS NOT NULL")).scalar()
            if not exists:
                return

            # 컬럼 추가 (PostgreSQL: ADD COLUMN IF NOT EXISTS 지원)
            conn.execute(
                text(
                    """
                    ALTER TABLE public.community_users
                      ADD COLUMN IF NOT EXISTS last_recruit_posted_at timestamp with time zone NULL,
                      ADD COLUMN IF NOT EXISTS user_grade smallint NOT NULL DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS is_owner boolean NOT NULL DEFAULT false;
                    """
                )
            )

            # 인덱스 추가
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_community_users_is_owner ON public.community_users (is_owner);")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_community_users_user_grade ON public.community_users (user_grade);")
            )
    except Exception as e:
        # 스키마 동기화 실패해도 서버는 뜨게 하되, 로그는 남김
        print(f"[WARN] ensure community_users columns/indexes failed: {e}")


def _ensure_community_posts_columns() -> None:
    """
    Alembic 없이 운영 중인 DB 스키마를 최소한으로 동기화합니다.
    - community_posts에 필요한 컬럼이 없으면 생성(구인글 등록/수정 시 500 방지)
    - 이미 존재하면 스킵(에러 없이 idempotent)

    주의: PostgreSQL 전용 구문(to_regclass / ADD COLUMN IF NOT EXISTS)을 사용합니다.
    """
    try:
        with engine.begin() as conn:
            exists = conn.execute(text("SELECT to_regclass('public.community_posts') IS NOT NULL")).scalar()
            if not exists:
                return

            conn.execute(
                text(
                    """
                    ALTER TABLE public.community_posts
                      ADD COLUMN IF NOT EXISTS workplace_lat double precision NULL,
                      ADD COLUMN IF NOT EXISTS workplace_lng double precision NULL,
                      ADD COLUMN IF NOT EXISTS business_lat double precision NULL,
                      ADD COLUMN IF NOT EXISTS business_lng double precision NULL,

                      ADD COLUMN IF NOT EXISTS highlight_color varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS highlight_content varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS site_name varchar(255) NULL,

                      ADD COLUMN IF NOT EXISTS total_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS branch_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS hq_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS leader_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS member_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS team_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS each_use boolean NULL,

                      ADD COLUMN IF NOT EXISTS total_fee varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS branch_fee varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS hq_fee varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS leader_fee varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS member_fee varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS team_fee varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS each_fee varchar(255) NULL,

                      ADD COLUMN IF NOT EXISTS pay_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS meal_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS house_use boolean NULL,

                      ADD COLUMN IF NOT EXISTS pay_sup varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS meal_sup boolean NULL,
                      ADD COLUMN IF NOT EXISTS house_sup varchar(255) NULL,

                      ADD COLUMN IF NOT EXISTS item1_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS item1_type varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS item1_sup varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS item2_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS item2_type varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS item2_sup varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS item3_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS item3_type varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS item3_sup varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS item4_use boolean NULL,
                      ADD COLUMN IF NOT EXISTS item4_type varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS item4_sup varchar(255) NULL,

                      ADD COLUMN IF NOT EXISTS agent varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS other_role_name varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS other_role_fee varchar(255) NULL,
                      ADD COLUMN IF NOT EXISTS post_type double precision NULL,
                      ADD COLUMN IF NOT EXISTS card_type double precision NULL,

                      ADD COLUMN IF NOT EXISTS reward_point_amount integer NULL,
                      ADD COLUMN IF NOT EXISTS is_event boolean NULL,

                      ADD COLUMN IF NOT EXISTS status varchar(20) NOT NULL DEFAULT 'published';
                    """
                )
            )
    except Exception as e:
        print(f"[WARN] ensure community_posts columns failed: {e}")


def _ensure_pending_signup_rewards_table() -> None:
    """회원가입 포인트 비동기 큐 테이블 생성."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS public.pending_signup_rewards (
                      id bigserial PRIMARY KEY,
                      user_id integer NOT NULL,
                      referral_code_input varchar(20) NULL,
                      phone_already_saved boolean NOT NULL DEFAULT false,
                      status varchar(20) NOT NULL DEFAULT 'pending',
                      error_message text NULL,
                      created_at timestamptz NOT NULL DEFAULT now(),
                      processed_at timestamptz NULL
                    );
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pending_signup_rewards_user_id ON public.pending_signup_rewards (user_id);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pending_signup_rewards_status ON public.pending_signup_rewards (status);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_pending_signup_rewards_status_created ON public.pending_signup_rewards (status, created_at);"
                )
            )
    except Exception as e:
        print(f"[WARN] ensure pending_signup_rewards table failed: {e}")


def _ensure_phone_table() -> None:
    """
    Alembic 없이 phone 테이블을 생성/동기화합니다.
    요구 스키마:
      create table if not exists phone (
        id         bigserial primary key,
        phone      text not null,
        created_at timestamptz not null default now()
      );
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS public.phone (
                      id bigserial PRIMARY KEY,
                      phone text NOT NULL,
                      created_at timestamptz NOT NULL DEFAULT now()
                    );
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_phone_phone ON public.phone (phone);"))
    except Exception as e:
        print(f"[WARN] ensure phone table failed: {e}")


def _ensure_community_user_restrictions_table() -> None:
    """
    Alembic 없이 community_user_restrictions 테이블을 생성/동기화합니다.
    요구 스키마(Contract/지시서):
      - user_id, post_type(1|3|4), restricted_until(timestamptz|null), reason, created_at, created_by_user_id
      - UNIQUE (user_id, post_type) (업서트 갱신/해제용)
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS public.community_user_restrictions (
                      id bigserial PRIMARY KEY,
                      user_id integer NOT NULL,
                      post_type smallint NOT NULL,
                      restricted_until timestamptz NULL,
                      reason text NULL,
                      created_at timestamptz NOT NULL DEFAULT now(),
                      created_by_user_id integer NULL,
                      CONSTRAINT uq_community_user_restrictions_user_post_type UNIQUE (user_id, post_type)
                    );
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_community_user_restrictions_user_id ON public.community_user_restrictions (user_id);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_community_user_restrictions_post_type ON public.community_user_restrictions (post_type);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_community_user_restrictions_user_post_type ON public.community_user_restrictions (user_id, post_type);"
                )
            )
    except Exception as e:
        print(f"[WARN] ensure community_user_restrictions table failed: {e}")

