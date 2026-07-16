# Prompt templates

Each `*.txt` file here is the reviewed source of truth for a prompt, named by
its filename stem (`system-context.txt` -> prompt `system-context`). Changes go
through a PR so the diff gets human review and the eval-gate CI check
(`.github/workflows/eval-gate.yml`) runs against the change.

Merging a file **adds** a version via `gatekeep prompt sync prompts/`; it never
activates it. Promote explicitly with `gatekeep prompt promote <name> <version>`,
which runs the eval gate.

## Eval-case fixtures

A prompt's CI-gating eval cases live in `<name>.cases.json`, sibling to
`<name>.txt` (e.g. `system-context.cases.json` alongside `system-context.txt`).
Each fixture holds a `pass_threshold` and a list of `cases` in the same shape
`gatekeep eval add-case` accepts. Loading a directory of fixtures with
`gatekeep eval load-fixtures prompts/` (which the eval-gate CI check already
runs) gets-or-creates the suite for each prompt and replaces only that suite's
`source="fixture"` cases - it never touches manually-added (`source="manual"`)
or curated-and-approved (`source="curated"`) cases, so it's safe to re-run
against a persistent dev DB as well as CI's fresh ephemeral one.

## Local setup

To add or edit a prompt, run the CLI against a local Gatekeep install rather
than editing the DB directly - see "Develop on Gatekeep" in the root
`README.md` for installing the package and running Postgres/Redis locally.
`gatekeep prompt --help` and `gatekeep eval --help` list every subcommand
referenced above.
