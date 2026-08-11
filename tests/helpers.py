from __future__ import annotations

from gatekeep.providers.base import CompletionResult


class FakeProvider:
    """Provider stub returning queued texts (or raising queued exceptions) in order,
    one per complete() call."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.payloads = []

    async def complete(self, payload):
        self.payloads.append(payload)
        text = self._texts.pop(0)
        if isinstance(text, Exception):
            raise text
        return CompletionResult(text=text, input_tokens=1, output_tokens=1, stop_reason="stop")
