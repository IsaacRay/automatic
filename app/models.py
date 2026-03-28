"""Database models — all 5 tables."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, Index
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
    cron_expression = Column(String(100), nullable=True)
    timezone = Column(String(50), nullable=True, default="America/New_York")
    status = Column(String(20), nullable=False, default="pending")
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class PendingConfirmation(Base):
    __tablename__ = "pending_confirmations"

    id = Column(Integer, primary_key=True)
    user_phone = Column(String(20), nullable=False, index=True)
    action_type = Column(String(30), nullable=False)  # e.g. "reschedule"
    payload = Column(Text, nullable=False)  # JSON text
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc) + timedelta(minutes=10),
    )


class NagSchedule(Base):
    __tablename__ = "nag_schedules"

    id = Column(Integer, primary_key=True)
    user_phone = Column(String(20), nullable=False, index=True)
    label = Column(Text, nullable=False)
    message = Column(Text, nullable=False)
    cron_expression = Column(String(100), nullable=False)
    interval_minutes = Column(Integer, nullable=False)
    max_duration_minutes = Column(Integer, nullable=True)
    timezone = Column(String(50), nullable=False, default="America/New_York")
    next_nag_at = Column(DateTime(timezone=True), nullable=False, index=True)
    nag_until = Column(DateTime(timezone=True), nullable=True)
    active_since = Column(DateTime(timezone=True), nullable=True)
    nag_count = Column(Integer, nullable=False, default=0)
    repeating = Column(Boolean, nullable=False, default=False)
    anchor_to_completion = Column(Boolean, nullable=False, default=False)
    cycle_months = Column(Integer, nullable=True)
    cycle_days = Column(Integer, nullable=True)
    recurrence_description = Column(String(200), nullable=True)
    source = Column(String(50), nullable=True)
    source_ref = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    deadline_at = Column(DateTime(timezone=True), nullable=True)
    min_interval_minutes = Column(Integer, nullable=True)
    max_interval_minutes = Column(Integer, nullable=True)


class ProcessedEmail(Base):
    __tablename__ = "processed_emails"

    id = Column(Integer, primary_key=True)
    message_id = Column(String(255), nullable=False, unique=True, index=True)
    subject = Column(Text, nullable=True)
    date = Column(String(100), nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AppState(Base):
    __tablename__ = "app_state"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ExerciseLog(Base):
    __tablename__ = "exercise_log"

    id = Column(Integer, primary_key=True)
    user_phone = Column(String(20), nullable=False, index=True)
    activity = Column(String(50), nullable=False)      # "run", "bike", "indoor bike"
    duration_minutes = Column(Integer, nullable=True)
    distance_miles = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


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
