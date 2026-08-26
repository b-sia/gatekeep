from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_service import (
    AccountNotActiveError,
    EmailConflictError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
    InvalidTokenError,
)
from gatekeep.config import get_settings
from gatekeep.email import get_email_backend
from gatekeep.email.messages import build_reset_email, build_verification_email
from gatekeep.middleware.auth import _enforce_pre_auth_rate_limit
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
        raise HTTPException(
            status_code=403,
            detail={
                "error": {"message": "CSRF check failed.", "type": "permission_error", "code": None}
            },
        )


def _set_session_cookies(response: Response, session_token: str) -> str:
    """Set the session + CSRF cookies on a response and return the CSRF token."""
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=get_settings().session_ttl_seconds,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,
        secure=True,
        samesite="lax",
        max_age=get_settings().session_ttl_seconds,
    )
    return csrf


class SignupIn(BaseModel):
    """Request body for account signup."""

    email: EmailStr
    password: str


class TokenIn(BaseModel):
    """Request body for endpoints that consume a single-use email token."""

    token: str


class LoginIn(BaseModel):
    """Request body for email/password login."""

    email: EmailStr
    password: str


class ResetRequestIn(BaseModel):
    """Request body for initiating a password reset."""

    email: EmailStr


class ResetIn(BaseModel):
    """Request body for completing a password reset."""

    token: str
    new_password: str


@auth_router.post("/signup", status_code=202)
async def signup(
    body: SignupIn,
    session: AsyncSession = Depends(get_session),
    _pre_auth: None = Depends(_enforce_pre_auth_rate_limit),
) -> dict:
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
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": str(exc), "type": "invalid_request_error", "code": None}},
        ) from exc
    return {"status": "verified"}


@auth_router.post("/login")
async def login(
    body: LoginIn,
    response: Response,
    session: AsyncSession = Depends(get_session),
    _pre_auth: None = Depends(_enforce_pre_auth_rate_limit),
) -> dict:
    """Authenticate and set session + CSRF cookies. Returns the account status."""
    try:
        account, token = await auth_service.login(session, email=body.email, password=body.password)
    except (InvalidCredentialsError, EmailNotVerifiedError, AccountNotActiveError) as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Invalid email or password.",
                    "type": "authentication_error",
                    "code": None,
                }
            },
        ) from exc
    csrf = _set_session_cookies(response, token)
    return {
        "account_id": account.id,
        "status": account.status,
        "is_operator": account.is_operator,
        "csrf_token": csrf,
    }


@auth_router.post("/logout")
async def logout(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> dict:
    """Revoke the current session and clear cookies."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await auth_service.logout(session, raw_session_token=token)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"status": "ok"}


@auth_router.post("/password/reset-request", status_code=202)
async def reset_request(
    body: ResetRequestIn,
    session: AsyncSession = Depends(get_session),
    _pre_auth: None = Depends(_enforce_pre_auth_rate_limit),
) -> dict:
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
            session, raw_token=body.token, new_password=body.new_password
        )
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": str(exc), "type": "invalid_request_error", "code": None}},
        ) from exc
    return {"status": "ok"}
