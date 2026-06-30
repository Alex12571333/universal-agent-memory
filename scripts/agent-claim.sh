#!/usr/bin/env bash
set -euo pipefail

issue="${1:?usage: agent-claim.sh ISSUE SLUG}"
slug="${2:?usage: agent-claim.sh ISSUE SLUG}"
repo="${UAM_GITHUB_REPO:-Alex12571333/universal-agent-memory}"
login="$(gh api user --jq .login)"
state="$(gh issue view "$issue" --repo "$repo" --json state --jq .state)"
assignees="$(gh issue view "$issue" --repo "$repo" --json assignees --jq '.assignees[].login')"

if [[ "$state" != "OPEN" ]]; then
  echo "Issue #$issue is not open." >&2
  exit 1
fi
if [[ -n "$assignees" && "$assignees" != "$login" ]]; then
  echo "Issue #$issue is already claimed by: $assignees" >&2
  exit 1
fi

gh label create "status:available" --repo "$repo" --color 2DA44E --force
gh label create "status:in-progress" --repo "$repo" --color FBCA04 --force
gh issue edit "$issue" \
  --repo "$repo" \
  --add-assignee "@me" \
  --remove-label "status:available" \
  --add-label "status:in-progress"

git fetch origin main
git switch -c "agent/${issue}-${slug}" origin/main
gh issue comment "$issue" --repo "$repo" --body "Claimed by @$login on branch \`agent/${issue}-${slug}\`."
