from __future__ import annotations

import argparse
import asyncio
import sys

from gatekeep.db import SessionLocal
from gatekeep.prompts import (
    PromptNotFoundError,
    PromptVersionNotFoundError,
    add_prompt_version,
    create_prompt,
    get_active_prompt_version,
    list_prompts,
    promote_prompt,
    rollback_prompt,
)


async def _create(name: str, template_file: str) -> None:
    """Create a new prompt from the template text in `template_file` (version 1, active)."""
    with open(template_file, encoding="utf-8") as f:
        template = f.read()
    async with SessionLocal() as session:
        await create_prompt(name, template, session)
    print(f"created prompt {name!r} at version 1")


async def _add_version(name: str, template_file: str) -> None:
    """Add a new, inactive version to an existing prompt (does not promote it)."""
    with open(template_file, encoding="utf-8") as f:
        template = f.read()
    async with SessionLocal() as session:
        version = await add_prompt_version(name, template, session)
    print(f"added version {version.version_num} to {name!r} (not active; promote it)")


async def _list() -> None:
    """Print every registered prompt name with its current active version number."""
    async with SessionLocal() as session:
        prompts = await list_prompts(session)
        for prompt in prompts:
            version = await get_active_prompt_version(prompt.name, session)
            print(f"{prompt.name}\tv{version.version_num}")


async def _show(name: str) -> None:
    """Print the active version's number and template text for a prompt."""
    async with SessionLocal() as session:
        version = await get_active_prompt_version(name, session)
        print(f"# {name} (active version {version.version_num})")
        print(version.template)


async def _promote(name: str, version_num: int) -> None:
    """Promote an existing prompt version to active."""
    async with SessionLocal() as session:
        promoted = await promote_prompt(name, version_num, session)
    print(f"promoted {name!r} to version {promoted.version_num}")


async def _rollback(name: str) -> None:
    """Revert a prompt's active version to version_num - 1."""
    async with SessionLocal() as session:
        rolled_back = await rollback_prompt(name, session)
    print(f"rolled back {name!r} to version {rolled_back.version_num}")


def build_parser() -> argparse.ArgumentParser:
    """Build the `gatekeep` CLI's argument parser and its `prompt` subcommands."""
    parser = argparse.ArgumentParser(prog="gatekeep")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prompt_parser = subparsers.add_parser("prompt", help="manage prompt templates")
    prompt_subparsers = prompt_parser.add_subparsers(
        dest="prompt_command", required=True
    )

    create_parser = prompt_subparsers.add_parser(
        "create", help="create a new prompt from a template file"
    )
    create_parser.add_argument("name")
    create_parser.add_argument("template_file")

    add_version_parser = prompt_subparsers.add_parser(
        "add-version",
        help="add a new (inactive) version to an existing prompt; promote it to activate",
    )
    add_version_parser.add_argument("name")
    add_version_parser.add_argument("template_file")

    prompt_subparsers.add_parser("list", help="list all prompts and active versions")

    show_parser = prompt_subparsers.add_parser(
        "show", help="show a prompt's active template"
    )
    show_parser.add_argument("name")

    promote_parser = prompt_subparsers.add_parser(
        "promote", help="promote an existing prompt version to active"
    )
    promote_parser.add_argument("name")
    promote_parser.add_argument("version", type=int)

    rollback_parser = prompt_subparsers.add_parser(
        "rollback", help="revert a prompt to its previous version"
    )
    rollback_parser.add_argument("name")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `gatekeep` console script; dispatches `prompt` subcommands."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "prompt":
            if args.prompt_command == "create":
                asyncio.run(_create(args.name, args.template_file))
            elif args.prompt_command == "add-version":
                asyncio.run(_add_version(args.name, args.template_file))
            elif args.prompt_command == "list":
                asyncio.run(_list())
            elif args.prompt_command == "show":
                asyncio.run(_show(args.name))
            elif args.prompt_command == "promote":
                asyncio.run(_promote(args.name, args.version))
            elif args.prompt_command == "rollback":
                asyncio.run(_rollback(args.name))
    except (PromptNotFoundError, PromptVersionNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
