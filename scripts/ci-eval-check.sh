#!/usr/bin/env bash
# Sync in-repo prompt files into a fresh DB and run each suite against the
# newly-synced (not-yet-promoted) version. Exit non-zero if any suite fails,
# turning the PR check red. Prompts with no registered suite are skipped by
# `gatekeep eval run` raising a "no suite" error, which we treat as a skip.
set -euo pipefail

echo "Applying migrations..."
alembic upgrade head

echo "Syncing prompt files..."
gatekeep prompt sync prompts/

echo "Loading eval-case fixtures..."
gatekeep eval load-fixtures prompts/

status=0
for file in prompts/*.txt; do
  [ -e "$file" ] || continue
  name="$(basename "$file" .txt)"
  echo "== eval: $name =="
  if output="$(gatekeep eval run "$name" 2>&1)"; then
    code=0
  else
    code=$?
  fi
  echo "$output"
  if [ "$code" -ne 0 ]; then
    # Distinguish "no suite" (skip) from a real gate failure, without
    # re-running the command (its provider calls are not free/idempotent-cheap).
    if echo "$output" | grep -q "no eval suite registered"; then
      echo "  (no suite; skipping)"
    else
      echo "  FAILED"
      status=1
    fi
  fi
done
exit "$status"
