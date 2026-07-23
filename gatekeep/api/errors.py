from __future__ import annotations

from fastapi.responses import JSONResponse


def openai_error(
    status_code: int, message: str, err_type: str, code: str | None = None
) -> JSONResponse:
    """Build an OpenAI-shaped `{"error": {...}}` JSON error response."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


def map_provider_error(exc: Exception) -> JSONResponse:
    """Convert an exception raised by a provider SDK into an OpenAI-shaped error response.

    Falls back to a 502 with the exception's string representation if the
    exception doesn't carry a `status_code`/`message` (as the SDK's own
    API error types do).
    """
    status = getattr(exc, "status_code", 502)
    message = getattr(exc, "message", None) or str(exc)
    return openai_error(status, message, "upstream_error", "provider_error")


def anthropic_error(status_code: int, message: str, err_type: str) -> JSONResponse:
    """Build an Anthropic-shaped `{"type": "error", "error": {...}}` JSON error response."""
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": err_type, "message": message}},
    )


def map_provider_error_anthropic(exc: Exception) -> JSONResponse:
    """Convert an exception raised by a provider SDK into an Anthropic-shaped error response.

    Mirrors `map_provider_error`, but wraps the body in Anthropic's
    `{"type": "error", "error": {...}}` envelope with an Anthropic-vocabulary
    error type (`rate_limit_error` for 429s, `api_error` otherwise) instead of
    OpenAI's flat `{"error": {...}}` shape.
    """
    status = getattr(exc, "status_code", 502)
    message = getattr(exc, "message", None) or str(exc)
    err_type = "rate_limit_error" if status == 429 else "api_error"
    return anthropic_error(status, message, err_type)


def openai_error_to_anthropic(detail: dict) -> dict:
    """Convert an OpenAI-shaped `{"error": {"message", "type", "code"}}` body into
    Anthropic's `{"type": "error", "error": {"type", "message"}}` envelope.

    Used to reshape errors raised by shared dependencies (`require_api_key`,
    `require_rate_limit`) which only know the OpenAI shape, for requests
    hitting the Anthropic-native `/v1/messages` endpoint. The `type` values
    used by those dependencies (`authentication_error`, `rate_limit_error`,
    `service_unavailable_error`) are already valid Anthropic error types, so
    only the envelope and the dropped `code` field change.
    """
    inner = detail["error"]
    return {
        "type": "error",
        "error": {"type": inner.get("type", "api_error"), "message": inner["message"]},
    }
