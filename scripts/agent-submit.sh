#!/usr/bin/env bash
set -euo pipefail

issue="${1:?usage: agent-submit.sh ISSUE}"
repo="${UAM_GITHUB_REPO:-Alex12571333/universal-agent-memory}"
branch="$(git branch --show-current)"

if [[ "$branch" == "main" || "$branch" == "master" ]]; then
  echo "Refusing to submit directly from $branch." >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Commit or stash local changes before submit." >&2
  exit 1
fi

git push -u origin "$branch"
title="$(gh issue view "$issue" --repo "$repo" --json title --jq .title)"
if ! gh pr view "$branch" --repo "$repo" >/dev/null 2>&1; then
  gh pr create \
    --repo "$repo" \
    --base main \
    --head "$branch" \
    --title "$title" \
    --body "Closes #$issue"
fi
gh pr merge "$branch" --repo "$repo" --auto --squash --delete-branch
