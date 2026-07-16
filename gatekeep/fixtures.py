from __future__ import annotations

import json
import pathlib

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.evals import add_case, create_suite, get_suite_for_prompt
from gatekeep.models import EvalCase, EvalSuite


async def load_fixture_file(path: pathlib.Path, session: AsyncSession) -> EvalSuite:
    """Load one `<name>.cases.json` fixture into the DB, idempotently.

    Gets-or-creates the EvalSuite for the fixture's prompt name (the
    filename stem), updating pass_threshold to match the fixture. Deletes
    only this suite's existing source="fixture" cases before inserting the
    fixture's cases fresh with source="fixture", reviewed=True - manual and
    curated cases on the same suite are never touched, so re-running this
    (e.g. in CI, or against a persistent dev DB) is safe and repeatable.

    Args:
        path: Path to a `<name>.cases.json` fixture file.
        session: Active async DB session.

    Returns:
        The get-or-created EvalSuite for the fixture's prompt.
    """
    name = path.stem.removesuffix(".cases")
    data = json.loads(path.read_text(encoding="utf-8"))
    pass_threshold = data["pass_threshold"]
    cases = data.get("cases", [])

    suite = await get_suite_for_prompt(name, session)
    if suite is None:
        suite = await create_suite(name, session, pass_threshold=pass_threshold)
    else:
        suite.pass_threshold = pass_threshold
        await session.commit()
        await session.refresh(suite)

    await session.execute(
        delete(EvalCase).where(
            EvalCase.suite_id == suite.id, EvalCase.source == "fixture"
        )
    )
    await session.commit()

    for case in cases:
        await add_case(
            suite.id,
            session,
            input_messages=case["input_messages"],
            check_type=case["check_type"],
            expected=case.get("expected"),
            judge_criteria=case.get("judge_criteria"),
            reviewed=True,
            source="fixture",
        )

    return suite


async def load_fixtures_dir(directory: str, session: AsyncSession) -> list[EvalSuite]:
    """Load every `*.cases.json` fixture file in `directory`.

    Args:
        directory: Path to a directory containing `*.cases.json` fixture files
            (typically `prompts/`).
        session: Active async DB session.

    Returns:
        The list of EvalSuite rows loaded, one per fixture file, in sorted
        filename order.
    """
    suites = []
    for path in sorted(pathlib.Path(directory).glob("*.cases.json")):
        suites.append(await load_fixture_file(path, session))
    return suites
