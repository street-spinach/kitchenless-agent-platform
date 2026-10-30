CI failed on PR #{{PR}} for issue #{{ISSUE}}. You are on branch `issue-{{ISSUE}}`, running
unattended.

1. See which checks failed: `gh pr checks {{PR}}`.
2. Read the failure logs: `gh run list --branch issue-{{ISSUE}} --limit 5`, then
   `gh run view <run-id> --log-failed`.
3. Fix the code. Change a test only if the test contradicts the design, and say so in the
   commit message. Never weaken, skip or delete tests or eval cases, and never edit
   evals/thresholds.yaml.
4. Re-run the issue's Verify commands and `make lint test-unit` locally until they pass.
5. Commit and push to the same branch. Do not open a new PR. Do not merge.

If you can't fix it, comment on issue #{{ISSUE}} with what you found, run
`gh issue edit {{ISSUE}} --add-label needs-human`, and stop.
