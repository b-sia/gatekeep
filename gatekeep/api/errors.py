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


def map_anthropic_error(exc: Exception) -> JSONResponse:
    """Convert an exception raised by the Anthropic SDK into an OpenAI-shaped error response.

    Falls back to a 502 with the exception's string representation if the
    exception doesn't carry a `status_code`/`message` (as the SDK's own
    API error types do).
    """
    status = getattr(exc, "status_code", 502)
    message = getattr(exc, "message", None) or str(exc)
    return openai_error(status, message, "upstream_error", "anthropic_error")
