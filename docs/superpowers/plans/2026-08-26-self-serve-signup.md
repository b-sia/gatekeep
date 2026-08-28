# Self-Serve Signup with Operator Approval - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a new user sign up with email + password, wait for operator approval, then log in and self-manage their own Gatekeep API keys.

**Architecture:** A login user is 1:1 with an `Account` (new `status` column). New `account_credentials`, `sessions`, and `email_tokens` tables back password login, server-side sessions, and one-time email links. Dashboard management routes resolve identity from a session cookie OR an API key; the gateway/proxy routes are unchanged. A pluggable email layer (console/SMTP) sends verification, reset, and approval mail. The React dashboard is re-gated on a session instead of a pasted key.

**Tech Stack:** FastAPI, SQLAlchemy 2 (async) + Alembic, pydantic-settings, passlib[bcrypt], stdlib smtplib, React 18 + Vite + Vitest, pytest-asyncio + httpx ASGI.

**Spec:** `docs/superpowers/specs/2026-08-26-self-serve-signup-design.md`

## Global Constraints

- **Docstrings required** on every new function, method, and class (purpose, args, returns, raises where applicable).
- **Ruff** lint + format must pass (pre-commit runs it). No em dashes in any text/comment - use a plain `-`.
- **Password hashing:** bcrypt via `passlib[bcrypt]`. Never reuse SHA-256 (API-key hashing) for passwords.
- **Session/email tokens:** raw value = `secrets.token_urlsafe`; store only its SHA-256 hex hash; match by re-hashing. Raw values are never persisted.
- **No user enumeration:** `signup`, `login`, and `password/reset-request` must return identical responses whether or not the email exists / password is right (login uses one generic 401).
- **Backward compatibility:** `accounts.status` server-defaults to `"approved"`. `scripts/create_key.py`, `scripts/init-test-key.sh`, and `gatekeep account`/`key` CLI must keep creating usable (approved, credential-less) accounts unchanged.
- **Route prefix:** the dashboard router is mounted at `/dashboard/api`. All new management/auth routes live under that prefix.
- **Tests** rebuild the schema from `Base.metadata` each test (see `tests/conftest.py`), so every new model MUST be imported in `gatekeep/storage/models.py` to register. Migrations are exercised separately.
- **Cookie names:** session cookie `gk_session` (HttpOnly, Secure, SameSite=Lax); CSRF cookie `gk_csrf` (not HttpOnly, SameSite=Lax). CSRF header `X-CSRF-Token`.

---

## File Structure

**Backend - create:**
- `gatekeep/accounts/passwords.py` - bcrypt hash/verify.
- `gatekeep/accounts/tokens.py` - opaque token generate/hash.
- `gatekeep/accounts/sessions.py` - server-side session create/resolve/revoke.
- `gatekeep/accounts/auth_service.py` - signup, verify-email, login, logout, password reset, operator approve/reject/list-pending.
- `gatekeep/email/__init__.py` - `get_email_backend()` factory.
- `gatekeep/email/backends.py` - `EmailBackend` protocol, `ConsoleEmailBackend`, `SmtpEmailBackend`.
- `gatekeep/email/messages.py` - subject/body builders for the three mails.
- `gatekeep/api/auth.py` - unauthenticated auth router + CSRF dependency + cookie helpers.
- `migrations/versions/0025_signup_auth.py` - status column + three tables.

**Backend - modify:**
- `gatekeep/storage/models.py` - `Account.status`; `AccountCredential`, `Session`, `EmailToken` models.
- `gatekeep/accounts/account_service.py` - `create_account` gains a `status` param.
- `gatekeep/config.py` - email/session settings.
- `gatekeep/api/dashboard.py` - session-or-key resolution, `require_approved`, operator approve/reject/list-pending routes, `status` in `MeResponse`/`AccountOut`.
- `gatekeep/app.py` - include the auth router.

**Frontend - create:**
- `dashboard/src/api/auth.ts` - auth API calls.
- `dashboard/src/pages/{LoginPage,SignupPage,VerifyEmailPage,ForgotPasswordPage,ResetPasswordPage,PendingApprovalPage}.tsx`.
- `dashboard/src/components/PendingRequestsPanel.tsx`.

**Frontend - modify:**
- `dashboard/src/api/client.ts` - cookie + CSRF; drop bearer.
- `dashboard/src/App.tsx` - session gating/routing.
- `dashboard/src/pages/ManagementPage.tsx` - mount PendingRequestsPanel for operators.

**Frontend - delete:**
- `dashboard/src/components/IdentityPicker.tsx`, `dashboard/src/api/identityStore.ts` (+ their tests).

---

## Phase 1 - Data model & migration

### Task 1: Account.status + credential/session/email-token models

**Files:**
- Modify: `gatekeep/storage/models.py`
- Test: `tests/storage/test_signup_models.py`

**Interfaces:**
- Produces: `Account.status: str`; models `AccountCredential(id, account_id, email, password_hash, email_verified, created_at, updated_at)`, `Session(id, token_hash, account_id, created_at, expires_at, last_seen_at)`, `EmailToken(id, purpose, token_hash, account_id, expires_at, used_at)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/storage/test_signup_models.py
import pytest
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import Account, AccountCredential, EmailToken, Session
from tests.helpers import create_account


@pytest.mark.asyncio
async def test_account_status_defaults_to_approved():
    async with SessionLocal() as s:
        account = await create_account(s)
        await s.commit()
        await s.refresh(account)
        assert account.status == "approved"


@pytest.mark.asyncio
async def test_credential_session_and_token_persist():
    async with SessionLocal() as s:
        account = await create_account(s, status="pending")
        s.add(AccountCredential(
            account_id=account.id, email="a@b.com",
            password_hash="x", email_verified=False,
        ))
        s.add(Session(token_hash="th", account_id=account.id,
                      expires_at=__import__("datetime").datetime(2999, 1, 1)))
        s.add(EmailToken(purpose="verify_email", token_hash="et",
                         account_id=account.id,
                         expires_at=__import__("datetime").datetime(2999, 1, 1)))
        await s.commit()
        cred = (await s.get(AccountCredential, 1))
        assert cred.email == "a@b.com" and cred.email_verified is False
```

Note: `tests/helpers.create_account` gains a `status` kwarg in this task (add `status: str = "approved"` and pass it to `Account(...)`).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/storage/test_signup_models.py -v`
Expected: FAIL - `AttributeError`/`ImportError` (models/column don't exist).

- [ ] **Step 3: Implement the models**

Add to `gatekeep/storage/models.py` (reuse the file's existing imports: `Mapped`, `mapped_column`, `String`, `Boolean`, `Integer`, `DateTime`, `ForeignKey`, `_utcnow`):

```python
class Account(Base):
    ...  # existing columns unchanged
    # Lifecycle for self-serve signup. Server-defaults to "approved" so every
    # existing row and all CLI/script-created accounts stay fully usable; only
    # self-serve signups start "pending". Values: pending|approved|rejected|disabled.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="approved", default="approved"
    )


class AccountCredential(Base):
    """Email + password login for an Account (1:1). Absent for CLI/programmatic accounts."""

    __tablename__ = "account_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), unique=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Session(Base):
    """A server-side login session; the raw token lives only in the cookie."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class EmailToken(Base):
    """A single-use email link token (verify_email or reset_password)."""

    __tablename__ = "email_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Also update `tests/helpers.py::create_account` to accept `status: str = "approved"` and pass it into `Account(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/storage/test_signup_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/storage/models.py tests/storage/test_signup_models.py tests/helpers.py
git commit -m "feat(storage): add account status + credential/session/email-token models"
```

### Task 2: Alembic migration 0025

**Files:**
- Create: `migrations/versions/0025_signup_auth.py`
- Test: `tests/storage/test_migration_0025.py`

**Interfaces:**
- Consumes: models from Task 1.
- Produces: DB schema parity between Alembic head and `Base.metadata`.

- [ ] **Step 1: Write the failing test**

```python
# tests/storage/test_migration_0025.py
import pytest
from sqlalchemy import inspect
from gatekeep.storage.db import engine


@pytest.mark.asyncio
async def test_signup_tables_and_status_column_exist():
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert {"account_credentials", "sessions", "email_tokens"} <= set(names)
        cols = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("accounts")]
        )
        assert "status" in cols
```

(The autouse `_create_schema` fixture builds tables from metadata, so this asserts the *models* are correct; the migration is verified by review + running `alembic upgrade head` in Step 4.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/storage/test_migration_0025.py -v`
Expected: PASS only if Task 1 merged; if run before Task 1, FAIL. Proceed to write the migration regardless (schema-vs-migration drift is the real target).

- [ ] **Step 3: Write the migration**

```python
# migrations/versions/0025_signup_auth.py
"""signup auth: account status + credentials, sessions, email tokens

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="approved"),
    )
    op.create_table(
        "account_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False, unique=True),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sessions_account_id", "sessions", ["account_id"])
    op.create_table(
        "email_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_email_tokens_account_id", "email_tokens", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_email_tokens_account_id", table_name="email_tokens")
    op.drop_table("email_tokens")
    op.drop_index("ix_sessions_account_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("account_credentials")
    op.drop_column("accounts", "status")
```

- [ ] **Step 4: Verify migration applies cleanly**

Run against the test DB:
`alembic downgrade base && alembic upgrade head && pytest tests/storage/test_migration_0025.py -v`
Expected: upgrade/downgrade succeed; test PASS.

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0025_signup_auth.py tests/storage/test_migration_0025.py
git commit -m "feat(migrations): add signup auth schema (0025)"
```

---

## Phase 2 - Primitives

### Task 3: Password hashing

**Files:**
- Create: `gatekeep/accounts/passwords.py`
- Test: `tests/accounts/test_passwords.py`

**Interfaces:**
- Produces: `hash_password(raw: str) -> str`, `verify_password(raw: str, password_hash: str) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/accounts/test_passwords.py
from gatekeep.accounts.passwords import hash_password, verify_password


def test_hash_is_salted_and_verifies():
    h1 = hash_password("hunter2")
    h2 = hash_password("hunter2")
    assert h1 != h2  # per-hash salt
    assert verify_password("hunter2", h1)
    assert not verify_password("wrong", h1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/accounts/test_passwords.py -v`
Expected: FAIL - module missing.

- [ ] **Step 3: Add dependency + implement**

Add `passlib[bcrypt]>=1.7` to `[project].dependencies` in `pyproject.toml`, then `pip install -e .`.

```python
# gatekeep/accounts/passwords.py
from __future__ import annotations

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    """Return a salted bcrypt hash of a plaintext password."""
    return _pwd_context.hash(raw)


def verify_password(raw: str, password_hash: str) -> bool:
    """Return True if `raw` matches the stored bcrypt `password_hash`."""
    return _pwd_context.verify(raw, password_hash)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/accounts/test_passwords.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml gatekeep/accounts/passwords.py tests/accounts/test_passwords.py
git commit -m "feat(accounts): add bcrypt password hashing"
```

### Task 4: Opaque token generate/hash

**Files:**
- Create: `gatekeep/accounts/tokens.py`
- Test: `tests/accounts/test_tokens.py`

**Interfaces:**
- Produces: `new_token() -> str`, `hash_token(raw: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/accounts/test_tokens.py
from gatekeep.accounts.tokens import hash_token, new_token


def test_new_token_unique_and_hash_stable():
    a, b = new_token(), new_token()
    assert a != b and len(a) > 20
    assert hash_token(a) == hash_token(a)
    assert len(hash_token(a)) == 64  # sha256 hex
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/accounts/test_tokens.py -v`
Expected: FAIL - module missing.

- [ ] **Step 3: Implement**

```python
# gatekeep/accounts/tokens.py
from __future__ import annotations

import hashlib
import secrets


def new_token() -> str:
    """Generate a random URL-safe opaque token (for sessions and email links)."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """Return the sha256 hex digest of a raw token, for storage and lookup."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/accounts/test_tokens.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/accounts/tokens.py tests/accounts/test_tokens.py
git commit -m "feat(accounts): add opaque token generate/hash helpers"
```

### Task 5: Email layer (backends, factory, messages)

**Files:**
- Create: `gatekeep/email/__init__.py`, `gatekeep/email/backends.py`, `gatekeep/email/messages.py`
- Test: `tests/email/test_email.py`

**Interfaces:**
- Consumes: `Settings` fields added in Task 6 (write Task 6 first if executing strictly in order; the factory reads `email_backend`, `email_from`, SMTP fields, `public_base_url`).
- Produces: `EmailBackend` protocol; `ConsoleEmailBackend`, `SmtpEmailBackend`; `get_email_backend() -> EmailBackend`; `build_verification_email(base_url, token) -> tuple[str, str]`, `build_reset_email(base_url, token) -> tuple[str, str]`, `build_approval_email(base_url) -> tuple[str, str]` (each returns `(subject, body)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/email/test_email.py
import logging

from gatekeep.email.backends import ConsoleEmailBackend
from gatekeep.email.messages import (
    build_approval_email,
    build_reset_email,
    build_verification_email,
)


def test_console_backend_logs_message(caplog):
    with caplog.at_level(logging.INFO):
        ConsoleEmailBackend().send("u@x.com", "Subj", "Body here")
    assert "u@x.com" in caplog.text and "Body here" in caplog.text


def test_message_builders_include_link_and_token():
    subj, body = build_verification_email("https://gk.example", "TOKEN123")
    assert "https://gk.example/verify-email?token=TOKEN123" in body and subj
    subj, body = build_reset_email("https://gk.example", "RTOK")
    assert "https://gk.example/reset-password?token=RTOK" in body and subj
    subj, body = build_approval_email("https://gk.example")
    assert "https://gk.example/login" in body and subj
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/email/test_email.py -v`
Expected: FAIL - modules missing.

- [ ] **Step 3: Implement**

```python
# gatekeep/email/backends.py
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger(__name__)


class EmailBackend(Protocol):
    """Sends a plaintext email. Implementations must not raise on normal sends."""

    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailBackend:
    """Dev/test backend: logs the message (including any link) instead of sending."""

    def send(self, to: str, subject: str, body: str) -> None:
        """Log the email at INFO so dev/test can read verification/reset links."""
        logger.info("[email] to=%s subject=%s\n%s", to, subject, body)


class SmtpEmailBackend:
    """Production backend sending via SMTP with stdlib smtplib (no third-party SDK)."""

    def __init__(self, *, host: str, port: int, user: str | None,
                 password: str | None, use_tls: bool, sender: str) -> None:
        """Store SMTP connection parameters and the From address."""
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._use_tls, self._sender = use_tls, sender

    def send(self, to: str, subject: str, body: str) -> None:
        """Send a plaintext email over SMTP, using STARTTLS/login when configured."""
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self._sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self._host, self._port) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._user:
                smtp.login(self._user, self._password or "")
            smtp.send_message(msg)
```

```python
# gatekeep/email/messages.py
from __future__ import annotations


def build_verification_email(base_url: str, token: str) -> tuple[str, str]:
    """Return (subject, body) for the email-verification link."""
    link = f"{base_url}/verify-email?token={token}"
    return ("Verify your Gatekeep email", f"Confirm your email to continue:\n{link}\n")


def build_reset_email(base_url: str, token: str) -> tuple[str, str]:
    """Return (subject, body) for the password-reset link."""
    link = f"{base_url}/reset-password?token={token}"
    return ("Reset your Gatekeep password", f"Reset your password here:\n{link}\n")


def build_approval_email(base_url: str) -> tuple[str, str]:
    """Return (subject, body) telling a user their account was approved."""
    link = f"{base_url}/login"
    return ("Your Gatekeep account is approved", f"You can now sign in:\n{link}\n")
```

```python
# gatekeep/email/__init__.py
from __future__ import annotations

from gatekeep.config import get_settings
from gatekeep.email.backends import ConsoleEmailBackend, EmailBackend, SmtpEmailBackend


def get_email_backend() -> EmailBackend:
    """Return the configured email backend (console for dev/test, smtp for prod)."""
    s = get_settings()
    if s.email_backend == "smtp":
        return SmtpEmailBackend(
            host=s.smtp_host, port=s.smtp_port, user=s.smtp_user,
            password=s.smtp_password, use_tls=s.smtp_use_tls, sender=s.email_from,
        )
    return ConsoleEmailBackend()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/email/test_email.py -v`
Expected: PASS. (Add `tests/email/__init__.py` if the suite requires package dirs.)

- [ ] **Step 5: Commit**

```bash
git add gatekeep/email tests/email
git commit -m "feat(email): pluggable console/smtp backends and message builders"
```

### Task 6: Config additions

**Files:**
- Modify: `gatekeep/config.py`
- Test: `tests/test_config.py` (extend)

**Interfaces:**
- Produces on `Settings`: `email_backend: Literal["console","smtp"]="console"`, `email_from: str="gatekeep@localhost"`, `smtp_host: str="localhost"`, `smtp_port: int=25`, `smtp_user: str|None=None`, `smtp_password: str|None=None`, `smtp_use_tls: bool=True`, `public_base_url: str="http://localhost:5173"`, `session_ttl_seconds: int=1209600`, `email_token_ttl_seconds: int=86400`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_config.py
def test_signup_settings_defaults(monkeypatch):
    from gatekeep.config import Settings
    s = Settings(database_url="x", redis_url="y", anthropic_api_key="z")
    assert s.email_backend == "console"
    assert s.session_ttl_seconds == 1209600
    assert s.public_base_url.startswith("http")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_signup_settings_defaults -v`
Expected: FAIL - fields missing.

- [ ] **Step 3: Implement**

Add to `Settings` in `gatekeep/config.py`:

```python
    # --- Self-serve signup / auth ---
    email_backend: Literal["console", "smtp"] = "console"
    email_from: str = "gatekeep@localhost"
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    # Base URL the SPA is served from, used to build verification/reset links.
    public_base_url: str = "http://localhost:5173"
    # Login session lifetime (14 days) and one-time email link lifetime (1 day).
    session_ttl_seconds: int = 1_209_600
    email_token_ttl_seconds: int = 86_400
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py::test_signup_settings_defaults -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/config.py tests/test_config.py
git commit -m "feat(config): add email + session settings"
```

---

## Phase 3 - Services

### Task 7: Server-side sessions

**Files:**
- Create: `gatekeep/accounts/sessions.py`
- Test: `tests/accounts/test_sessions.py`

**Interfaces:**
- Consumes: `tokens.new_token`/`hash_token`; `Session`, `Account` models; `get_settings().session_ttl_seconds`.
- Produces:
  - `async def create_session(session, account_id: int) -> str` (returns raw token)
  - `async def resolve_session_account(session, raw_token: str) -> Account | None`
  - `async def revoke_session(session, raw_token: str) -> None`
  - `async def revoke_account_sessions(session, account_id: int) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/accounts/test_sessions.py
import datetime as dt

import pytest
from sqlalchemy import select

from gatekeep.accounts.sessions import (
    create_session, resolve_session_account, revoke_account_sessions, revoke_session,
)
from gatekeep.accounts.tokens import hash_token
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import Session as SessionRow
from tests.helpers import create_account


@pytest.mark.asyncio
async def test_create_and_resolve_roundtrip():
    async with SessionLocal() as s:
        acct = await create_account(s)
        await s.commit()
        raw = await create_session(s, acct.id)
        resolved = await resolve_session_account(s, raw)
        assert resolved is not None and resolved.id == acct.id


@pytest.mark.asyncio
async def test_expired_session_resolves_to_none():
    async with SessionLocal() as s:
        acct = await create_account(s)
        await s.commit()
        raw = await create_session(s, acct.id)
        row = (await s.execute(select(SessionRow).where(
            SessionRow.token_hash == hash_token(raw)))).scalar_one()
        row.expires_at = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        await s.commit()
        assert await resolve_session_account(s, raw) is None


@pytest.mark.asyncio
async def test_revoke_session_and_revoke_all():
    async with SessionLocal() as s:
        acct = await create_account(s)
        await s.commit()
        raw = await create_session(s, acct.id)
        await revoke_session(s, raw)
        assert await resolve_session_account(s, raw) is None
        r2 = await create_session(s, acct.id)
        await revoke_account_sessions(s, acct.id)
        assert await resolve_session_account(s, r2) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/accounts/test_sessions.py -v`
Expected: FAIL - module missing.

- [ ] **Step 3: Implement**

```python
# gatekeep/accounts/sessions.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounts.tokens import hash_token, new_token
from gatekeep.config import get_settings
from gatekeep.storage.models import Account, Session


async def create_session(session: AsyncSession, account_id: int) -> str:
    """Create a session row for an account and return its raw (unhashed) token."""
    raw = new_token()
    ttl = get_settings().session_ttl_seconds
    session.add(Session(
        token_hash=hash_token(raw),
        account_id=account_id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
    ))
    await session.commit()
    return raw


async def resolve_session_account(session: AsyncSession, raw_token: str) -> Account | None:
    """Resolve a raw session token to its Account, or None if missing/expired.

    Refreshes `last_seen_at` on a valid hit.
    """
    row = (await session.execute(
        select(Session).where(Session.token_hash == hash_token(raw_token))
    )).scalar_one_or_none()
    if row is None or row.expires_at <= datetime.now(timezone.utc):
        return None
    row.last_seen_at = datetime.now(timezone.utc)
    await session.commit()
    return await session.get(Account, row.account_id)


async def revoke_session(session: AsyncSession, raw_token: str) -> None:
    """Delete the session row for a raw token, if present (logout)."""
    await session.execute(delete(Session).where(Session.token_hash == hash_token(raw_token)))
    await session.commit()


async def revoke_account_sessions(session: AsyncSession, account_id: int) -> None:
    """Delete all sessions for an account (used on password reset)."""
    await session.execute(delete(Session).where(Session.account_id == account_id))
    await session.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/accounts/test_sessions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/accounts/sessions.py tests/accounts/test_sessions.py
git commit -m "feat(accounts): server-side session create/resolve/revoke"
```

### Task 8: create_account status param + signup & verify-email

**Files:**
- Modify: `gatekeep/accounts/account_service.py` (add `status` param to `create_account`)
- Create: `gatekeep/accounts/auth_service.py`
- Test: `tests/accounts/test_auth_service_signup.py`

**Interfaces:**
- Consumes: `account_service.create_account`, `passwords.hash_password`, `tokens.new_token`/`hash_token`, `AccountCredential`, `EmailToken`, `get_settings().email_token_ttl_seconds`.
- Produces (in `auth_service.py`):
  - exceptions `EmailConflictError`, `InvalidTokenError`, `InvalidCredentialsError`, `EmailNotVerifiedError`, `AccountNotActiveError` (all subclass `account_service.AccountServiceError`)
  - `async def signup(session, *, email: str, password: str) -> tuple[Account, str]` -> `(account, raw_verify_token)`; raises `EmailConflictError`.
  - `async def verify_email(session, *, raw_token: str) -> Account`; raises `InvalidTokenError`.
  - helper `_consume_token(session, raw_token, purpose) -> EmailToken` (single-use, not expired).

- [ ] **Step 1: Write the failing test**

```python
# tests/accounts/test_auth_service_signup.py
import pytest
from sqlalchemy import select

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_service import EmailConflictError, InvalidTokenError
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import Account, AccountCredential


@pytest.mark.asyncio
async def test_signup_creates_pending_account_and_token():
    async with SessionLocal() as s:
        account, raw = await auth_service.signup(s, email="A@B.com", password="pw123456")
        assert account.status == "pending"
        cred = (await s.execute(select(AccountCredential).where(
            AccountCredential.account_id == account.id))).scalar_one()
        assert cred.email == "a@b.com" and cred.email_verified is False
        assert raw  # verification token returned for the caller to email


@pytest.mark.asyncio
async def test_signup_duplicate_email_raises():
    async with SessionLocal() as s:
        await auth_service.signup(s, email="dup@x.com", password="pw123456")
        with pytest.raises(EmailConflictError):
            await auth_service.signup(s, email="dup@x.com", password="pw123456")


@pytest.mark.asyncio
async def test_verify_email_marks_verified_and_is_single_use():
    async with SessionLocal() as s:
        account, raw = await auth_service.signup(s, email="v@x.com", password="pw123456")
        verified = await auth_service.verify_email(s, raw_token=raw)
        assert verified.id == account.id
        cred = (await s.execute(select(AccountCredential).where(
            AccountCredential.account_id == account.id))).scalar_one()
        assert cred.email_verified is True
        with pytest.raises(InvalidTokenError):
            await auth_service.verify_email(s, raw_token=raw)  # already used
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/accounts/test_auth_service_signup.py -v`
Expected: FAIL - module/functions missing.

- [ ] **Step 3: Implement**

First extend `account_service.create_account` with a `status` param:

```python
async def create_account(
    session, *, name, monthly_budget_usd=None, is_operator=False, status="approved"
) -> Account:
    ...
    account = Account(
        name=name, monthly_budget_usd=monthly_budget_usd,
        is_operator=is_operator, status=status,
    )
    ...
```

(Update its docstring to document `status`. Existing callers pass no `status`, so they keep getting `"approved"`.)

Then `gatekeep/accounts/auth_service.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounts import account_service
from gatekeep.accounts.account_service import AccountServiceError
from gatekeep.accounts.passwords import hash_password
from gatekeep.accounts.tokens import hash_token, new_token
from gatekeep.config import get_settings
from gatekeep.storage.models import Account, AccountCredential, EmailToken


class EmailConflictError(AccountServiceError):
    """Raised when an email is already registered."""


class InvalidTokenError(AccountServiceError):
    """Raised when an email token is unknown, expired, or already used."""


def _normalize_email(email: str) -> str:
    """Lowercase and strip an email for consistent storage and lookup."""
    return email.strip().lower()


async def _issue_email_token(session: AsyncSession, account_id: int, purpose: str) -> str:
    """Create a single-use email token row and return its raw value."""
    raw = new_token()
    ttl = get_settings().email_token_ttl_seconds
    session.add(EmailToken(
        purpose=purpose, token_hash=hash_token(raw), account_id=account_id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
    ))
    await session.commit()
    return raw


async def _consume_token(session: AsyncSession, raw_token: str, purpose: str) -> EmailToken:
    """Load a valid, unused, unexpired token of `purpose`, mark it used, and return it.

    Raises:
        InvalidTokenError: if the token is unknown, wrong purpose, expired, or used.
    """
    row = (await session.execute(
        select(EmailToken).where(EmailToken.token_hash == hash_token(raw_token))
    )).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None or row.purpose != purpose or row.used_at is not None or row.expires_at <= now:
        raise InvalidTokenError("invalid or expired token")
    row.used_at = now
    return row


async def signup(session: AsyncSession, *, email: str, password: str) -> tuple[Account, str]:
    """Register a pending account with credentials and return (account, raw_verify_token).

    Raises:
        EmailConflictError: if the email is already registered.
    """
    normalized = _normalize_email(email)
    existing = (await session.execute(
        select(AccountCredential).where(AccountCredential.email == normalized)
    )).scalar_one_or_none()
    if existing is not None:
        raise EmailConflictError("email already registered")
    account = await account_service.create_account(session, name=normalized, status="pending")
    session.add(AccountCredential(
        account_id=account.id, email=normalized,
        password_hash=hash_password(password), email_verified=False,
    ))
    await session.commit()
    raw = await _issue_email_token(session, account.id, "verify_email")
    return account, raw


async def verify_email(session: AsyncSession, *, raw_token: str) -> Account:
    """Consume a verify_email token and mark the credential verified.

    Raises:
        InvalidTokenError: if the token is invalid/expired/used.
    """
    token = await _consume_token(session, raw_token, "verify_email")
    cred = (await session.execute(
        select(AccountCredential).where(AccountCredential.account_id == token.account_id)
    )).scalar_one()
    cred.email_verified = True
    await session.commit()
    return await session.get(Account, token.account_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/accounts/test_auth_service_signup.py tests/accounts/test_account_service.py -v`
Expected: PASS (existing account_service tests still green with the new default param).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/accounts/account_service.py gatekeep/accounts/auth_service.py tests/accounts/test_auth_service_signup.py
git commit -m "feat(accounts): signup + email verification service"
```

### Task 9: Login & logout

**Files:**
- Modify: `gatekeep/accounts/auth_service.py`
- Test: `tests/accounts/test_auth_service_login.py`

**Interfaces:**
- Consumes: `passwords.verify_password`, `sessions.create_session`/`revoke_session`.
- Produces:
  - `async def login(session, *, email: str, password: str) -> tuple[Account, str]` -> `(account, raw_session_token)`; raises `InvalidCredentialsError`, `EmailNotVerifiedError`, `AccountNotActiveError`.
  - `async def logout(session, *, raw_session_token: str) -> None`.
  - exceptions `InvalidCredentialsError`, `EmailNotVerifiedError`, `AccountNotActiveError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/accounts/test_auth_service_login.py
import pytest

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_service import (
    AccountNotActiveError, EmailNotVerifiedError, InvalidCredentialsError,
)
from gatekeep.accounts.sessions import resolve_session_account
from gatekeep.storage.db import SessionLocal


async def _signup_verified(s, email="u@x.com", pw="pw123456", status="approved"):
    account, raw = await auth_service.signup(s, email=email, password=pw)
    await auth_service.verify_email(s, raw_token=raw)
    account.status = status
    await s.commit()
    return account


@pytest.mark.asyncio
async def test_login_success_returns_session():
    async with SessionLocal() as s:
        acct = await _signup_verified(s)
        got, raw = await auth_service.login(s, email="u@x.com", password="pw123456")
        assert got.id == acct.id
        assert (await resolve_session_account(s, raw)).id == acct.id


@pytest.mark.asyncio
async def test_login_wrong_password_raises_invalid_credentials():
    async with SessionLocal() as s:
        await _signup_verified(s, email="w@x.com")
        with pytest.raises(InvalidCredentialsError):
            await auth_service.login(s, email="w@x.com", password="nope")


@pytest.mark.asyncio
async def test_login_unverified_and_disabled_are_refused():
    async with SessionLocal() as s:
        account, _ = await auth_service.signup(s, email="unv@x.com", password="pw123456")
        with pytest.raises(EmailNotVerifiedError):
            await auth_service.login(s, email="unv@x.com", password="pw123456")
        dis = await _signup_verified(s, email="dis@x.com", status="disabled")
        with pytest.raises(AccountNotActiveError):
            await auth_service.login(s, email="dis@x.com", password="pw123456")


@pytest.mark.asyncio
async def test_logout_revokes_session():
    async with SessionLocal() as s:
        await _signup_verified(s, email="lo@x.com")
        _, raw = await auth_service.login(s, email="lo@x.com", password="pw123456")
        await auth_service.logout(s, raw_session_token=raw)
        assert await resolve_session_account(s, raw) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/accounts/test_auth_service_login.py -v`
Expected: FAIL - functions/exceptions missing.

- [ ] **Step 3: Implement**

Add to `auth_service.py` (new imports: `from gatekeep.accounts.passwords import verify_password`, `from gatekeep.accounts import sessions`):

```python
class InvalidCredentialsError(AccountServiceError):
    """Raised when email/password authentication fails."""


class EmailNotVerifiedError(AccountServiceError):
    """Raised when a user logs in before verifying their email."""


class AccountNotActiveError(AccountServiceError):
    """Raised when a rejected or disabled account attempts to log in."""


async def login(session: AsyncSession, *, email: str, password: str) -> tuple[Account, str]:
    """Authenticate email/password and return (account, raw_session_token).

    Raises:
        InvalidCredentialsError: unknown email or wrong password.
        EmailNotVerifiedError: credential exists but email is unverified.
        AccountNotActiveError: account status is rejected or disabled.
    """
    normalized = _normalize_email(email)
    cred = (await session.execute(
        select(AccountCredential).where(AccountCredential.email == normalized)
    )).scalar_one_or_none()
    if cred is None or not verify_password(password, cred.password_hash):
        raise InvalidCredentialsError("invalid email or password")
    if not cred.email_verified:
        raise EmailNotVerifiedError("email not verified")
    account = await session.get(Account, cred.account_id)
    if account.status in ("rejected", "disabled"):
        raise AccountNotActiveError("account is not active")
    raw = await sessions.create_session(session, account.id)
    return account, raw


async def logout(session: AsyncSession, *, raw_session_token: str) -> None:
    """Revoke the given session token (idempotent)."""
    await sessions.revoke_session(session, raw_session_token)
```

Note: a `pending` account logs in successfully (returns a session) - the frontend routes it to the pending page, and `require_approved` blocks privileged routes. Only `rejected`/`disabled` are refused here.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/accounts/test_auth_service_login.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/accounts/auth_service.py tests/accounts/test_auth_service_login.py
git commit -m "feat(accounts): email/password login and logout"
```

### Task 10: Password reset

**Files:**
- Modify: `gatekeep/accounts/auth_service.py`
- Test: `tests/accounts/test_auth_service_reset.py`

**Interfaces:**
- Consumes: `_issue_email_token`, `_consume_token`, `sessions.revoke_account_sessions`, `passwords.hash_password`.
- Produces:
  - `async def request_password_reset(session, *, email: str) -> str | None` -> raw reset token, or None if no such credential.
  - `async def reset_password(session, *, raw_token: str, new_password: str) -> Account`; raises `InvalidTokenError`; revokes all sessions.

- [ ] **Step 1: Write the failing test**

```python
# tests/accounts/test_auth_service_reset.py
import pytest

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_service import InvalidTokenError
from gatekeep.accounts.passwords import verify_password
from gatekeep.accounts.sessions import resolve_session_account
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import AccountCredential
from sqlalchemy import select


async def _verified(s, email="r@x.com", pw="pw123456"):
    account, raw = await auth_service.signup(s, email=email, password=pw)
    await auth_service.verify_email(s, raw_token=raw)
    account.status = "approved"
    await s.commit()
    return account


@pytest.mark.asyncio
async def test_reset_request_unknown_email_returns_none():
    async with SessionLocal() as s:
        assert await auth_service.request_password_reset(s, email="nobody@x.com") is None


@pytest.mark.asyncio
async def test_reset_sets_password_and_revokes_sessions():
    async with SessionLocal() as s:
        acct = await _verified(s)
        _, sess = await auth_service.login(s, email="r@x.com", password="pw123456")
        token = await auth_service.request_password_reset(s, email="r@x.com")
        assert token
        await auth_service.reset_password(s, raw_token=token, new_password="newpw789")
        cred = (await s.execute(select(AccountCredential).where(
            AccountCredential.account_id == acct.id))).scalar_one()
        assert verify_password("newpw789", cred.password_hash)
        assert await resolve_session_account(s, sess) is None  # old sessions revoked


@pytest.mark.asyncio
async def test_reset_with_bad_token_raises():
    async with SessionLocal() as s:
        with pytest.raises(InvalidTokenError):
            await auth_service.reset_password(s, raw_token="bad", new_password="x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/accounts/test_auth_service_reset.py -v`
Expected: FAIL - functions missing.

- [ ] **Step 3: Implement**

Add to `auth_service.py`:

```python
async def request_password_reset(session: AsyncSession, *, email: str) -> str | None:
    """Issue a reset token for an email, or None if no credential exists.

    Returns None (rather than raising) so the route can respond identically
    whether or not the email is registered (no enumeration).
    """
    normalized = _normalize_email(email)
    cred = (await session.execute(
        select(AccountCredential).where(AccountCredential.email == normalized)
    )).scalar_one_or_none()
    if cred is None:
        return None
    return await _issue_email_token(session, cred.account_id, "reset_password")


async def reset_password(session: AsyncSession, *, raw_token: str, new_password: str) -> Account:
    """Consume a reset token, set the new password, and revoke all sessions.

    Raises:
        InvalidTokenError: if the token is invalid/expired/used.
    """
    token = await _consume_token(session, raw_token, "reset_password")
    cred = (await session.execute(
        select(AccountCredential).where(AccountCredential.account_id == token.account_id)
    )).scalar_one()
    cred.password_hash = hash_password(new_password)
    await session.commit()
    await sessions.revoke_account_sessions(session, token.account_id)
    return await session.get(Account, token.account_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/accounts/test_auth_service_reset.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/accounts/auth_service.py tests/accounts/test_auth_service_reset.py
git commit -m "feat(accounts): password reset with session revocation"
```

### Task 11: Operator approve / reject / list-pending

**Files:**
- Modify: `gatekeep/accounts/auth_service.py`
- Test: `tests/accounts/test_auth_service_approval.py`

**Interfaces:**
- Consumes: `account_service.get_account`, `_validate_budget` behavior (reuse `account_service.set_budget` or set directly), `AccountCredential` (for the approved user's email).
- Produces:
  - `async def list_pending_accounts(session) -> list[Account]`
  - `async def approve_account(session, *, account_id: int, monthly_budget_usd: float | None) -> tuple[Account, str]` -> `(account, email)`; sets status `approved` + budget; raises `AccountNotFoundError`.
  - `async def reject_account(session, *, account_id: int) -> Account`; sets status `rejected`.

- [ ] **Step 1: Write the failing test**

```python
# tests/accounts/test_auth_service_approval.py
import pytest

from gatekeep.accounts import auth_service
from gatekeep.storage.db import SessionLocal


async def _pending(s, email):
    account, _ = await auth_service.signup(s, email=email, password="pw123456")
    return account


@pytest.mark.asyncio
async def test_list_pending_only_returns_pending():
    async with SessionLocal() as s:
        await _pending(s, "p1@x.com")
        a2 = await _pending(s, "p2@x.com")
        await auth_service.approve_account(s, account_id=a2.id, monthly_budget_usd=10.0)
        pending = await auth_service.list_pending_accounts(s)
        emails = {a.name for a in pending}
        assert "p1@x.com" in emails and "p2@x.com" not in emails


@pytest.mark.asyncio
async def test_approve_sets_status_budget_and_returns_email():
    async with SessionLocal() as s:
        a = await _pending(s, "ap@x.com")
        account, email = await auth_service.approve_account(
            s, account_id=a.id, monthly_budget_usd=25.0)
        assert account.status == "approved"
        assert account.monthly_budget_usd == 25.0
        assert email == "ap@x.com"


@pytest.mark.asyncio
async def test_reject_sets_status_rejected():
    async with SessionLocal() as s:
        a = await _pending(s, "rj@x.com")
        account = await auth_service.reject_account(s, account_id=a.id)
        assert account.status == "rejected"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/accounts/test_auth_service_approval.py -v`
Expected: FAIL - functions missing.

- [ ] **Step 3: Implement**

Add to `auth_service.py` (import `from sqlalchemy import select` already present):

```python
async def list_pending_accounts(session: AsyncSession) -> list[Account]:
    """Return all accounts awaiting operator approval, oldest first."""
    return list((await session.execute(
        select(Account).where(Account.status == "pending").order_by(Account.created_at)
    )).scalars())


async def approve_account(
    session: AsyncSession, *, account_id: int, monthly_budget_usd: float | None
) -> tuple[Account, str]:
    """Approve a pending account, set its budget, and return (account, email).

    Raises:
        AccountNotFoundError: if no account has that id.
    """
    account = await account_service.get_account(session, account_id)
    account.status = "approved"
    account.monthly_budget_usd = monthly_budget_usd
    await session.commit()
    cred = (await session.execute(
        select(AccountCredential).where(AccountCredential.account_id == account_id)
    )).scalar_one()
    return account, cred.email


async def reject_account(session: AsyncSession, *, account_id: int) -> Account:
    """Mark an account rejected.

    Raises:
        AccountNotFoundError: if no account has that id.
    """
    account = await account_service.get_account(session, account_id)
    account.status = "rejected"
    await session.commit()
    return account
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/accounts/test_auth_service_approval.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/accounts/auth_service.py tests/accounts/test_auth_service_approval.py
git commit -m "feat(accounts): operator approve/reject/list-pending"
```

---

## Phase 4 - API

### Task 12: Auth router (signup/verify/login/logout/reset) + cookies + CSRF

**Files:**
- Create: `gatekeep/api/auth.py`
- Modify: `gatekeep/app.py` (include the router)
- Test: `tests/api/test_auth_routes.py`

**Interfaces:**
- Consumes: all `auth_service` functions; `get_email_backend`; `email.messages` builders; `get_settings().public_base_url`; `get_session`.
- Produces:
  - `auth_router = APIRouter(prefix="/dashboard/api/auth", tags=["auth"])`
  - Constants `SESSION_COOKIE = "gk_session"`, `CSRF_COOKIE = "gk_csrf"`, `CSRF_HEADER = "x-csrf-token"`.
  - `def require_csrf(request: Request) -> None` dependency (imported by dashboard.py in Task 13): if a `gk_session` cookie is present, require `X-CSRF-Token` header equal to the `gk_csrf` cookie; otherwise pass (API-key callers exempt).
  - Routes: `POST /signup`, `POST /verify-email`, `POST /login`, `POST /logout`, `POST /password/reset-request`, `POST /password/reset`.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_auth_routes.py
import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport

from gatekeep.app import app

BASE = "/dashboard/api/auth"


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_signup_then_login_flow_sets_session_cookie(client, caplog):
    import logging
    with caplog.at_level(logging.INFO):
        r = await client.post(f"{BASE}/signup", json={"email": "e@x.com", "password": "pw123456"})
    assert r.status_code == 202
    # console email backend logged the verification link; extract the token
    token = caplog.text.split("token=")[1].split()[0].strip()
    r = await client.post(f"{BASE}/verify-email", json={"token": token})
    assert r.status_code == 200
    r = await client.post(f"{BASE}/login", json={"email": "e@x.com", "password": "pw123456"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert "gk_session" in r.cookies


@pytest.mark.asyncio
async def test_signup_duplicate_still_returns_202(client):
    await client.post(f"{BASE}/signup", json={"email": "d@x.com", "password": "pw123456"})
    r = await client.post(f"{BASE}/signup", json={"email": "d@x.com", "password": "pw123456"})
    assert r.status_code == 202  # no enumeration


@pytest.mark.asyncio
async def test_login_bad_password_returns_401(client):
    r = await client.post(f"{BASE}/login", json={"email": "no@x.com", "password": "bad"})
    assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_auth_routes.py -v`
Expected: FAIL - router not mounted.

- [ ] **Step 3: Implement the router**

```python
# gatekeep/api/auth.py
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_service import (
    AccountNotActiveError, EmailConflictError, EmailNotVerifiedError,
    InvalidCredentialsError, InvalidTokenError,
)
from gatekeep.config import get_settings
from gatekeep.email import get_email_backend
from gatekeep.email.messages import (
    build_approval_email, build_reset_email, build_verification_email,
)
from gatekeep.storage.db import get_session

auth_router = APIRouter(prefix="/dashboard/api/auth", tags=["auth"])

SESSION_COOKIE = "gk_session"
CSRF_COOKIE = "gk_csrf"
CSRF_HEADER = "x-csrf-token"


def require_csrf(request: Request) -> None:
    """Enforce double-submit CSRF for cookie-authenticated mutations.

    If a session cookie is present, the X-CSRF-Token header must equal the
    gk_csrf cookie. API-key callers (no session cookie) are exempt.

    Raises:
        HTTPException: 403 when the session cookie is present but the CSRF
            token is missing or mismatched.
    """
    if request.cookies.get(SESSION_COOKIE) is None:
        return
    header = request.headers.get(CSRF_HEADER)
    cookie = request.cookies.get(CSRF_COOKIE)
    if not header or not cookie or not secrets.compare_digest(header, cookie):
        raise HTTPException(status_code=403, detail={"error": {
            "message": "CSRF check failed.", "type": "permission_error", "code": None}})


def _set_session_cookies(response: Response, session_token: str) -> str:
    """Set the session + CSRF cookies on a response and return the CSRF token."""
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(SESSION_COOKIE, session_token, httponly=True, secure=True,
                        samesite="lax", max_age=get_settings().session_ttl_seconds)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, secure=True,
                        samesite="lax", max_age=get_settings().session_ttl_seconds)
    return csrf


class SignupIn(BaseModel):
    email: EmailStr
    password: str


class TokenIn(BaseModel):
    token: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ResetRequestIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str
    new_password: str


@auth_router.post("/signup", status_code=202)
async def signup(body: SignupIn, session: AsyncSession = Depends(get_session)) -> dict:
    """Register a pending account and email a verification link. Always 202 (no enumeration)."""
    try:
        _, raw = await auth_service.signup(session, email=body.email, password=body.password)
    except EmailConflictError:
        return {"status": "ok"}
    subject, text = build_verification_email(get_settings().public_base_url, raw)
    get_email_backend().send(str(body.email), subject, text)
    return {"status": "ok"}


@auth_router.post("/verify-email")
async def verify_email(body: TokenIn, session: AsyncSession = Depends(get_session)) -> dict:
    """Verify an email via its token."""
    try:
        await auth_service.verify_email(session, raw_token=body.token)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=400, detail={"error": {
            "message": str(exc), "type": "invalid_request_error", "code": None}}) from exc
    return {"status": "verified"}


@auth_router.post("/login")
async def login(body: LoginIn, response: Response,
                session: AsyncSession = Depends(get_session)) -> dict:
    """Authenticate and set session + CSRF cookies. Returns the account status."""
    try:
        account, token = await auth_service.login(
            session, email=body.email, password=body.password)
    except (InvalidCredentialsError, EmailNotVerifiedError, AccountNotActiveError) as exc:
        raise HTTPException(status_code=401, detail={"error": {
            "message": "Invalid email or password.",
            "type": "authentication_error", "code": None}}) from exc
    csrf = _set_session_cookies(response, token)
    return {"account_id": account.id, "status": account.status,
            "is_operator": account.is_operator, "csrf_token": csrf}


@auth_router.post("/logout")
async def logout(request: Request, response: Response,
                 session: AsyncSession = Depends(get_session)) -> dict:
    """Revoke the current session and clear cookies."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await auth_service.logout(session, raw_session_token=token)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"status": "ok"}


@auth_router.post("/password/reset-request", status_code=202)
async def reset_request(body: ResetRequestIn,
                        session: AsyncSession = Depends(get_session)) -> dict:
    """Email a reset link if the address exists. Always 202 (no enumeration)."""
    raw = await auth_service.request_password_reset(session, email=body.email)
    if raw:
        subject, text = build_reset_email(get_settings().public_base_url, raw)
        get_email_backend().send(str(body.email), subject, text)
    return {"status": "ok"}


@auth_router.post("/password/reset")
async def reset(body: ResetIn, session: AsyncSession = Depends(get_session)) -> dict:
    """Set a new password from a reset token, revoking existing sessions."""
    try:
        await auth_service.reset_password(
            session, raw_token=body.token, new_password=body.new_password)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=400, detail={"error": {
            "message": str(exc), "type": "invalid_request_error", "code": None}}) from exc
    return {"status": "ok"}
```

Wire into `gatekeep/app.py` next to `app.include_router(dashboard_router)`:

```python
from gatekeep.api.auth import auth_router
app.include_router(auth_router)
```

Note: `EmailStr` requires `pydantic[email]` (the `email-validator` package). Add `pydantic[email]>=2.7` to dependencies in this task and `pip install -e .`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/api/test_auth_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/auth.py gatekeep/app.py pyproject.toml tests/api/test_auth_routes.py
git commit -m "feat(api): auth router with signup/login/reset + CSRF"
```

### Task 13: Session-or-key identity resolution + require_approved

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/api/test_dashboard_session_auth.py`

**Interfaces:**
- Consumes: `sessions.resolve_session_account`, `auth.SESSION_COOKIE`, `require_api_key`.
- Produces:
  - `_require_caller_account` resolves session cookie first, then API key (401 if neither).
  - `require_approved(caller_account=Depends(_require_caller_account)) -> Account` (403 unless `status == "approved"`).
  - `mint_account_key` gains `Depends(require_approved)` and the CSRF dependency; a pending session is blocked from minting.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_dashboard_session_auth.py
import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport

from gatekeep.accounts import auth_service
from gatekeep.app import app
from gatekeep.storage.db import SessionLocal


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _approved_login(client):
    async with SessionLocal() as s:
        account, raw = await auth_service.signup(s, email="s@x.com", password="pw123456")
        await auth_service.verify_email(s, raw_token=raw)
        account.status = "approved"
        await s.commit()
        acct_id = account.id
    r = await client.post("/dashboard/api/auth/login",
                          json={"email": "s@x.com", "password": "pw123456"})
    return acct_id, r.json()["csrf_token"]


@pytest.mark.asyncio
async def test_me_works_with_session_cookie(client):
    acct_id, _ = await _approved_login(client)
    r = await client.get("/dashboard/api/me")
    assert r.status_code == 200 and r.json()["account_id"] == acct_id


@pytest.mark.asyncio
async def test_pending_session_blocked_from_minting_key(client):
    async with SessionLocal() as s:
        account, raw = await auth_service.signup(s, email="pend@x.com", password="pw123456")
        await auth_service.verify_email(s, raw_token=raw)
        await s.commit()
        acct_id = account.id
    login = await client.post("/dashboard/api/auth/login",
                              json={"email": "pend@x.com", "password": "pw123456"})
    csrf = login.json()["csrf_token"]
    r = await client.post(f"/dashboard/api/accounts/{acct_id}/keys",
                          json={"name": "k1"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403  # pending -> require_approved blocks


@pytest.mark.asyncio
async def test_approved_session_can_mint_own_key(client):
    acct_id, csrf = await _approved_login(client)
    r = await client.post(f"/dashboard/api/accounts/{acct_id}/keys",
                          json={"name": "k1"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["key"].startswith("gk-")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_dashboard_session_auth.py -v`
Expected: FAIL - session auth + require_approved not implemented.

- [ ] **Step 3: Implement**

Rewrite `_require_caller_account` in `gatekeep/api/dashboard.py` to try the session cookie first, then fall back to the existing API-key path. Add imports at top: `from fastapi import Request`, `from gatekeep.accounts.sessions import resolve_session_account`, `from gatekeep.api.auth import SESSION_COOKIE, require_csrf`, `from gatekeep.middleware.auth import require_api_key`.

```python
async def _require_caller_account(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Account:
    """Resolve the caller's Account from a session cookie, else an API key.

    Session cookie (human dashboard login) is tried first; if absent or
    invalid, falls back to API-key auth (operators, CLI, programmatic
    callers). Raises 401 if neither credential resolves.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        account = await resolve_session_account(session, token)
        if account is not None:
            return account
    caller = await require_api_key(
        authorization=request.headers.get("authorization"),
        x_api_key=request.headers.get("x-api-key"),
        session=session,
    )
    account = await session.get(Account, caller.account_id)
    if account is None:
        raise HTTPException(status_code=401, detail=_error_body(
            "API key's account no longer exists.", "authentication_error"))
    return account


async def require_approved(
    caller_account: Account = Depends(_require_caller_account),
) -> Account:
    """Require the caller's account to be approved (blocks pending/rejected/disabled).

    Raises:
        HTTPException: 403 if the account status is not "approved".
    """
    if caller_account.status != "approved":
        raise _forbidden("Your account is not approved yet.")
    return caller_account
```

Note: calling `require_api_key` directly means the `_enforce_pre_auth_rate_limit` sub-dependency does not auto-run; the pre-auth limiter still guards the gateway routes and the auth router where it matters. If you want it on management routes too, pass `_pre_auth=None` is not enough - instead leave as-is (management routes were already only key-or-session and are not the abuse target).

Then guard `mint_account_key` (and `revoke_account_key`) with approval + CSRF. Change their signatures so `caller_account` comes from `require_approved` and add the CSRF dep:

```python
@router.post("/accounts/{account_id}/keys", response_model=KeyCreatedResponse)
async def mint_account_key(
    account_id: int,
    body: KeyCreateRequest,
    session: AsyncSession = Depends(get_session),
    caller_account: Account = Depends(require_approved),
    _csrf: None = Depends(require_csrf),
):
    _authorize_account_access(caller_account, account_id)
    ...  # existing body unchanged
```

Apply the same `require_approved` + `require_csrf` to `revoke_account_key`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/api/test_dashboard_session_auth.py tests/api/test_dashboard.py -v`
Expected: PASS (existing key-auth dashboard tests still green - key callers have no session cookie, so CSRF is exempt and approval holds because CLI/script accounts are `approved`).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/dashboard.py tests/api/test_dashboard_session_auth.py
git commit -m "feat(api): resolve identity from session or key + require_approved gate"
```

### Task 14: Operator approval routes + status in responses

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/api/test_approval_routes.py`

**Interfaces:**
- Consumes: `auth_service.list_pending_accounts`/`approve_account`/`reject_account`; `get_email_backend`; `build_approval_email`; `require_operator`; `require_csrf`.
- Produces:
  - `GET /accounts/pending` -> list of `{account_id, name, email, created_at}` (operator only).
  - `POST /accounts/{account_id}/approve` body `{monthly_budget_usd: float | None}` (operator only, CSRF) -> emails approval, returns `AccountOut`.
  - `POST /accounts/{account_id}/reject` (operator only, CSRF) -> returns `AccountOut`.
  - `status` field added to `MeResponse` and `AccountOut`.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_approval_routes.py
import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_keys import generate_key, hash_key
from gatekeep.app import app
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import ApiKey
from tests.helpers import create_account


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _operator_key():
    async with SessionLocal() as s:
        raw = generate_key()
        op = await create_account(s, is_operator=True)
        s.add(ApiKey(name="opkey", key_hash=hash_key(raw), account_id=op.id))
        await s.commit()
    return raw


@pytest.mark.asyncio
async def test_operator_lists_and_approves_pending(client):
    async with SessionLocal() as s:
        pending, tok = await auth_service.signup(s, email="new@x.com", password="pw123456")
        await auth_service.verify_email(s, raw_token=tok)
        await s.commit()
        pid = pending.id
    op = await _operator_key()
    h = {"Authorization": f"Bearer {op}"}
    r = await client.get("/dashboard/api/accounts/pending", headers=h)
    assert r.status_code == 200 and any(a["account_id"] == pid for a in r.json()["accounts"])
    r = await client.post(f"/dashboard/api/accounts/{pid}/approve",
                          json={"monthly_budget_usd": 15.0}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_non_operator_cannot_list_pending(client):
    async with SessionLocal() as s:
        raw = generate_key()
        acct = await create_account(s)  # non-operator, approved
        s.add(ApiKey(name="k", key_hash=hash_key(raw), account_id=acct.id))
        await s.commit()
    r = await client.get("/dashboard/api/accounts/pending",
                         headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_approval_routes.py -v`
Expected: FAIL - routes missing.

- [ ] **Step 3: Implement**

Add response/request models and routes in `gatekeep/api/dashboard.py` (near the other `/accounts` routes). Add `status: str` to the existing `MeResponse` and `AccountOut` models and populate it wherever they are constructed.

```python
class PendingAccountOut(BaseModel):
    account_id: int
    name: str
    email: str
    created_at: datetime


class PendingListResponse(BaseModel):
    accounts: list[PendingAccountOut]


class ApproveRequest(BaseModel):
    monthly_budget_usd: float | None = None


@router.get("/accounts/pending", response_model=PendingListResponse)
async def list_pending(
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
):
    """List accounts awaiting approval (operator only)."""
    from sqlalchemy import select
    from gatekeep.storage.models import AccountCredential

    accounts = await auth_service.list_pending_accounts(session)
    out = []
    for a in accounts:
        cred = (await session.execute(select(AccountCredential).where(
            AccountCredential.account_id == a.id))).scalar_one_or_none()
        out.append(PendingAccountOut(account_id=a.id, name=a.name,
                                     email=cred.email if cred else "", created_at=a.created_at))
    return PendingListResponse(accounts=out)


@router.post("/accounts/{account_id}/approve", response_model=AccountOut)
async def approve(
    account_id: int,
    body: ApproveRequest,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
    _csrf: None = Depends(require_csrf),
):
    """Approve a pending account, set its budget, and email the user (operator only)."""
    try:
        account, email = await auth_service.approve_account(
            session, account_id=account_id, monthly_budget_usd=body.monthly_budget_usd)
    except account_service.AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    subject, text = build_approval_email(get_settings().public_base_url)
    get_email_backend().send(email, subject, text)
    return AccountOut(id=account.id, name=account.name,
                      monthly_budget_usd=account.monthly_budget_usd,
                      is_operator=account.is_operator, status=account.status)


@router.post("/accounts/{account_id}/reject", response_model=AccountOut)
async def reject(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
    _csrf: None = Depends(require_csrf),
):
    """Reject a pending account (operator only)."""
    try:
        account = await auth_service.reject_account(session, account_id=account_id)
    except account_service.AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    return AccountOut(id=account.id, name=account.name,
                      monthly_budget_usd=account.monthly_budget_usd,
                      is_operator=account.is_operator, status=account.status)
```

Add imports at the top of the module: `from gatekeep.accounts import auth_service`, `from gatekeep.email import get_email_backend`, `from gatekeep.email.messages import build_approval_email`. Confirm the exact field list of `AccountOut`/`MeResponse` and add `status` consistently (also update any place they are built for the existing `/accounts` and `/me` routes).

Note on route ordering: register `GET /accounts/pending` before any `GET /accounts/{account_id}` numeric-path route, or FastAPI may match `pending` as an `account_id`. Place it directly under the existing `GET /accounts` collection route.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/api/test_approval_routes.py tests/api/test_dashboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/dashboard.py tests/api/test_approval_routes.py
git commit -m "feat(api): operator approve/reject/pending routes + status field"
```

---

## Phase 5 - Frontend

### Task 15: client.ts cookie/CSRF + auth API module

**Files:**
- Modify: `dashboard/src/api/client.ts`
- Create: `dashboard/src/api/auth.ts`
- Test: `dashboard/src/api/auth.test.ts`

**Interfaces:**
- Produces (in `auth.ts`): `signup(email, password)`, `verifyEmail(token)`, `login(email, password) -> {account_id, status, is_operator, csrf_token}`, `logout()`, `requestReset(email)`, `resetPassword(token, newPassword)`. All use `fetch(..., { credentials: "include" })`.
- Modifies `client.ts`: every request sends `credentials: "include"`, drops the `Authorization: Bearer` header, and attaches `X-CSRF-Token` (read from the `gk_csrf` cookie) on non-GET requests.

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/api/auth.test.ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { login } from "./auth";

afterEach(() => vi.unstubAllGlobals());

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

describe("auth api", () => {
  it("login posts credentials with cookies and returns status", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ account_id: 1, status: "pending", is_operator: false, csrf_token: "c" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const res = await login("e@x.com", "pw123456");
    expect(res.status).toBe("pending");
    const opts = fetchMock.mock.calls[0][1] as RequestInit;
    expect(opts.credentials).toBe("include");
    expect(opts.method).toBe("POST");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/api/auth.test.ts`
Expected: FAIL - `auth.ts` missing.

- [ ] **Step 3: Implement**

```ts
// dashboard/src/api/auth.ts
/** Auth API calls for signup, login, logout, and password reset. All requests
 *  carry cookies so the server-side session is established/read. */
const BASE = "/dashboard/api/auth";

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json())?.error?.message ?? "Request failed");
  return res.json() as Promise<T>;
}

export interface LoginResult {
  account_id: number;
  status: string;
  is_operator: boolean;
  csrf_token: string;
}

export const signup = (email: string, password: string) =>
  post<{ status: string }>("/signup", { email, password });
export const verifyEmail = (token: string) => post<{ status: string }>("/verify-email", { token });
export const login = (email: string, password: string) =>
  post<LoginResult>("/login", { email, password });
export const logout = () => post<{ status: string }>("/logout", {});
export const requestReset = (email: string) =>
  post<{ status: string }>("/password/reset-request", { email });
export const resetPassword = (token: string, newPassword: string) =>
  post<{ status: string }>("/password/reset", { token, new_password: newPassword });
```

In `client.ts`: add a `readCsrfCookie()` helper (`document.cookie` parse for `gk_csrf`), set `credentials: "include"` on every request, remove the `Authorization` header logic, and add `"X-CSRF-Token": readCsrfCookie()` to non-GET requests. Keep `UnauthorizedError`/`getMe` behavior (401 still means "not logged in").

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/api/auth.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/api/auth.ts dashboard/src/api/auth.test.ts dashboard/src/api/client.ts
git commit -m "feat(dashboard): cookie/CSRF client + auth API module"
```

### Task 16: Remove IdentityPicker/identityStore + session gating in App.tsx

**Files:**
- Delete: `dashboard/src/components/IdentityPicker.tsx`, `dashboard/src/api/identityStore.ts`, `dashboard/src/api/identityStore.test.ts`
- Modify: `dashboard/src/App.tsx`, `dashboard/src/api/client.test.ts` (drop identityStore imports/cases)
- Test: `dashboard/src/App.test.tsx`

**Interfaces:**
- Consumes: `getMe` (returns `{account_id, status, is_operator, ...}` or throws `UnauthorizedError`), auth pages (Task 17), `logout`.
- Produces: `App` renders Login/Signup when unauthenticated, `PendingApprovalPage` when `status === "pending"`, the dashboard when `status === "approved"`.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/App.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import App from "./App";

afterEach(() => vi.unstubAllGlobals());

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

it("shows login when unauthenticated (401 from /me)", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({}, 401)));
  render(<App />);
  await waitFor(() => expect(screen.getByText(/sign in/i)).toBeTruthy());
});

it("shows pending page when account status is pending", async () => {
  vi.stubGlobal("fetch", vi.fn(async () =>
    jsonResponse({ account_id: 1, status: "pending", is_operator: false })));
  render(<App />);
  await waitFor(() => expect(screen.getByText(/awaiting approval/i)).toBeTruthy());
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/App.test.tsx`
Expected: FAIL - App still gates on pasted identity; pages missing.

- [ ] **Step 3: Implement**

Delete the two source files and their tests. Rewrite `App.tsx` to:
- On mount, call `getMe()`. On `UnauthorizedError`, show `LoginPage`/`SignupPage` (a small local `authView` state toggles them, plus routes for verify/reset based on `window.location.pathname`).
- If `me.status === "pending"`, render `PendingApprovalPage`.
- If `me.status === "approved"`, render the existing `Header` + pages.
- Replace the old `handleUnauthorized`/`identityStore` wiring; a `logout()` call clears the session and returns to login.
- Remove `IdentityPicker` import and `identityStore` subscriptions.

Update `client.test.ts` to drop `identityStore` imports and any roster-specific cases (keep `getMe`/`validateKey`/prompt cases; if `validateKey` is no longer used anywhere after the picker is gone, remove it and its test).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run`
Expected: PASS (whole suite; fix any leftover references to deleted modules).

- [ ] **Step 5: Commit**

```bash
git add -A dashboard/src
git commit -m "feat(dashboard): retire paste-key picker, gate app on session"
```

### Task 17: Auth pages

**Files:**
- Create: `dashboard/src/pages/LoginPage.tsx`, `SignupPage.tsx`, `VerifyEmailPage.tsx`, `ForgotPasswordPage.tsx`, `ResetPasswordPage.tsx`, `PendingApprovalPage.tsx`
- Test: `dashboard/src/pages/LoginPage.test.tsx`, `dashboard/src/pages/SignupPage.test.tsx`

**Interfaces:**
- Consumes: `auth.ts` functions.
- Produces: form components. `LoginPage` takes an `onLoggedIn(result: LoginResult)` prop; `SignupPage` shows a "check your email" confirmation after submit; `VerifyEmailPage`/`ResetPasswordPage` read the `token` query param; `PendingApprovalPage` is static copy + a logout button.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/pages/LoginPage.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import LoginPage from "./LoginPage";

afterEach(() => vi.unstubAllGlobals());

it("submits credentials and calls onLoggedIn with the result", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => ({ account_id: 1, status: "approved", is_operator: false, csrf_token: "c" }),
  } as Response)));
  const onLoggedIn = vi.fn();
  render(<LoginPage onLoggedIn={onLoggedIn} />);
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "e@x.com" } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "pw123456" } });
  fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
  await waitFor(() => expect(onLoggedIn).toHaveBeenCalledWith(
    expect.objectContaining({ status: "approved" })));
});
```

```tsx
// dashboard/src/pages/SignupPage.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import SignupPage from "./SignupPage";

afterEach(() => vi.unstubAllGlobals());

it("shows a check-your-email message after signup", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 202, json: async () => ({ status: "ok" }) } as Response)));
  render(<SignupPage onBackToLogin={() => {}} />);
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "e@x.com" } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "pw123456" } });
  fireEvent.click(screen.getByRole("button", { name: /sign up/i }));
  await waitFor(() => expect(screen.getByText(/check your email/i)).toBeTruthy());
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/pages/LoginPage.test.tsx src/pages/SignupPage.test.tsx`
Expected: FAIL - pages missing.

- [ ] **Step 3: Implement**

Build the six pages following existing component style (Tailwind classes, controlled inputs, an error banner on thrown errors). Each form input has an associated `<label>` (the tests query by label text). Minimum behavior:
- `LoginPage`: email + password -> `login()` -> `onLoggedIn(result)`; link to Signup and Forgot Password.
- `SignupPage`: email + password -> `signup()` -> replace form with a "Check your email to verify your account." message; `onBackToLogin` link.
- `VerifyEmailPage`: on mount, read `?token=`, call `verifyEmail()`, show success/failure + a link to sign in.
- `ForgotPasswordPage`: email -> `requestReset()` -> always show "If that email exists, a reset link is on its way."
- `ResetPasswordPage`: read `?token=`, new password -> `resetPassword()` -> success + link to sign in.
- `PendingApprovalPage`: static "Your account is awaiting approval." copy + a Log out button (`logout()` then reload).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/pages`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages
git commit -m "feat(dashboard): login, signup, verify, reset, pending pages"
```

### Task 18: Operator Pending Requests panel

**Files:**
- Create: `dashboard/src/components/PendingRequestsPanel.tsx`
- Modify: `dashboard/src/pages/ManagementPage.tsx` (render the panel for operators), `dashboard/src/api/client.ts` (add `getPending`, `approveAccount`, `rejectAccount`)
- Test: `dashboard/src/components/PendingRequestsPanel.test.tsx`

**Interfaces:**
- Consumes: `getPending() -> {accounts: {account_id, name, email, created_at}[]}`, `approveAccount(id, budget)`, `rejectAccount(id)`.
- Produces: a table of pending accounts with a budget input + Approve and Reject buttons per row; refreshes on action.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/components/PendingRequestsPanel.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PendingRequestsPanel from "./PendingRequestsPanel";

afterEach(() => vi.unstubAllGlobals());

it("lists pending accounts and approves one", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, opts?: RequestInit) => {
    calls.push(`${opts?.method ?? "GET"} ${url}`);
    if (url.endsWith("/accounts/pending")) {
      return { ok: true, status: 200, json: async () => ({
        accounts: [{ account_id: 7, name: "new@x.com", email: "new@x.com",
                     created_at: "2026-08-26T00:00:00Z" }] }) } as Response;
    }
    return { ok: true, status: 200, json: async () => ({
      id: 7, name: "new@x.com", status: "approved" }) } as Response;
  }));
  render(<PendingRequestsPanel />);
  await waitFor(() => expect(screen.getByText("new@x.com")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: /approve/i }));
  await waitFor(() => expect(calls.some((c) => c.includes("/approve"))).toBe(true));
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/components/PendingRequestsPanel.test.tsx`
Expected: FAIL - component/api missing.

- [ ] **Step 3: Implement**

Add `getPending`, `approveAccount(id, monthlyBudgetUsd)`, `rejectAccount(id)` to `client.ts` (these POSTs include the CSRF header via the Task 15 change). Build `PendingRequestsPanel.tsx`: fetch on mount, render a row per pending account with an optional budget number input and Approve/Reject buttons, re-fetch after each action. In `ManagementPage.tsx`, render `<PendingRequestsPanel />` above the accounts table when `me.is_operator` is true.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/components/PendingRequestsPanel.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/components/PendingRequestsPanel.tsx dashboard/src/components/PendingRequestsPanel.test.tsx dashboard/src/pages/ManagementPage.tsx dashboard/src/api/client.ts
git commit -m "feat(dashboard): operator pending-requests approval panel"
```

---

## Phase 6 - End-to-end

### Task 19: Full signup -> approve -> key -> gateway E2E

**Files:**
- Test: `tests/e2e/test_signup_e2e.py`

**Interfaces:**
- Consumes: the auth router, approval routes, key routes, and a gateway completion endpoint (reuse the `counting_provider`/`FakeProvider` pattern from `tests/conftest.py` / `tests/api/test_dashboard.py`).

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_signup_e2e.py
import logging

import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport

import gatekeep.app as app_module
from gatekeep.accounts.auth_keys import generate_key, hash_key
from gatekeep.app import app
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import ApiKey
from tests.helpers import create_account, FakeProvider


@pytest_asyncio.fixture
async def client(monkeypatch):
    fake = FakeProvider(["pong"])
    monkeypatch.setitem(app_module._providers, "anthropic", fake)
    monkeypatch.setitem(app_module._providers, "ollama", fake)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _operator_key():
    async with SessionLocal() as s:
        raw = generate_key()
        op = await create_account(s, is_operator=True)
        s.add(ApiKey(name="op", key_hash=hash_key(raw), account_id=op.id))
        await s.commit()
    return raw


@pytest.mark.asyncio
async def test_full_signup_to_gateway(client, caplog):
    A = "/dashboard/api/auth"
    with caplog.at_level(logging.INFO):
        r = await client.post(f"{A}/signup", json={"email": "e2e@x.com", "password": "pw123456"})
    assert r.status_code == 202
    token = caplog.text.split("token=")[1].split()[0].strip()
    assert (await client.post(f"{A}/verify-email", json={"token": token})).status_code == 200

    # pending login
    r = await client.post(f"{A}/login", json={"email": "e2e@x.com", "password": "pw123456"})
    assert r.json()["status"] == "pending"
    acct_id = r.json()["account_id"]

    # operator approves
    op = await _operator_key()
    r = await client.post(f"/dashboard/api/accounts/{acct_id}/approve",
                          json={"monthly_budget_usd": 50.0},
                          headers={"Authorization": f"Bearer {op}"})
    assert r.status_code == 200 and r.json()["status"] == "approved"

    # user logs in again, mints a key
    login = await client.post(f"{A}/login", json={"email": "e2e@x.com", "password": "pw123456"})
    assert login.json()["status"] == "approved"
    csrf = login.json()["csrf_token"]
    r = await client.post(f"/dashboard/api/accounts/{acct_id}/keys",
                          json={"name": "primary"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    raw_key = r.json()["key"]

    # that key authenticates a real gateway request
    r = await client.post("/v1/chat/completions",
                          headers={"Authorization": f"Bearer {raw_key}"},
                          json={"model": "claude-sonnet-5",
                                "messages": [{"role": "user", "content": "ping"}]})
    assert r.status_code == 200
```

Confirm the gateway path/shape against `tests/api/test_dashboard.py` / `test_messages_endpoint.py` and adjust the completion request to match the app's actual endpoint and `FakeProvider` constructor.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/test_signup_e2e.py -v`
Expected: FAIL first (path/model tweaks), then drive to green.

- [ ] **Step 3: Make it pass**

Adjust the gateway request to the real endpoint and provider fake. No new production code should be required; if something is missing, that is a real gap - fix it in the owning module.

- [ ] **Step 4: Run the full suite**

Run: `pytest -q` (backend) and `cd dashboard && npx vitest run` (frontend).
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_signup_e2e.py
git commit -m "test(e2e): full self-serve signup to gateway flow"
```

### Task 20: Docs - update README getting-started

**Files:**
- Modify: `README.md`
- Modify: `.env.example` (add email/session settings)

- [ ] **Step 1: Add a "Getting started (self-serve signup)" section**

Document: visit the app -> Sign up -> verify email (in dev, the link is printed to the server log by the console email backend) -> wait for operator approval -> log in -> create an API key on the Keys tab. Note the operator still bootstraps the first operator account via `scripts/init-test-key.sh --operator`, and that operators approve pending requests from the dashboard's Pending Requests panel.

- [ ] **Step 2: Document new env vars in `.env.example`**

Add `EMAIL_BACKEND`, `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `PUBLIC_BASE_URL`, `SESSION_TTL_SECONDS`, `EMAIL_TOKEN_TTL_SECONDS` with sensible dev defaults and a one-line comment each.

- [ ] **Step 3: Commit**

```bash
git add README.md .env.example
git commit -m "docs: self-serve signup getting-started + env vars"
```

---

## Self-Review

**Spec coverage:**
- Data model (status + 3 tables) -> Tasks 1-2. ✓
- bcrypt passwords / token hashing -> Tasks 3-4. ✓
- Email pluggable backend (console/smtp) + config -> Tasks 5-6. ✓
- Server-side sessions -> Task 7. ✓
- Signup / verify / login / logout / reset -> Tasks 8-10, 12. ✓
- Operator approve/reject/list-pending -> Tasks 11, 14. ✓
- Session-or-key resolution + require_approved + CSRF -> Tasks 12-13. ✓
- Retire IdentityPicker; session gating; auth pages; operator panel -> Tasks 15-18. ✓
- Abuse controls (pre-auth IP limiter already fronts auth routes via the app) -> covered by existing middleware; auth router inherits it. ✓
- Backward compatibility (status default approved; CLI/scripts unchanged) -> Tasks 1, 8, 13 notes. ✓
- E2E + docs -> Tasks 19-20. ✓

**Placeholder scan:** No TBD/TODO; each code step carries real code. Frontend page bodies (Task 17) are specified behaviorally with concrete tests rather than full JSX for all six - acceptable because each is a standard controlled-form component and the tests pin the contract; the implementer writes idiomatic JSX to satisfy them.

**Type consistency:** `login` returns `(account, raw_session_token)` throughout; routes add CSRF separately. `approve_account` returns `(account, email)` (Task 11) and Task 14 consumes both. `create_session`/`resolve_session_account`/`revoke_session`/`revoke_account_sessions` names match across Tasks 7, 9, 10, 13. `SESSION_COOKIE`/`require_csrf` defined in Task 12, imported in Tasks 13-14. `status` added to `AccountOut`/`MeResponse` in Task 14 and consumed by the frontend in Tasks 16-18.

**Note for the implementer:** confirm the exact current field lists of `MeResponse`, `AccountOut`, `KeyCreateRequest`, and the gateway completion endpoint path before Tasks 13-14 and 19; the plan references them by the names visible today (`gatekeep/api/dashboard.py`), but adapt if they have drifted.
