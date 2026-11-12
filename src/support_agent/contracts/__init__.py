from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Kind = Literal["feature_request", "defect", "service_request", "incident", "unclear"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(StrictModel):
    segment_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=500)


class Item(StrictModel):
    kind: Kind
    urgency: Literal["critical", "high", "normal", "unknown"]
    need: str = Field(min_length=3, max_length=500)
    context: str = Field(max_length=500)
    evidence: list[Evidence] = Field(min_length=1, max_length=3)
    needs_review: bool
    review_reason: str | None = Field(max_length=300)


class Extraction(StrictModel):
    items: list[Item] = Field(max_length=20)


class Match(StrictModel):
    action: Literal["match", "new", "uncertain"]
    candidate_id: str | None
    reason: str = Field(max_length=300)
    evidence: list[Evidence] = Field(max_length=3)


class Attachment(StrictModel):
    id: str = Field(pattern=r"^[\w-]{1,64}$")
    content_ref: str = Field(pattern=r"^[\w./-]{1,200}$")
    declared_type: str = Field(max_length=100)
    transcript: str | None = Field(default=None, max_length=50000)
    transcript_quality: Literal["usable", "poor", "unknown"] = "unknown"


class TicketContent(StrictModel):
    external_id: str = Field(pattern=r"^[\w-]{1,100}$")
    version: str = Field(min_length=1, max_length=100)
    text: str = Field(max_length=200000)
    channel: Literal["text", "audio", "document", "image"]
    occurred_at: datetime
    attachments: list[Attachment] = Field(default_factory=list, max_length=32)
    duplicate_of: str | None = Field(default=None, max_length=100)

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, v):
        if v.tzinfo is None:
            raise ValueError("timezone required")
        return v

    @model_validator(mode="after")
    def unique_attachments(self):
        ids = [attachment.id for attachment in self.attachments]
        if len(ids) != len(set(ids)):
            raise ValueError("attachment IDs must be unique within the ticket")
        return self


class Change(StrictModel):
    sequence: int = Field(ge=1)
    external_id: str = Field(pattern=r"^[\w-]{1,100}$")
    version: str = Field(min_length=1, max_length=100)
    operation: Literal["upsert", "delete", "access_revoked"]
    changed_at: datetime
    backfill: bool = False

    @field_validator("changed_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value


class ChangeBatch(StrictModel):
    changes: list[Change] = Field(max_length=200)
    next_cursor: str = Field(min_length=1, max_length=200)
    has_more: bool


class Segment(StrictModel):
    segment_id: str = Field(min_length=1, max_length=100)
    source_ref: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=40)
    page: int | None = Field(default=None, ge=1)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    text: str = Field(max_length=200000)
    quality: Literal["usable", "poor", "unknown"] = "unknown"
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    processor_version: str = Field(min_length=1, max_length=100)


class DecisionRequest(StrictModel):
    revision: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["accepted", "rejected", "needs_clarification"]
    reason: str = Field(min_length=1, max_length=500)


class DeliveryResolutionRequest(StrictModel):
    resolution: Literal["delivered", "not_delivered", "corrected"]
    reason: str = Field(min_length=1, max_length=500)
    external_id: str | None = Field(default=None, min_length=1, max_length=100)
