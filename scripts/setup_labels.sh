#!/usr/bin/env bash
set -euo pipefail
create() { gh label create "$1" --color "$2" --description "$3" --force; }
create ready             0E8A16 "Ready for the loop"
create in-progress       FBCA04 "Being implemented by the loop"
create attempt-1         C5DEF5 "First attempt"
create attempt-2         C5DEF5 "Second attempt"
create awaiting-review   D93F0B "Critical PR waiting for human review"
create needs-human       B60205 "Agent stopped and needs a human"
create design-change     5319E7 "Design must change before implementation"
create deploy-failed     B60205 "Failed verification on the VM and was rolled back"
create eval-regression   B60205 "Nightly evals below threshold"
create approved-by-human 0E8A16 "Human reviewed this critical change"
