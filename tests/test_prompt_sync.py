from gatekeep.prompts.prompts import (
    create_prompt,
    get_active_prompt_version,
    sync_prompt_from_text,
)


async def test_sync_creates_prompt_when_absent(session):
    version = await sync_prompt_from_text("system-context", "hello", session)
    assert version.version_num == 1
    active = await get_active_prompt_version("system-context", session)
    assert active.template == "hello"


async def test_sync_adds_inactive_version_when_text_changes(session):
    await create_prompt("system-context", "v1", session)
    version = await sync_prompt_from_text("system-context", "v2 text", session)
    assert version.version_num == 2
    assert version.active is False
    # active is still v1 until an explicit, gated promote
    active = await get_active_prompt_version("system-context", session)
    assert active.version_num == 1


async def test_sync_is_noop_when_text_matches_active(session):
    await create_prompt("system-context", "same", session)
    version = await sync_prompt_from_text("system-context", "same", session)
    assert version.version_num == 1  # no new version created
