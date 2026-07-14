# Prompt templates

Each `*.txt` file here is the reviewed source of truth for a prompt, named by
its filename stem (`system-context.txt` -> prompt `system-context`). Changes go
through a PR so the diff gets human review and the eval-gate CI check
(`.github/workflows/eval-gate.yml`) runs against the change.

Merging a file **adds** a version via `gatekeep prompt sync prompts/`; it never
activates it. Promote explicitly with `gatekeep prompt promote <name> <version>`,
which runs the eval gate.
