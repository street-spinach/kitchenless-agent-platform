#!/usr/bin/env bash
# Unattended issue loop: one issue at a time, auto-merge on green, two strikes.
set -uo pipefail

MAX_ISSUES="${MAX_ISSUES:-10}"
MAX_TURNS="${MAX_TURNS:-80}"
MERGE_TIMEOUT_MIN="${MERGE_TIMEOUT_MIN:-20}"
STOP_FILE=".loop-stop"
LOG_DIR=".loop-logs"
mkdir -p "$LOG_DIR"

log() { echo "[$(date +%H:%M:%S)] $*"; }

deps_done() {
  local deps d
  deps=$(gh issue view "$1" --json body -q .body \
    | awk 'tolower($0) ~ /^## depends on/{f=1;next} /^## /{f=0} f' \
    | grep -oE '#[0-9]+' | tr -d '#' || true)
  for d in $deps; do
    [ "$(gh issue view "$d" --json state -q .state)" = "CLOSED" ] || return 1
  done
  return 0
}

next_issue() {
  local n
  for n in $(gh issue list --label ready --state open --limit 100 \
               --json number -q 'sort_by(.number) | .[].number'); do
    if deps_done "$n"; then echo "$n"; return; fi
  done
}

attempts() {
  gh issue view "$1" --json labels \
    -q '[.labels[].name | select(startswith("attempt-"))] | length'
}

has_label() {
  gh issue view "$1" --json labels -q '.labels[].name' | grep -qx "$2"
}

run_claude() {  # $1 prompt file, $2 issue, $3 pr (optional)
  local prompt
  prompt=$(sed -e "s/{{ISSUE}}/$2/g" -e "s/{{PR}}/${3:-}/g" "$1")
  claude -p "$prompt" \
    --permission-mode acceptEdits \
    --max-turns "$MAX_TURNS" \
    --output-format json \
    > "$LOG_DIR/issue-$2-$(date +%s).json" 2>&1
}

failing_checks() {
  gh pr checks "$1" --json name,bucket -q '.[] | select(.bucket=="fail") | .name'
}

wait_checks() { gh pr checks "$1" --watch --interval 30 >/dev/null 2>&1 || true; }

wait_merged() {
  local _
  for _ in $(seq 1 $((MERGE_TIMEOUT_MIN * 2))); do
    [ "$(gh pr view "$1" --json state -q .state)" = "MERGED" ] && return 0
    sleep 30
  done
  return 1
}

done_count=0
while [ "$done_count" -lt "$MAX_ISSUES" ] && [ ! -f "$STOP_FILE" ]; do
  # Without set -e a failed checkout is silent, so leftover changes from an agent run would
  # carry into the next issue's branch. Stop instead; never reset the tree automatically.
  if [ -n "$(git status --porcelain)" ]; then
    log "Working tree has uncommitted changes. Stopping. Inspect with: git status"
    break
  fi

  issue=$(next_issue)
  if [ -z "$issue" ]; then log "No ready issues. Stopping."; break; fi

  a=$(attempts "$issue")
  if [ "$a" -ge 2 ]; then
    gh issue edit "$issue" --remove-label ready --add-label needs-human
    gh issue comment "$issue" --body "Loop: two attempts used. Needs a human."
    continue
  fi

  gh issue edit "$issue" --remove-label ready --add-label "in-progress,attempt-$((a + 1))"
  git checkout -q main && git pull -q --ff-only
  git checkout -q -B "issue-$issue"
  log "Implementing #$issue (attempt $((a + 1)))"
  run_claude scripts/prompts/implement.md "$issue"

  pr=$(gh pr list --head "issue-$issue" --state open --json number -q '.[0].number')
  if [ -z "$pr" ]; then
    if ! has_label "$issue" needs-human && ! has_label "$issue" design-change; then
      gh issue edit "$issue" --add-label needs-human
      gh issue comment "$issue" --body "Loop: agent finished without opening a PR. See local logs."
    fi
    gh issue edit "$issue" --remove-label in-progress
    git checkout -q main
    continue
  fi

  gh pr merge "$pr" --auto --squash --delete-branch
  log "PR #$pr opened, auto-merge enabled. Waiting for checks."
  sleep 30   # give GitHub time to register the checks
  wait_checks "$pr"
  failed=$(failing_checks "$pr")

  if [ "$failed" = "merge-policy" ]; then
    gh issue edit "$issue" --remove-label in-progress --add-label awaiting-review
    log "#$issue touches critical paths. Waiting for your review on PR #$pr."
    git checkout -q main
    continue
  fi

  if [ -n "$failed" ]; then
    log "Checks failed on PR #$pr: $(echo "$failed" | tr '\n' ' '). One fix attempt."
    git checkout -q "issue-$issue"
    run_claude scripts/prompts/fix.md "$issue" "$pr"
    sleep 20
    wait_checks "$pr"
    failed=$(failing_checks "$pr")
    if [ "$failed" = "merge-policy" ]; then
      gh issue edit "$issue" --remove-label in-progress --add-label awaiting-review
      git checkout -q main
      continue
    fi
    if [ -n "$failed" ]; then
      gh issue edit "$issue" --remove-label in-progress --add-label needs-human
      gh issue comment "$issue" --body "Loop: checks still failing on PR #$pr after a fix attempt: $failed"
      git checkout -q main
      continue
    fi
  fi

  if wait_merged "$pr"; then
    log "PR #$pr merged. Deploy runs in GitHub Actions."
    done_count=$((done_count + 1))
  else
    gh issue edit "$issue" --remove-label in-progress --add-label needs-human
    gh issue comment "$issue" --body "Loop: PR #$pr green but not merged in time. Check branch protection."
  fi
  gh issue edit "$issue" --remove-label in-progress 2>/dev/null || true
  git checkout -q main
done
log "Loop finished. Issues merged: $done_count"
