from __future__ import annotations

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gatekeep.db import Base

EMBEDDING_DIM = 384


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class Account(Base):
    """A tenant (team) that owns API keys and all data captured through them.

    Accounts are the tenancy root: keys are disposable credentials onto an
    account, and every content/usage row is scoped to the account derived
    server-side from the authenticated key. `monthly_budget_usd` is the
    account's shared monthly spend pool (None means unlimited); `is_operator`
    grants the fleet-wide dashboard view (decision 6). There is deliberately
    no role hierarchy or RBAC - operator status is a single boolean.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Shared monthly USD spend cap for the whole account; None means unlimited.
    # Enforced by gatekeep.middleware.budget against cumulative
    # request_logs.cost_usd for the account in the current UTC calendar month.
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_operator: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ApiKey(Base):
    """A client's gateway API key, stored as a salted hash rather than plaintext.

    A key is a disposable credential onto its `Account`: rotating or revoking
    it never orphans history, which hangs off the account. `name` is unique
    only within an account (decision 7), so one tenant's labels never collide
    with another's namespace.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (UniqueConstraint("account_id", "name", name="uq_api_keys_account_id_name"),)


class RequestLog(Base):
    """One logged completion request: tokens used, USD cost, and cache status.

    `prompt_version_num` records which PromptVersion actually served the
    request (active or A/B candidate) when `prompt_name` is set, so cost/
    eval/quality can later be compared active-vs-candidate by version.
    """

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"), nullable=False)
    # Denormalized tenant attribution, written at capture time from the
    # authenticated key (decision 9). Kept on the row rather than joined through
    # key_id so attribution survives key rotation or revocation, and so
    # account-scoped dashboard/budget aggregates need no join.
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
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
    prompt_version_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Latency, in milliseconds. All three are nullable because each is
    # genuinely undefined in some cases, not merely unknown:
    #   duration_ms: request start until just before log_request, so on every
    #     path it is slightly smaller than the full-ASGI figure in
    #     gatekeep_request_duration_seconds - it excludes JSON serialization
    #     and the socket write on the non-streaming path, and the trailing
    #     events plus body teardown on the streaming one. On the streaming path
    #     log_request fires at StreamEnd, so it is time-to-last-token and
    #     matches gatekeep_time_to_last_token_seconds.
    #   provider_ms: time in the upstream call. NULL on a cache hit (no call
    #     was made). A NULL alone cannot distinguish a cache hit from a row
    #     predating this migration - disambiguate on `cached`, never on
    #     `provider_ms IS NULL`.
    #   ttft_ms: time to first token. NULL on every non-streamed request,
    #     where the concept does not exist.
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Which branch served the request: "cache_exact", "cache_semantic",
    # "provider", or "stream". Carries exactly the values the Prometheus
    # `path` label carries (observability/metrics.py), written from the same
    # parameter that feeds mark(), so the two stores cannot drift.
    #
    # NULL only on rows written before migration 0012. Nothing after the fact
    # can tell a streamed pre-0012 row from a non-streamed one, so latency
    # queries filter `path IS NOT NULL` rather than guessing.
    path: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Which of "ok", "provider_error", "client_disconnect" this request
    # ended as. NULL on any row written before this column existed (or by a
    # caller that doesn't pass it) - treated as "ok"-equivalent everywhere
    # this is read (dashboard.py's _latency_filters, the success-rate
    # aggregate), since failed rows were never logged at all before #17.
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        # Speeds up budget.get_period_spend's DB-fallback aggregate, which
        # filters by key_id and created_at >= period_start.
        Index("ix_request_logs_key_id_created_at", "key_id", "created_at"),
        # The composite above cannot serve the dashboard's time-only window
        # scans (key_id is the leading column), and percentile_cont sorts
        # every row it is handed, so narrowing the window cheaply matters.
        Index("ix_request_logs_created_at", "created_at"),
        # Account-scoped dashboard and budget aggregates (decisions 5, 6, 9)
        # filter by account_id + created_at; this composite serves them.
        Index("ix_request_logs_account_id_created_at", "account_id", "created_at"),
    )


class Prompt(Base):
    """A named prompt template whose active version is served at request time.

    `candidate_version_id`/`candidate_traffic_pct` optionally configure an
    A/B testing traffic split: when both are set, `resolve_prompt_version_for_request`
    routes `candidate_traffic_pct` percent of requests to the candidate
    version and the rest to `active_version_id`. Unset (the default) means
    no split - 100% of traffic goes to the active version, i.e. today's
    behavior. A candidate is deliberately *not* "active": setting one does
    not flip `active_version_id`, run the eval gate, or invalidate caches -
    see `set_candidate_version`/`clear_candidate_version` in prompts.py.
    """

    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    active_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_versions.id", use_alter=True, name="fk_prompts_active_version_id"),
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
    candidate_version_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "prompt_versions.id",
            use_alter=True,
            name="fk_prompts_candidate_version_id",
        ),
        nullable=True,
    )
    candidate_traffic_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    """A cached completion response with its embedding, for semantic-cache lookups.

    `prompt_version_num` records which PromptVersion's rendered template
    text produced this row when `prompt_name` is set. This matters once a
    prompt has an A/B candidate: two rows tagged with the same `prompt_name`
    can now come from two different templates (active vs. candidate), and
    the embedding-similarity match alone can't reliably tell them apart when
    the templates are only a small wording tweak apart (a common A/B test).
    `find_semantic_match` filters on this column so a request only reuses a
    semantically-similar response that was generated by the same version it
    just resolved to.
    """

    __tablename__ = "cached_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # Tenant the cached response belongs to (decision 1). Partitions the cache
    # so one caller's completion is never served verbatim to another; exact_hash
    # is unique per (account_id, exact_hash), not globally.
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    exact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_messages_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    prompt_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_version_num: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        # Speeds up find_semantic_match's equality filters (model, and
        # optionally prompt_version_num for A/B-scoped lookups) before it
        # sorts the matches by cosine distance.
        Index(
            "ix_cached_responses_model_prompt_version_num",
            "model",
            "prompt_version_num",
        ),
        # exact_hash is unique per account, not globally (decision 1), so two
        # tenants can independently cache the same request.
        UniqueConstraint(
            "account_id",
            "exact_hash",
            name="uq_cached_responses_account_id_exact_hash",
        ),
    )


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
    # Denormalized tenant attribution written at capture time (decision 4).
    # Kept on the row rather than joined through key_id so provenance filtering
    # and per-tenant deletion survive key rotation or revocation; this is the
    # substrate the eval-case provenance tags (decision 3) are derived from.
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
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
    # The account whose sample this case was curated from (decision 3).
    # NULL for manually authored cases, which have no originating tenant.
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class EvalRun(Base):
    """One execution of a suite against a specific prompt version and model."""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("eval_suites.id"), nullable=False)
    prompt_version_id: Mapped[int] = mapped_column(ForeignKey("prompt_versions.id"), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    report: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
