from __future__ import annotations

import random
from typing import TYPE_CHECKING

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.middleware.cache_exact import invalidate_prompt_cache
from gatekeep.middleware.cache_semantic import delete_cached_responses_by_prompt
from gatekeep.models import Prompt, PromptVersion

if TYPE_CHECKING:
    from gatekeep.evals import Gate


class PromptNotFoundError(ValueError):
    """Raised when a prompt name has no registered Prompt row."""


class PromptVersionNotFoundError(ValueError):
    """Raised when a specific prompt version number does not exist for a prompt."""


async def _get_prompt_row(name: str, session: AsyncSession) -> Prompt:
    """Fetch the Prompt row for `name`; raises PromptNotFoundError if unknown."""
    prompt = (await session.execute(select(Prompt).where(Prompt.name == name))).scalar_one_or_none()
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
            select(func.max(PromptVersion.version_num)).where(PromptVersion.prompt_id == prompt.id)
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
    name: str,
    version_num: int,
    session: AsyncSession,
    *,
    redis: Redis | None = None,
    gate: Gate | None = None,
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

    If `gate` is given, it is awaited with `(name, target, session)` after
    `target` is resolved but before any mutation happens. This is the eval
    gate: it runs the target version's eval suite (if one is registered)
    and raises EvalGateFailure to block promotion on a failing run, leaving
    `active_version_id` and all caches untouched. `gate` is optional and
    defaults to None (ungated), so existing callers are unaffected.
    rollback_prompt calls this function without a gate, so rollbacks are
    never eval-gated - reverting to an already-proven version doesn't need
    re-evaluation.

    Every promotion invalidates the cache entries this prompt previously
    produced, since they were built from substituted text that may no
    longer match the newly-active template: semantic-cache rows tagged with
    `name` are deleted in the same transaction as the version-pointer
    change, and, if `redis` is given, exact-cache entries tagged with `name`
    are deleted afterwards. Requests that never used `prompt_name` are
    untouched by this.
    """
    prompt = (
        await session.execute(select(Prompt).where(Prompt.name == name).with_for_update())
    ).scalar_one_or_none()
    if prompt is None:
        raise PromptNotFoundError(f"no prompt registered with name {name!r}")
    target = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.prompt_id == prompt.id,
                PromptVersion.version_num == version_num,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise PromptVersionNotFoundError(f"prompt {name!r} has no version {version_num}")

    if gate is not None:
        await gate(name, target, session)

    if prompt.active_version_id is not None and prompt.active_version_id != target.id:
        previous = await session.get(PromptVersion, prompt.active_version_id)
        if previous is not None:
            previous.active = False
        prompt.previous_version_id = prompt.active_version_id

    target.active = True
    prompt.active_version_id = target.id
    await delete_cached_responses_by_prompt(session, name)
    await session.commit()
    await session.refresh(target)
    if redis is not None:
        await invalidate_prompt_cache(redis, name)
    return target


async def rollback_prompt(
    name: str, session: AsyncSession, *, redis: Redis | None = None
) -> PromptVersion:
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
    return await promote_prompt(name, previous.version_num, session, redis=redis)


async def get_active_prompt_version(name: str, session: AsyncSession) -> PromptVersion:
    """Fetch the active PromptVersion row for `name`."""
    prompt = await _get_prompt_row(name, session)
    version = await session.get(PromptVersion, prompt.active_version_id)
    return version


async def get_prompt_row(name: str, session: AsyncSession) -> Prompt:
    """Fetch the Prompt row for `name`, exposing its candidate config.

    Unlike `get_active_prompt_version` (which returns the active
    PromptVersion's template), this returns the parent Prompt row itself -
    for callers that need to report or reason about
    `candidate_version_id`/`candidate_traffic_pct`, e.g. CLI display.

    Raises PromptNotFoundError if the prompt doesn't exist.
    """
    return await _get_prompt_row(name, session)


async def get_prompt(name: str, session: AsyncSession) -> str:
    """Fetch the active template text for `name`.

    Raises PromptNotFoundError if no prompt is registered under this name.
    """
    version = await get_active_prompt_version(name, session)
    return version.template


async def set_candidate_version(
    name: str,
    version_num: int,
    traffic_pct: float,
    session: AsyncSession,
) -> Prompt:
    """Configure an A/B testing candidate for a prompt (partial traffic split).

    Points `Prompt.candidate_version_id` at `version_num` and
    `Prompt.candidate_traffic_pct` at `traffic_pct`, so
    `resolve_prompt_version_for_request` starts sending that percentage of
    requests to the candidate version instead of the active one. This is
    deliberately a lighter-weight sibling of `promote_prompt`, not a variant
    of it: a candidate is not "active", so setting one must never run the
    eval gate or invalidate any cache - those only make sense for the
    version actually serving 100% of default traffic. Calling this again
    replaces any previously configured candidate/percentage (e.g. to widen
    a rollout from 10% to 50%).

    Raises PromptNotFoundError if the prompt doesn't exist,
    PromptVersionNotFoundError if version_num doesn't exist for it, and
    ValueError if traffic_pct is outside the inclusive [0, 100] range.
    """
    if not 0 <= traffic_pct <= 100:
        raise ValueError(f"traffic_pct must be between 0 and 100, got {traffic_pct!r}")
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
        raise PromptVersionNotFoundError(f"prompt {name!r} has no version {version_num}")

    prompt.candidate_version_id = target.id
    prompt.candidate_traffic_pct = traffic_pct
    await session.commit()
    await session.refresh(prompt)
    return prompt


async def clear_candidate_version(name: str, session: AsyncSession) -> Prompt:
    """Remove any configured A/B candidate for a prompt, restoring 100% active traffic.

    As lightweight as `set_candidate_version`: no eval gate, no cache
    invalidation - clearing a candidate never touches what's actually
    serving default traffic. A no-op (but still succeeds) if no candidate
    was configured. Raises PromptNotFoundError if the prompt doesn't exist.
    """
    prompt = await _get_prompt_row(name, session)
    prompt.candidate_version_id = None
    prompt.candidate_traffic_pct = None
    await session.commit()
    await session.refresh(prompt)
    return prompt


async def resolve_prompt_version_for_request(name: str, session: AsyncSession) -> PromptVersion:
    """Resolve which PromptVersion should serve one incoming request.

    This is the request-time A/B split: when a prompt has no candidate
    configured (the default), this always returns the active version,
    identical to `get_active_prompt_version` - existing promote/rollback
    behavior is completely unaffected by this function existing.

    When a candidate *is* configured, each call independently draws a fresh
    `random.random()` and routes to the candidate iff the draw falls within
    `candidate_traffic_pct` percent, otherwise to the active version. This
    is a stateless, per-request random split rather than a sticky
    per-key/session assignment: it's the simpler of the two designs (no
    assignment state to persist or expire), and matches the roadmap's
    framing of this as measuring a population-level traffic split rather
    than tracking individual users through a multi-request experience. The
    tradeoff is that a single client can be assigned to either version
    across consecutive requests in the same "experiment" - acceptable here
    since these are independent, stateless completion requests, not a
    multi-turn conversation pinned to one prompt version.

    "Is a candidate configured" (`candidate_version_id is not None`) and
    "how much traffic routes to it" (`candidate_traffic_pct`) are distinct
    states: a candidate configured at 0% traffic is a real, deliberate
    state - e.g. a rollout paused back to 0% without discarding which
    version it was testing, so it can be resumed by raising the pct again
    - and it is visible as such via `gatekeep prompt show` even though, for
    routing purposes, it behaves identically to no candidate (100% active).
    100% behaves like always-candidate. If `candidate_version_id` ever
    pointed at a row that no longer exists, this falls back to the active
    version rather than raising - in practice the `prompts.
    candidate_version_id` foreign key already makes that state
    unreachable (versions are immutable and never deleted), so this is
    defense-in-depth rather than a path exercised in normal operation.

    Raises PromptNotFoundError if the prompt doesn't exist.
    """
    prompt = await _get_prompt_row(name, session)
    active = await session.get(PromptVersion, prompt.active_version_id)
    # "is a candidate configured" (candidate_version_id) and "how much
    # traffic routes to it" (candidate_traffic_pct) are deliberately checked
    # separately: a candidate configured at 0% is a real, distinct state
    # (e.g. a paused rollout, kept configured so it can be resumed by just
    # raising the pct) from no candidate being configured at all - even
    # though both currently route 100% of requests to the active version.
    if prompt.candidate_version_id is None:
        return active
    traffic_pct = prompt.candidate_traffic_pct or 0.0
    if random.random() * 100 < traffic_pct:
        candidate = await session.get(PromptVersion, prompt.candidate_version_id)
        if candidate is not None:
            return candidate
    return active


async def list_prompts(session: AsyncSession) -> list[Prompt]:
    """List all registered prompts, ordered by name."""
    result = await session.execute(select(Prompt).order_by(Prompt.name))
    return list(result.scalars().all())


async def sync_prompt_from_text(name: str, template: str, session: AsyncSession) -> PromptVersion:
    """Idempotently reconcile a prompt with in-repo template text.

    Creates the prompt (version 1, active) if it does not exist; adds a new
    inactive version if the text differs from the current active version;
    returns the existing active version unchanged if the text already matches.
    Never promotes - activation stays an explicit, eval-gated step.
    """
    existing = (
        await session.execute(select(Prompt).where(Prompt.name == name))
    ).scalar_one_or_none()
    if existing is None:
        prompt = await create_prompt(name, template, session)
        return await get_active_prompt_version(prompt.name, session)

    active = await get_active_prompt_version(name, session)
    if active.template == template:
        return active
    return await add_prompt_version(name, template, session)
