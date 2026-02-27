"""Pydantic models for validating OpenAI response structures."""

from pydantic import BaseModel, Field


class ReminderData(BaseModel):
    message: str
    fire_at: str  # ISO 8601 UTC


class CreateReminderIntent(BaseModel):
    label: str
    reminders: list[ReminderData]
    parent_event_id: str | None = None


class CreateRecurringIntent(BaseModel):
    label: str
    cron_expression: str
    message_prompt: str


class AcknowledgeIntent(BaseModel):
    keyword: str | None = None
    all: bool = False


class SnoozeIntent(BaseModel):
    duration_minutes: int = 60
    keyword: str | None = None


class ParsedIntent(BaseModel):
    intent: str
    data: dict = Field(default_factory=dict)
