from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import Prompt, PromptVersion


class PromptNotFoundError(ValueError):
    """Raised when a prompt name has no registered Prompt row."""


class PromptVersionNotFoundError(ValueError):
    """Raised when a specific prompt version number does not exist for a prompt."""


async def _get_prompt_row(name: str, session: AsyncSession) -> Prompt:
    """Fetch the Prompt row for `name`; raises PromptNotFoundError if unknown."""
    prompt = (
        await session.execute(select(Prompt).where(Prompt.name == name))
    ).scalar_one_or_none()
    if prompt is None:
        raise PromptNotFoundError(f"no prompt registered with name {name!r}")
    return prompt


async def create_prompt(
    name: str,
    template: str,
    session: AsyncSession,
    *,
    created_by: str | None = None,
    notes: str | None = None,
) -> Prompt:
    """Create a new prompt with an initial version 1, marked active.

    Raises ValueError if a prompt with this name already exists. Creating a
    new version on an existing prompt is a separate operation
    (add_prompt_version); create is prompt-creation-only.
    """
    existing = (
        await session.execute(select(Prompt).where(Prompt.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"prompt {name!r} already exists")

    prompt = Prompt(name=name)
    session.add(prompt)
    await session.flush()

    version = PromptVersion(
        prompt_id=prompt.id,
        version_num=1,
        template=template,
        created_by=created_by,
        notes=notes,
        active=True,
    )
    session.add(version)
    await session.flush()

    prompt.active_version_id = version.id
    await session.commit()
    await session.refresh(prompt)
    return prompt


async def add_prompt_version(
    name: str,
    template: str,
    session: AsyncSession,
    *,
    created_by: str | None = None,
    notes: str | None = None,
) -> PromptVersion:
    """Add a new, inactive version to an existing prompt.

    Does not change which version is active; use promote_prompt for that.
    Raises PromptNotFoundError if the prompt does not exist.
    """
    prompt = await _get_prompt_row(name, session)
    max_version_num = (
        await session.execute(
            select(func.max(PromptVersion.version_num)).where(
                PromptVersion.prompt_id == prompt.id
            )
        )
    ).scalar_one()

    version = PromptVersion(
        prompt_id=prompt.id,
        version_num=max_version_num + 1,
        template=template,
        created_by=created_by,
        notes=notes,
        active=False,
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


async def promote_prompt(
    name: str, version_num: int, session: AsyncSession
) -> PromptVersion:
    """Atomically repoint a prompt's active version to an existing version_num.

    Flips the previously-active version's `active` flag to False and the
    newly-active one to True, keeping the denormalized flag consistent with
    Prompt.active_version_id. Also records the version being promoted away
    from in Prompt.previous_version_id, so rollback_prompt can revert to the
    version that was actually active before this promotion (not just
    version_num - 1). Raises PromptNotFoundError if the prompt doesn't
    exist, or PromptVersionNotFoundError if version_num doesn't exist for
    it.
    """
    prompt = await _get_prompt_row(name, session)
    target = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.prompt_id == prompt.id,
                PromptVersion.version_num == version_num,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise PromptVersionNotFoundError(
            f"prompt {name!r} has no version {version_num}"
        )

    if prompt.active_version_id is not None and prompt.active_version_id != target.id:
        previous = await session.get(PromptVersion, prompt.active_version_id)
        if previous is not None:
            previous.active = False
        prompt.previous_version_id = prompt.active_version_id

    target.active = True
    prompt.active_version_id = target.id
    await session.commit()
    await session.refresh(target)
    return target


async def rollback_prompt(name: str, session: AsyncSession) -> PromptVersion:
    """Revert a prompt to the version that was active immediately before its current one.

    Uses Prompt.previous_version_id, which promote_prompt keeps pointed at
    whatever was active right before the most recent promotion - this is
    real promotion history, not a guess based on version numbers, so it
    correctly handles prompts with drafted-but-never-promoted versions in
    between. Because rollback itself goes through promote_prompt, rolling
    back twice in a row toggles between the last two actually-active
    versions.

    Raises PromptNotFoundError if the prompt doesn't exist, or ValueError if
    there is no recorded previous version to roll back to (e.g. the prompt
    has only ever had one version, or has never been promoted since
    creation).
    """
    prompt = await _get_prompt_row(name, session)
    if prompt.previous_version_id is None:
        raise ValueError(f"prompt {name!r} has no earlier version to roll back to")
    previous = await session.get(PromptVersion, prompt.previous_version_id)
    return await promote_prompt(name, previous.version_num, session)


async def get_active_prompt_version(name: str, session: AsyncSession) -> PromptVersion:
    """Fetch the active PromptVersion row for `name`."""
    prompt = await _get_prompt_row(name, session)
    version = await session.get(PromptVersion, prompt.active_version_id)
    return version


async def get_prompt(name: str, session: AsyncSession) -> str:
    """Fetch the active template text for `name`.

    Raises PromptNotFoundError if no prompt is registered under this name.
    """
    version = await get_active_prompt_version(name, session)
    return version.template


async def list_prompts(session: AsyncSession) -> list[Prompt]:
    """List all registered prompts, ordered by name."""
    result = await session.execute(select(Prompt).order_by(Prompt.name))
    return list(result.scalars().all())
