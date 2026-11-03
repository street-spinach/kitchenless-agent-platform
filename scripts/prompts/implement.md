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
   If the change touches services/agents/, also run `make eval-safety`.
   Exception: where a Verify step is `make eval-fast`, run `make eval-safety` instead, plus
   each eval case this issue adds or changes, once:
   `uv run python -m evals.run --case <id> --repeats 1`. Say so in the PR body. The full
   suite runs nightly (architecture item 63).
   Run every check in the foreground and wait for it to finish, even a long one such as an
   eval run. Never end your turn while a command is still running in the background: the
   run ends when you stop, so a PR you meant to open after it is never opened.
7. Never weaken, skip or delete existing tests or eval cases, and never change
   evals/thresholds.yaml, unless the issue explicitly asks for it.
8. Commit with a message like `feat: <summary> (#{{ISSUE}})`, push with
   `git push -u origin issue-{{ISSUE}}`, and open a PR:
   `gh pr create --base main --head issue-{{ISSUE}} --title "<summary> (#{{ISSUE}})" --body "<body>"`
   The body must start with `Closes #{{ISSUE}}`, then summarize what changed and how you
   verified it (commands run and results), then the `## Decisions` section (below) if you
   made any.
9. Do not merge the PR. Do not add labels to the PR.

## Small gaps in the design: decide, record, continue

When the design or issue leaves a detail open (a reason code, a field name, a default, a
message id, a test file name, which of two readings applies, how a mock behaves), decide it
yourself and keep going. Choose, in this order of preference:

1. what another approved design, or code already on main, already does for the same thing;
2. the option that fails closed (refuses, raises, or does nothing) rather than open;
3. the smallest change that satisfies the design's stated goal and tests.

A decision must keep every CLAUDE.md rule, above all the safety rules. Record each one in
the PR body under `## Decisions`, as one line: what was open, what you chose, the
alternative, and why. Post the same list as a comment on issue #{{ISSUE}}. Shared
identifiers still come only from `services/common/ids.py`; if you need a new helper, add it
there.

## A blocker that isn't listed

If the issue needs code that another open issue owns (for example a module in that issue's
scope that doesn't exist yet), don't write it yourself and don't stop for a human:

1. add `#<blocker>` on its own line under `## Depends on` in the issue body
   (`gh issue view {{ISSUE}} --json body -q .body`, edit, then
   `gh issue edit {{ISSUE}} --body-file -`);
2. comment on the issue naming the blocker and what you need from it;
3. run `gh issue edit {{ISSUE}} --add-label ready`, and stop without opening a PR.

The loop then waits for the blocker to close before trying again.

## Verify steps you can't run here

If a Verify step needs something this environment doesn't have (the Compose stack, a
`.env` file, a Postgres database, a VM), run everything else, open the PR anyway, and list
in the PR body exactly which steps were not run and why. CI runs the database tests. Never
create or edit `.env` files to make a step run.

## Hard stops

Only in these cases, do NOT open a PR. Comment on the issue explaining precisely what you
found, add the label, and stop:
- Every reasonable option breaks a CLAUDE.md safety rule or an existing test:
  `gh issue edit {{ISSUE}} --add-label needs-human`
- The fix needs a change to `docs/designs/`, `evals/thresholds.yaml`, or a safety eval case
  the issue doesn't list, or the design is plainly wrong (not just incomplete):
  `gh issue edit {{ISSUE}} --add-label design-change`
- The change would need secrets or `.env` contents:
  `gh issue edit {{ISSUE}} --add-label needs-human`
- You cannot make the runnable Verify steps pass after reasonable effort:
  `gh issue edit {{ISSUE}} --add-label needs-human`
