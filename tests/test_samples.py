from gatekeep.models import ApiKey
from gatekeep.samples import recent_samples, record_request_sample
from tests.helpers import create_account


async def _key(session):
    """Create and flush an ApiKey on a freshly created account."""
    account = await create_account(session)
    key = ApiKey(name="k", key_hash="h", account_id=account.id)
    session.add(key)
    await session.flush()
    return key


async def test_record_and_read_recent_samples_newest_first(session):
    key = await _key(session)
    for i in range(3):
        await record_request_sample(
            session,
            key_id=key.id,
            account_id=key.account_id,
            prompt_name="p",
            model="claude-sonnet-5",
            input_messages=[{"role": "user", "content": f"m{i}"}],
            output_text=f"o{i}",
        )

    got = await recent_samples("p", session, limit=2)
    assert [s.output_text for s in got] == ["o2", "o1"]


async def test_recent_samples_filters_by_prompt_name(session):
    key = await _key(session)
    await record_request_sample(
        session,
        key_id=key.id,
        account_id=key.account_id,
        prompt_name="a",
        model="m",
        input_messages=[{"role": "user", "content": "x"}],
        output_text="ox",
    )
    await record_request_sample(
        session,
        key_id=key.id,
        account_id=key.account_id,
        prompt_name="b",
        model="m",
        input_messages=[{"role": "user", "content": "y"}],
        output_text="oy",
    )
    got = await recent_samples("a", session, limit=10)
    assert [s.output_text for s in got] == ["ox"]


async def test_record_request_sample_stamps_account(session):
    """The persisted row carries the caller's account_id (decision 4)."""
    key = await _key(session)
    sample = await record_request_sample(
        session,
        key_id=key.id,
        account_id=key.account_id,
        prompt_name="system-context",
        model="claude-sonnet-5",
        input_messages=[{"role": "user", "content": "hi"}],
        output_text="hello",
    )
    assert sample.account_id == key.account_id
