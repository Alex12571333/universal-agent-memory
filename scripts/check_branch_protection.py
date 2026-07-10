"""Verify GitHub branch protection for release branches."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.error
import urllib.request
from typing import Any

from memory_plane.config.secrets import read_secret_env

API_VERSION = "2022-11-28"
DEFAULT_REQUIRED_CHECKS = ("python", "web")


def main() -> int:
    """Check that a GitHub branch blocks direct production pushes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=_repo_from_git(), help="GitHub repo as owner/name")
    parser.add_argument("--branch", default="main", help="Branch to verify")
    parser.add_argument(
        "--token",
        default=read_secret_env("GITHUB_TOKEN", "GH_TOKEN"),
        help="GitHub token; defaults to GITHUB_TOKEN/GH_TOKEN",
    )
    parser.add_argument(
        "--required-check",
        action="append",
        dest="required_checks",
        default=[],
        help="Required status check context/job name; repeat for multiple checks",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    args = parser.parse_args()

    required_checks = tuple(args.required_checks or DEFAULT_REQUIRED_CHECKS)
    if not args.repo:
        parser.error("repo is required; pass --repo owner/name or configure origin remote")
    if not args.token:
        parser.error("GitHub token is required for branch-protection verification")

    try:
        protection = _fetch_branch_protection(args.repo, args.branch, args.token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _finish(
                False,
                "branch protection is not enabled or token lacks access",
                json_output=args.json,
            )
        raise
    except urllib.error.URLError as exc:
        return _finish(
            False,
            f"unable to reach GitHub API: {exc.reason}",
            json_output=args.json,
        )

    checks = _evaluate(protection, required_checks=required_checks)
    passed = all(item["passed"] for item in checks)
    result = {
        "repo": args.repo,
        "branch": args.branch,
        "passed": passed,
        "checks": checks,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        for item in checks:
            status = "PASS" if item["passed"] else "FAIL"
            print(f"{status} {item['name']}: {item['detail']}")
        print("branch_protection=PASS" if passed else "branch_protection=FAIL")
    return 0 if passed else 1


def _fetch_branch_protection(repo: str, branch: str, token: str) -> dict[str, Any]:
    """Fetch classic branch-protection settings from GitHub REST API."""
    url = f"https://api.github.com/repos/{repo}/branches/{branch}/protection"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "obelisk-memory-branch-protection-check",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("GitHub branch protection response is not an object")
    return data


def _evaluate(
    protection: dict[str, Any],
    *,
    required_checks: tuple[str, ...],
) -> list[dict[str, object]]:
    """Return named release-gate checks for one branch protection document."""
    pull_reviews = protection.get("required_pull_request_reviews")
    status_checks = protection.get("required_status_checks") or {}
    enforce_admins = protection.get("enforce_admins") or {}
    contexts = set(status_checks.get("contexts") or [])
    checks = status_checks.get("checks") or []
    contexts.update(str(item.get("context")) for item in checks if item.get("context"))
    missing_checks = [name for name in required_checks if name not in contexts]
    return [
        {
            "name": "pull-request-required",
            "passed": isinstance(pull_reviews, dict),
            "detail": "required_pull_request_reviews configured",
        },
        {
            "name": "status-checks-required",
            "passed": isinstance(status_checks, dict) and not missing_checks,
            "detail": (
                "required checks present: " + ", ".join(required_checks)
                if not missing_checks
                else "missing required checks: " + ", ".join(missing_checks)
            ),
        },
        {
            "name": "strict-status-checks",
            "passed": bool(status_checks.get("strict")),
            "detail": "branch must be up to date before merge",
        },
        {
            "name": "admins-enforced",
            "passed": bool(enforce_admins.get("enabled")),
            "detail": "admins cannot bypass direct-push protection",
        },
    ]


def _repo_from_git() -> str | None:
    """Infer owner/name from git remote origin when possible."""
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    remote = result.stdout.strip()
    patterns = (
        r"github\.com[:/](?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.search(pattern, remote)
        if match:
            return match.group("repo")
    return None


def _finish(passed: bool, detail: str, *, json_output: bool) -> int:
    """Print a minimal failure result for fetch-level errors."""
    result = {"passed": passed, "error": detail}
    if json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(("branch_protection=PASS " if passed else "branch_protection=FAIL ") + detail)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
