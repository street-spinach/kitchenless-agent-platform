#!/usr/bin/env bash
# Passes if no critical path is touched, or if the PR has the approved-by-human label.
set -euo pipefail
base="$1"
# --no-renames lists a moved file under both its old and new path, so moving a file out of a
# critical path still counts as touching it.
changed=$(git diff --name-only --no-renames "$base"...HEAD)
critical=$(printf '%s\n' "$changed" | grep -E -f .github/critical-paths.txt || true)

if [ -z "$critical" ]; then
  echo "No critical paths touched. Auto-merge allowed."
  exit 0
fi

echo "Critical paths touched:"
printf '  %s\n' $critical
if [[ ",${LABELS:-}," == *",approved-by-human,"* ]]; then
  echo "approved-by-human label present. Auto-merge allowed."
  exit 0
fi
echo "::error::Critical change. Review the PR and add the approved-by-human label to merge."
exit 1
