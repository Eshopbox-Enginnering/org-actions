#!/usr/bin/env python3
"""Controlled deployment-automation inventory and PR-only rollout (#7319).

DRY_RUN examines candidate repos. APPLY may open main PRs only for an explicit,
reviewed allowlist. It NEVER merges, tags, dispatches or modifies staging.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "staging-deploy/approved-repositories.json"
CALLER = """name: Deploy PR to staging

on:
  issue_comment:
    types: [created]

permissions:
  contents: write
  pull-requests: write
  issues: write
  actions: write
  checks: read
  statuses: read

jobs:
  deploy:
    if: github.event.issue.pull_request && github.event.comment.body == '/deploy-staging'
    uses: Eshopbox-Enginnering/org-actions/.github/workflows/reusable-backend-staging-promotion.yml@main
    secrets: inherit
"""
SOURCE_FILES = {}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def cmd(*args, cwd=None, check=True):
    p = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(args[:4])}: {p.stderr.strip()[:400]}")
    return p.stdout.strip() if check else p


def gh_api(path):
    result = cmd("gh", "api", path)
    return json.loads(result)


def named_repositories(target, single, selected, org):
    if target == "single":
        names = [single]
    elif target == "selected":
        names = [n.strip() for n in selected.split(",") if n.strip()]
        if len(names) > 25:
            raise ValueError("selected is limited to 25 repositories")
    elif target in ("backend", "all"):
        items = json.loads(cmd("gh", "repo", "list", org, "--limit", "1000",
                               "--json", "name,isArchived"))
        names = [i["name"] for i in items if not i["isArchived"]]
    else:
        raise ValueError("Invalid target")
    if not names or len(set(names)) != len(names) or any(
        not SAFE_NAME.fullmatch(n) for n in names
    ):
        raise ValueError("Invalid, empty or duplicated repository name")
    return names


def detect_backend(org, name):
    """Match the shared guideline classifier; unknown and frontend fail closed.

    Fetch metadata rather than inferring frontend/backend from the repo name.
    For Node apps, inspect package.json dependencies instead of installing
    frontend workflows by accident.
    """
    full = f"{org}/{name}"
    entries = gh_api(f"repos/{full}/contents")
    if not isinstance(entries, list):
        raise ValueError("Cannot inspect repository root")
    filenames = {entry.get("name") for entry in entries}
    if "composer.json" in filenames:
        return True
    frontend = {"angular.json", "vite.config.js", "vite.config.ts",
                "vite.config.mjs", "vue.config.js", "next.config.js",
                "next.config.ts", "next.config.mjs"}
    backend = {"pom.xml", "build.gradle", "build.gradle.kts", "go.mod",
               "requirements.txt", "pyproject.toml"}
    if filenames & frontend:
        return False
    if filenames & backend:
        return True
    if "package.json" in filenames:
        package = cmd("gh", "api", f"repos/{full}/contents/package.json",
                      "-H", "Accept: application/vnd.github.raw+json")
        if any(x in package for x in ('"express"', '"fastify"', '"koa"',
                                     '"nestjs"', '"@nestjs/core"')):
            return True
    return False


def check_repo(org, name):
    full = f"{org}/{name}"
    repo = gh_api(f"repos/{full}")
    if repo.get("archived") or repo.get("disabled"):
        return "SKIP archived/disabled", None
    if not detect_backend(org, name):
        return "SKIP frontend/unknown repository", None
    if repo.get("default_branch") != "main":
        return "BLOCK default branch is not main", None
    staging = cmd("gh", "api", f"repos/{full}/branches/staging", check=False)
    if staging.returncode:
        return "BLOCK no staging branch", None
    workflow = cmd("gh", "api", f"repos/{full}/contents/.github/workflows/cicd.yml",
                   "-H", "Accept: application/vnd.github.raw+json", check=False)
    if workflow.returncode:
        return "BLOCK no cicd.yml", None
    if "workflow_dispatch:" not in workflow.stdout or "dev-*" not in workflow.stdout:
        return "BLOCK cicd.yml lacks workflow_dispatch or dev-* tag trigger", None
    # Other checks (required workflow identities, branch rules, GitHub App
    # permissions) must be manually reviewed before entry in the allowlist.
    return "CANDIDATE main/staging/cicd.yml present", repo


def install(full, name, base, issue):
    with tempfile.TemporaryDirectory(prefix="staging-rollout-") as directory:
        cmd("gh", "repo", "clone", full, directory, "--", "--depth=1", "--branch", base)
        root = Path(directory)
        changed = []
        for target, desired in {'.github/workflows/auto-staging-deploy.yml': CALLER.encode()}.items():
            destination = root / target
            if destination.is_symlink():
                raise ValueError(f"Refusing symlink destination: {target}")
            if destination.exists() and destination.read_bytes() == desired:
                continue
            # A parent path may itself be a symlink. Never follow it outside
            # the clean target checkout during installer writes.
            if any(parent.is_symlink() for parent in destination.parents if parent != root and root in parent.parents):
                raise ValueError(f"Refusing symlink parent: {target}")
            if not destination.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Destination leaves checkout: {target}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(desired)
            changed.append(target)
        if not changed:
            print(f"{name}: NO_CHANGE canonical automation already installed", flush=True)
            return
        # Stable branch based on exact changes against this default branch.
        base_sha = cmd("git", "rev-parse", "HEAD", cwd=root)
        branch = f"org-action/staging-promotion-{issue}-{base_sha[:12]}"
        existing = cmd("gh", "pr", "list", "--repo", full, "--head", branch,
                       "--state", "all", "--json", "number,url,state", check=False)
        if existing.returncode:
            raise RuntimeError(f"{name}: cannot inspect existing PRs")
        prior = json.loads(existing.stdout)
        if prior:
            open_prs = [item for item in prior if item.get("state", "").upper() == "OPEN"]
            if open_prs:
                print(f"{name}: RESUME existing open rollout PR {open_prs[0]['url']}", flush=True)
                return
            raise RuntimeError(f"{name}: previous rollout PR is closed; a new reviewed branch is required")
        remote = cmd("git", "ls-remote", "--heads", "origin", branch, cwd=root)
        if remote:
            raise RuntimeError(f"{name}: orphaned rollout branch exists; review and resolve it before retry")
        cmd("git", "checkout", "-b", branch, cwd=root)
        cmd("git", "add", "--", *changed, cwd=root)
        cmd("git", "-c", "user.name=eshopbox-org-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", f"feat: stage deployment automation rollout (#{issue})", cwd=root)
        cmd("git", "push", "-u", "origin", branch, cwd=root)
        body = (
            f"Central Backlog: https://github.com/{full.split('/')[0]}/Central-Backlog/issues/{issue}\n\n"
            "Thin caller for centrally maintained /deploy-staging automation.\n\n"
            "Uses the shared GitHub workflow; no copied JavaScript or repository allowlist. "
            "No deployment requested.\n\n"
            f"Files: {', '.join(changed)}\n\n"
            "Staging companion PR must be created/merged before this main PR.\n"
        )
        url = cmd("gh", "pr", "create", "--repo", full, "--base", base, "--head", branch,
                  "--title", f"Feature to Main: Add PR-based staging promotion (#{issue})",
                  "--body", body, cwd=root)
        print(f"{name}: CREATED_MAIN_PR {url}", flush=True)


def main():
    operation = os.environ.get("OPERATION", "DRY_RUN")
    if operation not in ("DRY_RUN", "APPLY"):
        raise ValueError("Unknown operation")
    if operation == "APPLY" and os.environ.get("CONFIRMATION") != "APPLY":
        raise ValueError("APPLY requires explicit confirmation")
    org = os.environ["ORG"]
    issue = "7319"
    names = named_repositories(os.environ.get("TARGET", "single"),
                               os.environ.get("REPOSITORY", ""),
                               os.environ.get("REPOSITORIES", ""), org)
    print(f"Operation={operation} scanned={len(names)} backend-only caller rollout", flush=True)
    blocked = 0
    for name in names:
        try:
            status, _repo = check_repo(org, name)
            print(f"{name}: {status}", flush=True)
            if status != "CANDIDATE main/staging/cicd.yml present":
                if status.startswith("BLOCK"):
                    blocked += 1
                continue
            if operation == "APPLY":
                install(f"{org}/{name}", name, "main", issue)
        except Exception as exc:
            blocked += 1
            print(f"{name}: BLOCK {exc}", flush=True)
    print(f"Audit complete. Blocked={blocked}; no PRs merged or deployments triggered.", flush=True)
    if operation == "APPLY" and blocked:
        raise SystemExit("APPLY incomplete: one or more repositories blocked")


if __name__ == "__main__":
    main()
