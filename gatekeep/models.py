from __future__ import annotations

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gatekeep.db import Base

EMBEDDING_DIM = 384


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class ApiKey(Base):
    """A client's gateway API key, stored as a salted hash rather than plaintext."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # Hard cap on USD spend per calendar month; None means unlimited. Enforced
    # by gatekeep.middleware.budget.require_budget, tracked against cumulative
    # request_logs.cost_usd for the key in the current UTC calendar month.
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


class RequestLog(Base):
    """One logged completion request: tokens used, USD cost, and cache status."""

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cache_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    routed_from: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Prompt(Base):
    """A named prompt template whose active version is served at request time."""

    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    active_version_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "prompt_versions.id", use_alter=True, name="fk_prompts_active_version_id"
        ),
        nullable=True,
    )
    previous_version_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "prompt_versions.id",
            use_alter=True,
            name="fk_prompts_previous_version_id",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class PromptVersion(Base):
    """One immutable version of a prompt's template text."""

    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompts.id"), nullable=False)
    version_num: Mapped[int] = mapped_column(Integer, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CachedResponse(Base):
    """A cached completion response with its embedding, for semantic-cache lookups."""

    __tablename__ = "cached_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    exact_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_messages_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=False
    )
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    prompt_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class RequestSample(Base):
    """A durable, append-only sample of one cache-miss request's content.

    Written on the provider-served (cache-miss) path so curation has a
    representative corpus of real traffic per prompt_name. Deliberately
    decoupled from cached_responses (which is deduped and deleted on every
    prompt promotion) and from request_logs (which stores no message content).
    """

    __tablename__ = "request_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"), nullable=False)
    prompt_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    input_messages: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)


class EvalSuite(Base):
    """A per-prompt eval suite; a prompt version must clear it before promotion."""

    __tablename__ = "eval_suites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    pass_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class EvalCase(Base):
    """One scored eval case: an input, a check type, and its expected result or judge criteria."""

    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("eval_suites.id"), nullable=False)
    input_messages: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    expected: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_type: Mapped[str] = mapped_column(String(32), nullable=False)
    judge_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class EvalRun(Base):
    """One execution of a suite against a specific prompt version and model."""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("eval_suites.id"), nullable=False)
    prompt_version_id: Mapped[int] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    report: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
