#!/usr/bin/env bash
# Comments deploy results on the merged PR and its issue. Only a verification failure that
# this commit alone can explain re-queues the issue; a green deploy clears deploy-failed from
# every issue it contains. deploy.log exists whenever the deploy step ran, even if SSH failed,
# so the lines deploy.sh prints decide what happened, not the file.
set -euo pipefail
status="$1"
repo="$GITHUB_REPOSITORY"
run_url="$GITHUB_SERVER_URL/$repo/actions/runs/$GITHUB_RUN_ID"

pr_of() { gh api "repos/$repo/commits/$1/pulls" --jq '.[0].number // empty'; }

issue_of() {
  gh pr view "$1" --json body -q .body | grep -oiE 'closes #[0-9]+' | head -1 | grep -oE '[0-9]+' || true
}

# The last green SHA deploy.sh printed for this deploy. Empty when it printed none.
last_green() {
  sed -n "s/^Deploying $SHA (last green: \([0-9a-f]\{40\}\))$/\1/p" deploy.log 2>/dev/null | tail -n 1 || true
}

# Best effort: a reporting problem must not turn a green deploy red. A skipped deploy
# (DEPLOY_FREEZE) also ends green, but verified nothing, so it clears nothing.
clear_deploy_failed() {
  grep -qF "Deploy $SHA verified." deploy.log 2>/dev/null || return 0
  local base commits sha pr issue
  base=$(last_green)
  commits="$SHA"
  if [ -n "$base" ]; then
    commits+=$'\n'$(gh api --paginate "repos/$repo/compare/$base...$SHA?per_page=100" --jq '.commits[].sha' || true)
  fi
  for sha in $(printf '%s\n' "$commits" | sort -u); do
    pr=$(pr_of "$sha" || true)
    [ -n "$pr" ] || continue
    issue=$(issue_of "$pr")
    [ -n "$issue" ] || continue
    gh issue edit "$issue" --remove-label deploy-failed || echo "Could not clear deploy-failed from #$issue."
  done
}

if [ "$status" = "success" ]; then
  clear_deploy_failed
fi

pr=$(pr_of "$SHA")
[ -z "$pr" ] && { echo "No PR found for $SHA (direct push or manual run)."; exit 0; }
issue=$(issue_of "$pr")

comment() {
  gh pr comment "$pr" --body "$1"
  [ -z "$issue" ] || gh issue comment "$issue" --body "$1"
}

if [ "$status" = "success" ]; then
  gh pr comment "$pr" --body "Deployed and verified on the VM (smoke + scenario passed). [Run]($run_url)"
  [ -n "$issue" ] && gh issue comment "$issue" --body "Deployed and verified via PR #$pr."
  exit 0
fi

# These two comments carry no log: without deploy.sh's output the log is SSH's, which can
# name the VM host, and this repo is public. The run page masks it.
if ! grep -qF "Deploying $SHA" deploy.log 2>/dev/null; then
  comment "Deploy of PR #$pr didn't reach the VM, or stopped before changing anything. Nothing was rolled back and the issue isn't re-queued. [Run]($run_url)"
  exit 0
fi
if ! grep -qF "Verification failed for $SHA." deploy.log; then
  comment "Deploy of PR #$pr failed, but not at verification, so the issue isn't re-queued. Check the VM. [Run]($run_url)"
  exit 0
fi

tail_log=$(tail -n 60 deploy.log)
base=$(last_green)
new_commits=""
if [ -n "$base" ]; then
  new_commits=$(gh api "repos/$repo/compare/$base...$SHA" --jq '.total_commits' || true)
fi

# Exactly one commit since the last green one means this commit alone can explain the failure.
# Anything else (more commits, or a range we can't read) may be inherited: tell a human.
if [ "$new_commits" != "1" ]; then
  # shellcheck disable=SC2016  # backticks are a literal Markdown code fence
  body=$(printf 'Deploy of PR #%s failed verification and was rolled back, but this commit is not the only one since the last green deploy, so the failure may be inherited. The issue is not re-queued. [Run](%s)\n\n```\n%s\n```' "$pr" "$run_url" "$tail_log")
  comment "$body"
  [ -z "$issue" ] || gh issue edit "$issue" --add-label deploy-failed
  exit 0
fi

# shellcheck disable=SC2016  # backticks are a literal Markdown code fence
body=$(printf 'Deploy of PR #%s failed verification and was rolled back. [Run](%s)\n\n```\n%s\n```' "$pr" "$run_url" "$tail_log")
gh pr comment "$pr" --body "$body"
if [ -n "$issue" ]; then
  gh issue reopen "$issue"
  gh issue comment "$issue" --body "$body"
  gh issue edit "$issue" --add-label "ready,deploy-failed"
fi
