import json

from sqlalchemy import select

from gatekeep.prompts.evals import create_suite
from gatekeep.prompts.fixtures import load_fixture_file, load_fixtures_dir
from gatekeep.storage.models import EvalCase


def _write_fixture(tmp_path, name, pass_threshold, cases):
    path = tmp_path / f"{name}.cases.json"
    path.write_text(json.dumps({"pass_threshold": pass_threshold, "cases": cases}))
    return path


async def test_load_fixture_file_creates_suite_and_cases(tmp_path, session):
    path = _write_fixture(
        tmp_path,
        "system-context",
        1.0,
        [
            {
                "input_messages": [{"role": "user", "content": "2+2?"}],
                "check_type": "contains",
                "expected": "4",
            }
        ],
    )

    suite = await load_fixture_file(path, session)
    assert suite.prompt_name == "system-context"
    assert suite.pass_threshold == 1.0

    cases = (
        (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite.id)))
        .scalars()
        .all()
    )
    assert len(cases) == 1
    assert cases[0].source == "fixture"
    assert cases[0].reviewed is True
    assert cases[0].check_type == "contains"


async def test_load_fixture_file_updates_threshold_and_replaces_fixture_cases(tmp_path, session):
    await create_suite("system-context", session, pass_threshold=0.5)
    path = _write_fixture(
        tmp_path,
        "system-context",
        0.9,
        [
            {
                "input_messages": [{"role": "user", "content": "hi"}],
                "check_type": "contains",
                "expected": "hello",
            }
        ],
    )

    await load_fixture_file(path, session)
    # loading again with different content must not duplicate rows
    path2 = _write_fixture(
        tmp_path,
        "system-context",
        0.9,
        [
            {
                "input_messages": [{"role": "user", "content": "bye"}],
                "check_type": "contains",
                "expected": "goodbye",
            }
        ],
    )
    suite = await load_fixture_file(path2, session)

    assert suite.pass_threshold == 0.9
    cases = (
        (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite.id)))
        .scalars()
        .all()
    )
    assert len(cases) == 1
    assert cases[0].expected == "goodbye"


async def test_load_fixture_file_never_touches_manual_or_curated_cases(tmp_path, session):
    from gatekeep.prompts.evals import add_case

    suite = await create_suite("system-context", session, pass_threshold=0.9)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "manual"}],
        check_type="contains",
        expected="m",
        source="manual",
    )
    path = _write_fixture(
        tmp_path,
        "system-context",
        0.9,
        [
            {
                "input_messages": [{"role": "user", "content": "fixture"}],
                "check_type": "contains",
                "expected": "f",
            }
        ],
    )

    await load_fixture_file(path, session)

    cases = (
        (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite.id)))
        .scalars()
        .all()
    )
    sources = {c.source for c in cases}
    assert sources == {"manual", "fixture"}
    assert len(cases) == 2


async def test_load_fixtures_dir_loads_every_cases_json(tmp_path, session):
    _write_fixture(tmp_path, "a", 1.0, [])
    _write_fixture(tmp_path, "b", 1.0, [])

    suites = await load_fixtures_dir(str(tmp_path), session)
    names = {s.prompt_name for s in suites}
    assert names == {"a", "b"}
