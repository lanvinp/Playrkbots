from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from database import init_db, get_db
from models import User, Log
import os
from datetime import datetime

app = FastAPI(title="Playerok Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ADMIN_KEY = os.getenv("ADMIN_KEY", "changeme")


def check_admin(key: str):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


@app.on_event("startup")
async def startup():
    await init_db()


# ─── Админ роуты ───────────────────────────────────────────────────────────────

@app.get("/admin/users")
async def get_users(key: str, db: AsyncSession = Depends(get_db)):
    check_admin(key)
    result = await db.execute(select(User).order_by(desc(User.created_at)))
    users = result.scalars().all()
    return [
        {
            "telegram_id": u.telegram_id,
            "telegram_username": u.telegram_username,
            "playerok_username": u.playerok_username,
            "playerok_email": u.playerok_email,
            "is_authorized": u.is_authorized,
            "is_active": u.is_active,
            "created_at": u.created_at,
            "last_seen": u.last_seen,
        }
        for u in users
    ]


@app.get("/admin/logs")
async def get_logs(key: str, telegram_id: str = None, limit: int = 100, db: AsyncSession = Depends(get_db)):
    check_admin(key)
    query = select(Log).order_by(desc(Log.created_at)).limit(limit)
    if telegram_id:
        query = query.where(Log.telegram_id == telegram_id)
    result = await db.execute(query)
    logs = result.scalars().all()
    return [
        {
            "telegram_id": l.telegram_id,
            "level": l.level,
            "action": l.action,
            "details": l.details,
            "created_at": l.created_at,
        }
        for l in logs
    ]


@app.get("/admin/stats")
async def get_stats(key: str, db: AsyncSession = Depends(get_db)):
    check_admin(key)
    total = await db.execute(select(User))
    authorized = await db.execute(select(User).where(User.is_authorized == True))
    return {
        "total_users": len(total.scalars().all()),
        "authorized_users": len(authorized.scalars().all()),
    }


# ─── Внутренние роуты (для бота) ──────────────────────────────────────────────

@app.get("/internal/users")
async def get_internal_users(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.is_authorized == True, User.is_active == True)
    )
    users = result.scalars().all()
    return [
        {
            "telegram_id": u.telegram_id,
            "playerok_token": u.playerok_token,
            "playerok_user_id": u.playerok_user_id,
            "playerok_username": u.playerok_username,
            "is_authorized": u.is_authorized,
            "is_active": u.is_active,
            "auto_reply_enabled": u.auto_reply_enabled,
            "auto_reply_text": u.auto_reply_text,
            "auto_bump_enabled": u.auto_bump_enabled,
            "auto_restore_enabled": u.auto_restore_enabled,
            "bump_interval": u.bump_interval,
            "restore_interval": u.restore_interval,
            "check_interval": u.check_interval,
        }
        for u in users
    ]


@app.post("/internal/log")
async def add_log(data: dict, db: AsyncSession = Depends(get_db)):
    try:
        log = Log(
            telegram_id=data["telegram_id"],
            level=data.get("level", "INFO"),
            action=data["action"],
            details=data.get("details"),
        )
        db.add(log)
        await db.commit()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/internal/user/update")
async def update_user(data: dict, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(User).where(User.telegram_id == str(data["telegram_id"])))
        user = result.scalar_one_or_none()

        if not user:
            user = User(telegram_id=str(data["telegram_id"]))
            db.add(user)

        for key, value in data.items():
            if hasattr(user, key) and key != "telegram_id":
                setattr(user, key, value)

        user.last_seen = datetime.utcnow()
        await db.commit()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/internal/user/{telegram_id}")
async def get_user(telegram_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        return None
    return {
        "telegram_id": user.telegram_id,
        "playerok_token": user.playerok_token,
        "playerok_user_id": user.playerok_user_id,
        "playerok_username": user.playerok_username,
        "is_authorized": user.is_authorized,
        "auto_reply_enabled": user.auto_reply_enabled,
        "auto_reply_text": user.auto_reply_text,
        "auto_bump_enabled": user.auto_bump_enabled,
        "auto_restore_enabled": user.auto_restore_enabled,
        "bump_interval": user.bump_interval,
        "restore_interval": user.restore_interval,
        "check_interval": user.check_interval,
    }
