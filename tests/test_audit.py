from __future__ import annotations

from gatekeep.models import AuditEvent


async def test_audit_event_row_roundtrips(session):
    event = AuditEvent(
        actor_account_id=None,
        actor_label="op-acct",
        action="prompt.promote",
        entity_type="prompt",
        entity_ref="greeting",
        version_num=2,
        result="success",
        details={"from_version": 1, "to_version": 2},
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)

    assert event.id is not None
    assert event.created_at is not None
    assert event.details["to_version"] == 2
