from __future__ import annotations

import asyncio

from sentence_transformers import SentenceTransformer

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# This codebase has no real tokenizer, so text length is approximated with the
# common "~4 characters per token" heuristic for English text: 1000 tokens is
# treated as roughly 4000 characters.
_MAX_CHARS = 1000 * 4

_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    """Return the process-wide sentence-transformers model, loading it lazily on first use.

    First load either reads ~90MB of weights off disk or, if the on-disk
    HF Hub cache is empty (a fresh container with no baked-in weights),
    downloads them over the network - both can take tens of seconds. Callers
    on a request path must not call this directly; use `warm()` at startup
    or `embed_text_async()` (which offloads to a thread) so that cost never
    blocks the event loop.
    """
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def warm() -> None:
    """Force the model to load now. Intended for an app startup hook, so the
    load/download cost is paid before the process accepts traffic rather
    than stalling whichever request happens to arrive first."""
    get_model()


def is_too_long(text: str) -> bool:
    """Return True if text exceeds the ~1000-token proxy limit (character count / 4)."""
    return len(text) > _MAX_CHARS


def embed_text(text: str) -> list[float] | None:
    """Embed text with the local all-MiniLM-L6-v2 model, or None if it's too long to embed.

    Synchronous and potentially slow (see `get_model()`); call from a worker
    thread (`embed_text_async`) rather than directly on a request path.
    """
    if is_too_long(text):
        return None
    vector = get_model().encode(text, convert_to_numpy=True)
    return vector.tolist()


async def embed_text_async(text: str) -> list[float] | None:
    """Async wrapper around `embed_text`, offloaded to a worker thread so a
    cold model load/download or a slow encode cannot block the event loop
    and stall unrelated concurrent requests."""
    return await asyncio.to_thread(embed_text, text)
