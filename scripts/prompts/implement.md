You are implementing GitHub issue #{{ISSUE}} in this repository. You are running unattended:
nobody will answer questions during this run.

1. Read the issue: `gh issue view {{ISSUE}} --comments`.
2. Read CLAUDE.md and every design section listed under "Implements". The design is the
   source of truth. If an earlier attempt left comments on the issue, read them.
3. You are already on branch `issue-{{ISSUE}}`, created from the latest main.
4. Implement exactly what the issue and design specify. Keep the change small. Do not
   refactor unrelated code or add features that aren't asked for.
5. Add or update the tests listed under "Tests" and the eval cases listed under "Evals".
6. Run every command under "Verify", plus `make lint test-unit`. Iterate until they pass.
   If the change touches services/agents/, also run `make eval-fast`.
7. Never weaken, skip or delete existing tests or eval cases, and never change
   evals/thresholds.yaml, unless the issue explicitly asks for it.
8. Commit with a message like `feat: <summary> (#{{ISSUE}})`, push with
   `git push -u origin issue-{{ISSUE}}`, and open a PR:
   `gh pr create --base main --head issue-{{ISSUE}} --title "<summary> (#{{ISSUE}})" --body "<body>"`
   The body must start with `Closes #{{ISSUE}}`, then summarize what changed and how you
   verified it (commands run and results).
9. Do not merge the PR. Do not add labels to the PR.

Stop conditions. In these cases, do NOT open a PR. Comment on the issue explaining
precisely what you found, add the label, and stop:
- The design is ambiguous, or the issue contradicts the design:
  `gh issue edit {{ISSUE}} --add-label needs-human`
- Implementing it correctly requires changing the design:
  `gh issue edit {{ISSUE}} --add-label design-change`
- You cannot make Verify pass after reasonable effort:
  `gh issue edit {{ISSUE}} --add-label needs-human`
