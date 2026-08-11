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

## Building a suite

Register a suite and cases, then curate more from real traffic:

```bash
# One suite per prompt; threshold defaults to EVAL_PASS_THRESHOLD_DEFAULT.
gatekeep eval create-suite system-context --threshold 0.9

# Add a deterministic case from a JSON messages file.
echo '[{"role":"user","content":"ping"}]' > /tmp/case.json
gatekeep eval add-case system-context --input-file /tmp/case.json \
  --check-type contains --expected pong

# Grow the suite from recent real traffic (writes unreviewed cases).
gatekeep eval curate system-context --limit 20
gatekeep eval review system-context   # approve/reject each, interactively

# Run the suite manually (defaults to the active version + DEFAULT_MODEL).
gatekeep eval run system-context
```

The `llm_judge` check grades output with a fixed, stronger judge model
(`EVAL_JUDGE_MODEL`, default `claude-sonnet-5`) rather than the model under
test, so a model cannot rubber-stamp its own failure mode.

## Promotion gating

`gatekeep prompt promote <name> <version>` runs the suite first and refuses to
activate a version scoring below the suite threshold, printing a per-case
report. Prompts with no suite promote unconditionally - the gate is opt-in.
`gatekeep prompt rollback` is never gated.

Promoting a version also invalidates cached responses built from the old one,
so clients never see an answer generated from an inactive prompt.

CI needs an `ANTHROPIC_API_KEY` repository secret so the gate's generation and
judge calls can reach the provider.

## Cost-based routing

A prompt's eval history is what makes `"route_by_cost": true` safe: the gateway
will only substitute a cheaper model that has a passing eval run for that
prompt. See [`gatekeep/providers/README.md`](../gatekeep/providers/README.md).

## Local setup

To add or edit a prompt, run the CLI against a local Gatekeep install rather
than editing the DB directly - see "Testing" in the root `README.md` for
installing the package and running Postgres/Redis locally. `gatekeep prompt
--help` and `gatekeep eval --help` list every subcommand referenced above.
