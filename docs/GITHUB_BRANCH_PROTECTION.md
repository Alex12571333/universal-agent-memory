# GitHub branch protection

Obelisk Memory is not a production release process if `main` can be pushed
directly. The repository must require pull requests and green checks before a
merge.

## Required `main` rules

Configure `Settings → Rules → Rulesets` or `Settings → Branches` for `main`:

- require a pull request before merging;
- require status checks before merging;
- require the `python` and `web` CI jobs;
- require branches to be up to date before merging;
- do not allow administrators or bypass actors to skip the rule in production;
- block force pushes and deletions.

## Verification

Run the repository verifier with a token that can read branch protection:

```bash
GITHUB_TOKEN=... python scripts/check_branch_protection.py \
  --repo Alex12571333/universal-agent-memory \
  --branch main \
  --required-check python \
  --required-check web
```

Expected result:

```text
PASS pull-request-required: required_pull_request_reviews configured
PASS status-checks-required: required checks present: python, web
PASS strict-status-checks: branch must be up to date before merge
PASS admins-enforced: admins cannot bypass direct-push protection
branch_protection=PASS
```

If GitHub still prints `Bypassed rule violations` during `git push`, the
production release gate is not closed. That warning means a bypass actor can
still mutate `main` without the PR path.

## Current interpretation

The repo contains the verifier and release checklist hook. The actual GitHub
setting must still be enabled and verified with a valid GitHub token before
claiming full production readiness.
