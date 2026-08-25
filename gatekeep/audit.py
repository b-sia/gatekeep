from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import AuditEvent


async def record_audit_event(
    session: AsyncSession,
    *,
    actor_account_id: int | None,
    actor_label: str,
    action: str,
    entity_type: str,
    entity_ref: str | None,
    result: str,
    version_num: int | None = None,
    details: dict | None = None,
) -> AuditEvent:
    """Append one audit event and commit it.

    This is the single writer of `audit_events` rows. It lives in the
    endpoint/job layer, never inside the pure service functions: each
    endpoint calls its service, then records the outcome here. `details`
    defaults to an empty dict.

    Args:
        session: The async DB session to write through.
        actor_account_id: The acting operator's account id, or None if the
            account is unknown/deleted.
        actor_label: The operator's name denormalized at action time.
        action: Namespaced verb, e.g. `prompt.promote`, `eval.run`.
        entity_type: The target entity kind, e.g. `prompt`, `eval_suite`.
        entity_ref: Human id of the target (e.g. prompt name), or None.
        result: One of `success`, `blocked`, `error`.
        version_num: Target version number where relevant.
        details: Action-specific JSON payload; defaults to `{}`.

    Returns:
        The persisted `AuditEvent`, refreshed.
    """
    event = AuditEvent(
        actor_account_id=actor_account_id,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_ref=entity_ref,
        version_num=version_num,
        result=result,
        details=details or {},
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event
