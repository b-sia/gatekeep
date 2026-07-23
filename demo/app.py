"""
Demo application showing how to use Gatekeep as an OpenAI-compatible gateway.

This app demonstrates:
- Using Gatekeep instead of OpenAI API directly
- API key authentication
- Streaming responses
- Error handling

To use: Update GATEKEEP_URL and API_KEY below, then run this app.
"""

import json
import os
import pathlib
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse 
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load .env file if it exists
try:
    from dotenv import load_dotenv
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

app = FastAPI(title="Gatekeep Demo Chat")


class NoCacheMiddleware(BaseHTTPMiddleware):
    """Prevent caching of all responses in development."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.add_middleware(NoCacheMiddleware)

# Configuration - point to your Gatekeep instance
GATEKEEP_URL = os.getenv("GATEKEEP_URL", "http://localhost:8100")
API_KEY = os.getenv("GATEKEEP_API_KEY", "sk-test-key-goes-here")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-5")


class Message(BaseModel):
    """Request format for chat messages."""

    content: str
    model: str = DEFAULT_MODEL


@app.get("/")
async def index():
    """Serve the demo chat interface."""
    import pathlib
    html_path = pathlib.Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        response = FileResponse(html_path, media_type="text/html")
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    return {
        "message": "Chat demo API ready",
        "endpoints": {
            "chat": "POST /api/chat - Send a message (streaming)",
            "chat_sync": "POST /api/chat-sync - Send a message (non-streaming)",
        },
    }


@app.get("/dashboard")
async def dashboard_page():
    """Serve the cost/usage/eval dashboard page."""
    html_path = pathlib.Path(__file__).parent / "static" / "dashboard.html"
    response = FileResponse(html_path, media_type="text/html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


async def _proxy_dashboard_get(path: str, params: dict) -> dict:
    """Forward one GET request to a Gatekeep `/dashboard/api/...` endpoint.

    Attaches the demo's configured API_KEY as a Bearer token (kept
    server-side, never exposed to the browser) and forwards query
    parameters verbatim. Raises HTTPException mirroring the upstream
    status code on any non-2xx response or connection failure.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{GATEKEEP_URL}/dashboard/api/{path}",
                params={k: v for k, v in params.items() if v is not None},
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=30.0,
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Gateway error: {str(e)}")

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.get("/api/dashboard/summary")
async def dashboard_summary(
    start: str | None = None,
    end: str | None = None,
    model: str | None = None,
    key_id: int | None = None,
    prompt_name: str | None = None,
) -> dict:
    """Proxy for Gatekeep's `/dashboard/api/usage/summary` endpoint."""
    return await _proxy_dashboard_get(
        "usage/summary",
        {
            "start": start,
            "end": end,
            "model": model,
            "key_id": key_id,
            "prompt_name": prompt_name,
        },
    )


@app.get("/api/dashboard/timeseries")
async def dashboard_timeseries(
    start: str | None = None,
    end: str | None = None,
    interval: str = "day",
    model: str | None = None,
    key_id: int | None = None,
    prompt_name: str | None = None,
) -> dict:
    """Proxy for Gatekeep's `/dashboard/api/usage/timeseries` endpoint."""
    return await _proxy_dashboard_get(
        "usage/timeseries",
        {
            "start": start,
            "end": end,
            "interval": interval,
            "model": model,
            "key_id": key_id,
            "prompt_name": prompt_name,
        },
    )


@app.get("/api/dashboard/evals")
async def dashboard_evals(prompt_name: str | None = None, limit: int = 50) -> dict:
    """Proxy for Gatekeep's `/dashboard/api/evals` endpoint."""
    return await _proxy_dashboard_get(
        "evals", {"prompt_name": prompt_name, "limit": limit}
    )


@app.get("/api/dashboard/prompts")
async def dashboard_prompts() -> dict:
    """Proxy for Gatekeep's `/dashboard/api/prompts` endpoint."""
    return await _proxy_dashboard_get("prompts", {})


@app.get("/api/dashboard/prompts/{name}/versions")
async def dashboard_prompt_versions(name: str) -> dict:
    """Proxy for Gatekeep's `/dashboard/api/prompts/{name}/versions` endpoint."""
    return await _proxy_dashboard_get(f"prompts/{name}/versions", {})


@app.post("/api/chat")
async def chat_stream(message: Message) -> StreamingResponse:
    """
    Stream a chat completion response from Gatekeep.

    This demonstrates streaming - useful for real-time UI updates.
    """
    async def generate() -> AsyncGenerator[str, None]:
        """Stream responses from Gatekeep."""
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    f"{GATEKEEP_URL}/v1/chat/completions",
                    json={
                        "model": message.model,
                        "messages": [{"role": "user", "content": message.content}],
                        "stream": True,
                    },
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    timeout=60.0,
                ) as response:
                    if response.status_code != 200:
                        error_text = await response.aread()
                        payload = json.dumps(
                            {"error": f"Gateway error: {error_text.decode()}"}
                        )
                        yield f"data: {payload}\n\n"
                        return

                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            yield line + "\n"
        except httpx.RequestError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': f'Unexpected error: {e}'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/chat-sync")
async def chat_sync(message: Message) -> dict:
    """
    Send a chat completion request and return the full response.

    This demonstrates non-streaming - simpler but less responsive.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GATEKEEP_URL}/v1/chat/completions",
                json={
                    "model": message.model,
                    "messages": [{"role": "user", "content": message.content}],
                },
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=60.0,
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code, detail=response.text
                )

            return response.json()

    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Gateway error: {str(e)}")


# Serve static files (CSS, JS) from the demo/static directory
# Must be mounted after all other routes to avoid conflicts
try:
    import pathlib
    static_dir = pathlib.Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
except Exception:
    pass  # Static files optional if directory doesn't exist


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200)
