from __future__ import annotations

import gatekeep.app as app_module
from gatekeep.config import get_settings
from gatekeep.providers.stub import StubProvider


async def test_stub_request_returns_200_with_well_formed_body_when_registered(
    client, raw_key, monkeypatch
):
    monkeypatch.setenv("LOADTEST_STUB_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setitem(app_module._providers, "stub", StubProvider())

    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "stub/lat10-out10", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["completion_tokens"] == 10


async def test_stub_request_is_inert_when_not_registered(client, raw_key):
    assert "stub" not in app_module._providers
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "stub/lat10-out10", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"
