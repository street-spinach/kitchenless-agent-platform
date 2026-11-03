#!/usr/bin/env bash
# Unattended issue loop: one issue at a time, auto-merge on green, two strikes.
set -uo pipefail

MAX_ISSUES="${MAX_ISSUES:-10}"
MAX_TURNS="${MAX_TURNS:-80}"
MERGE_TIMEOUT_MIN="${MERGE_TIMEOUT_MIN:-45}"
# Minutes between retries after the agent hits the account's usage limit.
LIMIT_WAIT_MIN="${LIMIT_WAIT_MIN:-20}"
STOP_FILE=".loop-stop"
LOG_DIR=".loop-logs"
mkdir -p "$LOG_DIR"
LAST_LOG=""

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
  LAST_LOG="$LOG_DIR/issue-$2-$(date +%s).json"
  claude -p "$prompt" \
    --permission-mode acceptEdits \
    --max-turns "$MAX_TURNS" \
    --output-format json \
    > "$LAST_LOG" 2>&1
}

hit_usage_limit() {  # the last agent run stopped on the account's usage limit
  grep -Eq '"api_error" *: *"usage_limit_reached"|"api_error_status" *: *429' "$LAST_LOG"
}

# Run the agent. On a usage limit, keep the tree as it is (the work so far stays on the
# branch), wait, and run the same prompt again so the next run continues the work. These
# retries never count as attempts. Returns 1 if the stop file appears while waiting.
run_agent() {  # same arguments as run_claude
  local _
  while true; do
    run_claude "$@"
    hit_usage_limit || return 0
    log "Usage limit reached on #$2. Retrying every $LIMIT_WAIT_MIN min; the work so far stays on the branch."
    for _ in $(seq 1 "$LIMIT_WAIT_MIN"); do
      [ -f "$STOP_FILE" ] && return 1
      sleep 60
    done
  done
}

# Commit any changes an agent run left behind to a new wip branch, push it and say so on
# the issue, so the next issue starts clean and nothing is lost. Never discards work.
# Returns 1 if the changes could not be saved; the caller then stops the loop.
save_wip() {  # $1 issue, $2 why
  [ -z "$(git status --porcelain)" ] && return 0
  local branch
  branch="wip-$1-$(date +%Y%m%d-%H%M%S)"
  git checkout -q -b "$branch" || return 1
  git add -A || return 1
  git commit -q -m "wip(#$1): $2. Left uncommitted by the loop agent, not reviewed." || return 1
  if git push -q -u origin "$branch"; then
    gh issue comment "$1" --body "Loop: $2. The agent left uncommitted work; it is saved, not reviewed, on branch \`$branch\`." || true
  else
    log "Could not push $branch; it stays local only."
  fi
  log "Saved leftover work from #$1 on $branch."
}

# Leave the issue's branch for main, saving leftover work first.
back_to_main() {  # $1 issue, $2 why
  save_wip "$1" "$2" || { log "Could not save leftover work from #$1."; return 1; }
  git checkout -q main
}

# Agents start throwaway Postgres servers (pgserver) with their data under the log dir, and
# nothing stops or removes them; they once filled the disk. Run between issues only.
clean_scratch_db() {
  local pid
  for pid in $(pgrep -f "postgres.* -D $PWD/$LOG_DIR/pg" || true); do
    kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 2
  find "$LOG_DIR" -mindepth 1 -maxdepth 1 -type d -name 'pg*' -exec rm -rf {} + 2>/dev/null || true
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
  # Every path below saves leftover work with back_to_main. If changes are still here,
  # saving failed: stop rather than carry them into the next issue's branch, and never
  # reset the tree automatically.
  if [ -n "$(git status --porcelain)" ]; then
    log "Working tree has uncommitted changes. Stopping. Inspect with: git status"
    break
  fi
  clean_scratch_db

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
  if ! run_agent scripts/prompts/implement.md "$issue"; then
    gh issue edit "$issue" --remove-label "in-progress,attempt-$((a + 1))" --add-label ready
    log "Stop file found while waiting out a usage limit. #$issue is back to ready."
    back_to_main "$issue" "loop stopped while waiting out a usage limit"
    break
  fi

  pr=$(gh pr list --head "issue-$issue" --state open --json number -q '.[0].number')
  if [ -z "$pr" ]; then
    if has_label "$issue" ready && ! deps_done "$issue"; then
      # The agent found an unlisted blocker, added it under "Depends on" and put the issue
      # back to ready. That run doesn't count as an attempt.
      gh issue edit "$issue" --remove-label "in-progress,attempt-$((a + 1))"
      log "#$issue waits for a blocker the agent added. Back to ready."
      back_to_main "$issue" "the issue waits for a blocker the agent added" || break
      continue
    fi
    gh issue edit "$issue" --remove-label ready 2>/dev/null || true
    if ! has_label "$issue" needs-human && ! has_label "$issue" design-change; then
      gh issue edit "$issue" --add-label needs-human
      gh issue comment "$issue" --body "Loop: agent finished without opening a PR. See local logs."
    fi
    gh issue edit "$issue" --remove-label in-progress
    back_to_main "$issue" "the agent finished without opening a PR" || break
    continue
  fi

  gh pr merge "$pr" --auto --squash --delete-branch
  log "PR #$pr opened, auto-merge enabled. Waiting for checks."
  back_to_main "$issue" "work left over after opening PR #$pr" || break
  sleep 30   # give GitHub time to register the checks
  wait_checks "$pr"
  failed=$(failing_checks "$pr")

  if [ "$failed" = "merge-policy" ]; then
    gh issue edit "$issue" --remove-label in-progress --add-label awaiting-review
    log "#$issue touches critical paths. Waiting for your review on PR #$pr."
    continue
  fi

  if [ -n "$failed" ]; then
    log "Checks failed on PR #$pr: $(echo "$failed" | tr '\n' ' '). One fix attempt."
    git checkout -q "issue-$issue"
    if ! run_agent scripts/prompts/fix.md "$issue" "$pr"; then
      log "Stop file found while waiting out a usage limit during the fix of PR #$pr."
      back_to_main "$issue" "loop stopped during the fix attempt on PR #$pr"
      break
    fi
    back_to_main "$issue" "work left over after the fix attempt on PR #$pr" || break
    sleep 20
    wait_checks "$pr"
    failed=$(failing_checks "$pr")
    if [ "$failed" = "merge-policy" ]; then
      gh issue edit "$issue" --remove-label in-progress --add-label awaiting-review
      continue
    fi
    if [ -n "$failed" ]; then
      gh issue edit "$issue" --remove-label in-progress --add-label needs-human
      gh issue comment "$issue" --body "Loop: checks still failing on PR #$pr after a fix attempt: $failed"
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
done
log "Loop finished. Issues merged: $done_count"
