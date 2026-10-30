#!/usr/bin/env bash
# Comments deploy results on the merged PR and its issue. Reopens the issue on failure.
set -euo pipefail
status="$1"
repo="$GITHUB_REPOSITORY"
run_url="$GITHUB_SERVER_URL/$repo/actions/runs/$GITHUB_RUN_ID"

pr=$(gh api "repos/$repo/commits/$SHA/pulls" --jq '.[0].number // empty')
[ -z "$pr" ] && { echo "No PR found for $SHA (direct push or manual run)."; exit 0; }
issue=$(gh pr view "$pr" --json body -q .body | grep -oiE 'closes #[0-9]+' | head -1 | grep -oE '[0-9]+' || true)

if [ "$status" = "success" ]; then
  gh pr comment "$pr" --body "Deployed and verified on the VM (smoke + scenario passed). [Run]($run_url)"
  [ -n "$issue" ] && gh issue comment "$issue" --body "Deployed and verified via PR #$pr."
  exit 0
fi

tail_log=$(tail -n 60 deploy.log 2>/dev/null || echo "No log captured.")
# shellcheck disable=SC2016  # backticks are a literal Markdown code fence
body=$(printf 'Deploy of PR #%s failed verification and was rolled back. [Run](%s)\n\n```\n%s\n```' "$pr" "$run_url" "$tail_log")
gh pr comment "$pr" --body "$body"
if [ -n "$issue" ]; then
  gh issue reopen "$issue"
  gh issue comment "$issue" --body "$body"
  gh issue edit "$issue" --add-label "ready,deploy-failed"
fi
