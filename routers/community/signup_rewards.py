from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Community_User, Notification, PendingSignupReward, Point, Referral
from routers.community.logic import _apply_user_grade_upgrade

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_signup_reward_scheduler = None

SIGNUP_BONUS = 500
REFERRAL_BONUS_REFERRED = 1000
REFERRAL_BONUS_REFERRER = 5000


def _kst_tzinfo():
    if ZoneInfo:
        try:
            return ZoneInfo("Asia/Seoul")
        except Exception:
            pass
    return timezone(timedelta(hours=9))


def process_pending_signup_rewards(db: Session, limit: int = 50) -> dict:
    """pending 상태의 가입 포인트/추천인 보상을 처리한다."""
    rows = (
        db.query(PendingSignupReward)
        .filter(PendingSignupReward.status == "pending")
        .order_by(PendingSignupReward.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    done = 0
    failed = 0

    for job in rows:
        try:
            user = (
                db.query(Community_User)
                .filter(Community_User.id == int(job.user_id))
                .with_for_update()
                .first()
            )
            if not user:
                job.status = "failed"
                job.error_message = "user not found"
                job.processed_at = datetime.now(timezone.utc)
                failed += 1
                continue

            input_code = (job.referral_code_input or "").strip()
            phone_already_saved = bool(job.phone_already_saved)

            if not input_code:
                user.point_balance = int(user.point_balance or 0) + SIGNUP_BONUS
                db.add(Point(user_id=user.id, reason="signup_bonus", amount=SIGNUP_BONUS))
            elif not phone_already_saved:
                if user.referral_code and input_code == user.referral_code:
                    job.status = "failed"
                    job.error_message = "self referral code"
                    job.processed_at = datetime.now(timezone.utc)
                    failed += 1
                    continue

                referrer = (
                    db.query(Community_User)
                    .filter(Community_User.referral_code == input_code)
                    .with_for_update()
                    .first()
                )
                if not referrer:
                    job.status = "failed"
                    job.error_message = "invalid referral code"
                    job.processed_at = datetime.now(timezone.utc)
                    failed += 1
                    continue

                db.add(
                    Referral(
                        referrer_user_id=referrer.id,
                        referred_user_id=user.id,
                        referrer_code=input_code,
                    )
                )
                db.flush()

                referrer.point_balance = int(referrer.point_balance or 0) + REFERRAL_BONUS_REFERRER
                db.add(
                    Point(
                        user_id=referrer.id,
                        reason="referral_bonus_referrer",
                        amount=REFERRAL_BONUS_REFERRER,
                    )
                )

                ref_cnt = (
                    db.query(func.count(Referral.id))
                    .filter(Referral.referrer_user_id == referrer.id)
                    .scalar()
                    or 0
                )
                _apply_user_grade_upgrade(db, referrer, int(ref_cnt))

                user.point_balance = int(user.point_balance or 0) + REFERRAL_BONUS_REFERRED
                db.add(
                    Point(
                        user_id=user.id,
                        reason="referral_bonus_referred",
                        amount=REFERRAL_BONUS_REFERRED,
                    )
                )

                db.add(
                    Notification(
                        user_id=int(referrer.id),
                        type="referral",
                        title="추천인 가입 포인트 지급",
                        body=(
                            f"{user.username}님이 추천인코드로 가입하여 "
                            f"{REFERRAL_BONUS_REFERRER}점이 지급되었습니다."
                        ),
                        data={
                            "referred_username": user.username,
                            "amount": REFERRAL_BONUS_REFERRER,
                            "reason": "referral_bonus_referrer",
                        },
                        is_read=False,
                    )
                )

            job.status = "done"
            job.error_message = None
            job.processed_at = datetime.now(timezone.utc)
            done += 1
            db.commit()
        except IntegrityError as e:
            db.rollback()
            try:
                job = db.query(PendingSignupReward).filter(PendingSignupReward.id == job.id).first()
                if job:
                    job.status = "failed"
                    job.error_message = f"integrity: {e}"
                    job.processed_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception:
                db.rollback()
            failed += 1
        except Exception as e:
            db.rollback()
            try:
                job = db.query(PendingSignupReward).filter(PendingSignupReward.id == job.id).first()
                if job:
                    job.status = "failed"
                    job.error_message = str(e)[:500]
                    job.processed_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception:
                db.rollback()
            failed += 1
            print(f"[WARN] process_pending_signup_rewards failed job={getattr(job, 'id', None)}: {e}")

    return {"done": done, "failed": failed, "picked": len(rows)}


def _signup_reward_job() -> None:
    db = SessionLocal()
    try:
        process_pending_signup_rewards(db)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print("[WARN] signup reward scheduler job failed:", e)
    finally:
        db.close()


def register_signup_reward_startup(app: FastAPI) -> None:
    @app.on_event("startup")
    async def _startup() -> None:
        enabled = (os.getenv("SIGNUP_REWARD_SCHEDULER_ENABLED") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        if not enabled:
            return

        # 기본: 2분마다 처리 + 자정(KST) 한 번 더 drain
        interval_minutes = int(os.getenv("SIGNUP_REWARD_SCHEDULER_INTERVAL_MINUTES") or "2")
        midnight_hour = int(os.getenv("SIGNUP_REWARD_MIDNIGHT_HOUR") or "0")
        midnight_minute = int(os.getenv("SIGNUP_REWARD_MIDNIGHT_MINUTE") or "5")

        global _signup_reward_scheduler
        if _signup_reward_scheduler is not None:
            return

        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        tz = _kst_tzinfo()
        _signup_reward_scheduler = AsyncIOScheduler(
            timezone=tz,
            executors={"default": ThreadPoolExecutor(max_workers=1)},
        )
        _signup_reward_scheduler.add_job(
            _signup_reward_job,
            IntervalTrigger(minutes=max(1, interval_minutes), timezone=tz),
            id="signup_reward_interval",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        _signup_reward_scheduler.add_job(
            _signup_reward_job,
            CronTrigger(hour=midnight_hour, minute=midnight_minute, timezone=tz),
            id="signup_reward_midnight",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        _signup_reward_scheduler.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        global _signup_reward_scheduler
        if _signup_reward_scheduler is None:
            return
        try:
            _signup_reward_scheduler.shutdown(wait=False)
        finally:
            _signup_reward_scheduler = None
