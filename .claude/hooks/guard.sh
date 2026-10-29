#!/usr/bin/env bash
# PreToolUse guard for Bash. Exit code 2 blocks the command; stderr is shown to Claude.
set -uo pipefail

cmd=$(jq -r '.tool_input.command // ""')
branch=$(git -C "${CLAUDE_PROJECT_DIR:-.}" branch --show-current 2>/dev/null || echo "")

block() { echo "Blocked by .claude/hooks/guard.sh: $1" >&2; exit 2; }

# Patterns live in variables so bash regex quoting stays simple.
re_git_write='git[[:space:]]+(-C[[:space:]]+[^[:space:]]+[[:space:]]+)?(push|commit)([[:space:]]|$)'
re_push_main='git[[:space:]]+push[^;&|]*[[:space:]](origin[[:space:]]+)?(HEAD:)?(main|master)([[:space:]]|$)'
re_force='git[[:space:]]+push[^;&|]*(--force|[[:space:]]-f([[:space:]]|$))'
re_merge='gh[[:space:]]+pr[[:space:]]+merge'
re_env_file='(^|[[:space:]/="])\.env($|[[:space:]/"]|\.local)'
re_env_dump='(^|[;&|[:space:]])printenv([[:space:]]|$)|(^|[;&|][[:space:]]*)env[[:space:]]*($|[|;&])|/proc/[^[:space:]]*/environ'

# Never write to main.
if [[ "$cmd" =~ $re_git_write ]] && [[ "$branch" == "main" || "$branch" == "master" ]]; then
  block "you are on $branch. Work on the issue-<n> branch."
fi
if [[ "$cmd" =~ $re_push_main ]]; then
  block "never push to main. Open a PR from the issue branch."
fi

# No force pushes, no skipping hooks.
if [[ "$cmd" =~ $re_force ]]; then
  block "force pushes are not allowed."
fi
if [[ "$cmd" == *"--no-verify"* ]]; then
  block "do not skip git hooks."
fi

# Only a human may approve critical changes or merge.
if [[ "$cmd" == *"approved-by-human"* ]]; then
  block "only a human may use the approved-by-human label."
fi
if [[ "$cmd" =~ $re_merge ]]; then
  block "merging is handled by auto-merge, not by the agent."
fi

# No secrets.
if [[ "$cmd" =~ $re_env_file ]]; then
  block "do not read or write .env files. Use .env.example and make targets."
fi
if [[ "$cmd" =~ $re_env_dump ]]; then
  block "do not dump environment variables."
fi

exit 0
