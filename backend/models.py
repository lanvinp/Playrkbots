from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, nullable=False)
    telegram_username = Column(String, nullable=True)

    # Playerok данные
    playerok_email = Column(String, nullable=True)
    playerok_token = Column(String, nullable=True)
    playerok_user_id = Column(String, nullable=True)
    playerok_username = Column(String, nullable=True)

    # Настройки
    auto_reply_enabled = Column(Boolean, default=True)
    auto_reply_text = Column(Text, default="Спасибо за заказ! Выполняю как можно скорее.")
    check_interval = Column(Integer, default=30)

    # Статус
    is_authorized = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, nullable=False)
    level = Column(String, default="INFO")  # INFO, WARNING, ERROR
    action = Column(String, nullable=False)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, nullable=False)
    email = Column(String, nullable=False)
    step = Column(String, default="awaiting_code")  # awaiting_code, done
    created_at = Column(DateTime, default=datetime.utcnow)
