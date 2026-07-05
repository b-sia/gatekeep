from __future__ import annotations

from fastapi.responses import JSONResponse


def openai_error(
    status_code: int, message: str, err_type: str, code: str | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


def map_anthropic_error(exc: Exception) -> JSONResponse:
    status = getattr(exc, "status_code", 502)
    message = getattr(exc, "message", None) or str(exc)
    return openai_error(status, message, "upstream_error", "anthropic_error")
