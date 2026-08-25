from __future__ import annotations

from gatekeep.audit.audit import record_audit_event
from gatekeep.storage.models import AuditEvent
from tests.helpers import create_account


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


async def test_record_audit_event_persists_and_commits(session):
    actor = await create_account(session, name="ops", is_operator=True)
    await session.commit()

    event = await record_audit_event(
        session,
        actor_account_id=actor.id,
        actor_label=actor.name,
        action="prompt.create",
        entity_type="prompt",
        entity_ref="greeting",
        result="success",
        details={"template_len": 12},
    )

    assert event.id is not None
    assert event.result == "success"
    assert event.actor_label == "ops"
    assert event.version_num is None
    assert event.details == {"template_len": 12}


async def test_record_audit_event_defaults_details_to_empty_dict(session):
    event = await record_audit_event(
        session,
        actor_account_id=None,
        actor_label="ops",
        action="eval.run",
        entity_type="prompt",
        entity_ref="greeting",
        result="error",
    )
    assert event.details == {}
