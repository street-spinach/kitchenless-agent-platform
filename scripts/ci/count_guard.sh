#!/usr/bin/env bash
# Fails if the number of tests or eval cases drops compared with the base branch.
set -euo pipefail
base="$1"

count_tests() { (cd "$1" && uv run --quiet pytest --collect-only -q tests/unit tests/contract 2>/dev/null | grep -c '::' || true); }
count_evals() { find "$1/evals/cases" -name '*.yaml' 2>/dev/null | wc -l | tr -d ' '; }

tmp=$(mktemp -d)
git worktree add --quiet "$tmp" "$base"
base_tests=$(count_tests "$tmp"); base_evals=$(count_evals "$tmp")
head_tests=$(count_tests .);      head_evals=$(count_evals .)
git worktree remove --force "$tmp"

echo "Tests: base=$base_tests head=$head_tests | Eval cases: base=$base_evals head=$head_evals"
fail=0
[ "$head_tests" -lt "$base_tests" ] && { echo "::error::Test count dropped."; fail=1; }
[ "$head_evals" -lt "$base_evals" ] && { echo "::error::Eval case count dropped."; fail=1; }
exit $fail
