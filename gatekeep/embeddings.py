from __future__ import annotations

from sentence_transformers import SentenceTransformer

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# This codebase has no real tokenizer, so text length is approximated with the
# common "~4 characters per token" heuristic for English text: 1000 tokens is
# treated as roughly 4000 characters.
_MAX_CHARS = 1000 * 4

_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    """Return the process-wide sentence-transformers model, loading it lazily on first use."""
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def is_too_long(text: str) -> bool:
    """Return True if text exceeds the ~1000-token proxy limit (character count / 4)."""
    return len(text) > _MAX_CHARS


def embed_text(text: str) -> list[float] | None:
    """Embed text with the local all-MiniLM-L6-v2 model, or None if it's too long to embed."""
    if is_too_long(text):
        return None
    vector = get_model().encode(text, convert_to_numpy=True)
    return vector.tolist()
