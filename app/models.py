"""Database models — all 4 tables."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, Index
from app.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class Reminder(Base):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True)
    user_phone = Column(String(20), nullable=False, index=True)
    label = Column(Text, nullable=False)
    fire_at = Column(DateTime(timezone=True), nullable=False, index=True)
    message = Column(Text, nullable=False)
    parent_event_id = Column(String(64), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class RecurringSchedule(Base):
    __tablename__ = "recurring_schedules"

    id = Column(Integer, primary_key=True)
    user_phone = Column(String(20), nullable=False, index=True)
    label = Column(Text, nullable=False)
    message_prompt = Column(Text, nullable=False)
    cron_expression = Column(String(100), nullable=False)
    timezone = Column(String(50), nullable=False, default="America/New_York")
    next_fire_at = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ActionItem(Base):
    __tablename__ = "action_items"

    id = Column(Integer, primary_key=True)
    user_phone = Column(String(20), nullable=False, index=True)
    source = Column(String(50), nullable=False)
    source_ref = Column(Text, nullable=True)
    description = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    remind_count = Column(Integer, nullable=False, default=0)
    next_remind_at = Column(DateTime(timezone=True), nullable=True, index=True)
    snooze_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class SmsLog(Base):
    __tablename__ = "sms_log"

    id = Column(Integer, primary_key=True)
    direction = Column(String(10), nullable=False)
    phone = Column(String(20), nullable=False)
    body = Column(Text, nullable=False)
    twilio_sid = Column(String(64), nullable=True)
    related_type = Column(String(30), nullable=True)
    related_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
