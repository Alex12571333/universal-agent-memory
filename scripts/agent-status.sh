#!/usr/bin/env bash
set -euo pipefail

repo="${UAM_GITHUB_REPO:-Alex12571333/universal-agent-memory}"

echo "ACTIVE WORK"
gh issue list \
  --repo "$repo" \
  --state open \
  --label "status:in-progress" \
  --json number,title,assignees,labels,updatedAt \
  --template '{{range .}}#{{.number}} {{.title}} | {{range .assignees}}{{.login}} {{end}}| {{.updatedAt}}{{"\n"}}{{else}}none{{"\n"}}{{end}}'

echo
echo "OPEN PULL REQUESTS"
gh pr list \
  --repo "$repo" \
  --state open \
  --json number,title,headRefName,author,isDraft,statusCheckRollup \
  --template '{{range .}}#{{.number}} {{.title}} | {{.author.login}} | {{.headRefName}} | draft={{.isDraft}}{{"\n"}}{{else}}none{{"\n"}}{{end}}'

echo
echo "AVAILABLE WORK"
gh issue list \
  --repo "$repo" \
  --state open \
  --label "status:available" \
  --json number,title,labels \
  --template '{{range .}}#{{.number}} {{.title}}{{"\n"}}{{else}}none{{"\n"}}{{end}}'
