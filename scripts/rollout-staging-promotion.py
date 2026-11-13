#!/usr/bin/env python3
"""Controlled deployment-automation inventory and PR-only rollout (#7319).

DRY_RUN examines backend candidates. APPLY creates a staging PR, waits for
its standard-rule merge, and only then creates/merges a main PR. No bypass.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILES = {
    "staging-deploy/auto-staging-deploy.cjs": ".github/scripts/auto-staging-deploy.cjs",
    "staging-deploy/auto-staging-pr.cjs": ".github/scripts/auto-staging-pr.cjs",
    "staging-deploy/auto-staging-deploy.test.cjs": ".github/scripts/auto-staging-deploy.test.cjs",
    "staging-deploy/auto-staging-pr.test.cjs": ".github/scripts/auto-staging-pr.test.cjs",
    "staging-deploy/auto-staging-deploy.yml": ".github/workflows/auto-staging-deploy.yml",
}
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
    return "CANDIDATE main/staging/cicd.yml present", repo


def stage_payload(root):
    changed = []
    for src, target in SOURCE_FILES.items():
        desired = (ROOT / src).read_bytes()
        destination = root / target
        if destination.is_symlink() or any(
            parent.is_symlink() for parent in destination.parents
            if parent != root and root in parent.parents
        ):
            raise ValueError(f"Refusing symlink destination: {target}")
        if not destination.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Destination leaves checkout: {target}")
        if destination.exists() and destination.read_bytes() == desired:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(desired)
        changed.append(target)
    return changed


def pr_for_branch(full, branch):
    result = cmd("gh", "pr", "list", "--repo", full, "--head", branch,
                 "--state", "all", "--json", "number,url,state,baseRefName,mergedAt")
    found = json.loads(result)
    if len(found) > 1:
        raise RuntimeError(f"Ambiguous prior PRs for {branch}")
    return found[0] if found else None


def verify_pr(full, pr, target):
    if pr["baseRefName"] != target:
        raise RuntimeError(f"Existing PR targets {pr['baseRefName']}, expected {target}")
    if pr["state"] == "CLOSED" and not pr.get("mergedAt"):
        raise RuntimeError(f"Previous {target} PR was closed without merge: {pr['url']}")
    return bool(pr.get("mergedAt"))


def merge_regular_pr(full, number, target, sha):
    # Never use --admin, force, or a ruleset/required-check bypass.
    cmd("gh", "pr", "merge", str(number), "--repo", full, "--merge",
        "--match-head-commit", sha)
    pr = json.loads(cmd("gh", "pr", "view", str(number), "--repo", full,
                        "--json", "mergedAt,mergeCommit,baseRefName"))
    if not pr.get("mergedAt") or pr.get("baseRefName") != target:
        raise RuntimeError(f"{full}: {target} PR merge not confirmed")
    return pr["mergeCommit"]["oid"]


def install_phase(full, name, target, issue):
    with tempfile.TemporaryDirectory(prefix="staging-rollout-") as directory:
        cmd("gh", "repo", "clone", full, directory, "--", "--depth=1",
            "--branch", target)
        root = Path(directory)
        changed = stage_payload(root)
        if not changed:
            print(f"{name} {target}: NO_CHANGE", flush=True)
            return "current"
        base_sha = cmd("git", "rev-parse", "HEAD", cwd=root)
        # Use the same org-action branch family as the existing guideline sync:
        # a target-specific branch, based on the destination base and payload.
        import hashlib
        payload_hash = hashlib.sha256(b"".join(
            (ROOT / src).read_bytes() for src in SOURCE_FILES)).hexdigest()[:10]
        branch = f"org-action/staging-promotion-{target}-{issue}-{base_sha[:10]}-{payload_hash}"
        prior = pr_for_branch(full, branch)
        if prior:
            if verify_pr(full, prior, target):
                print(f"{name} {target}: ALREADY_MERGED {prior['url']}", flush=True)
                return "merged"
            print(f"{name} {target}: RESUME {prior['url']}", flush=True)
            pr_number = prior["number"]
        else:
            if cmd("git", "ls-remote", "--heads", "origin", branch, cwd=root):
                raise RuntimeError(f"{name}: orphaned rollout branch {branch}")
            cmd("git", "checkout", "-b", branch, cwd=root)
            cmd("git", "add", "--", *changed, cwd=root)
            cmd("git", "-c", "user.name=eshopbox-org-actions[bot]",
                "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit", "-m", f"feat: enable staging promotion automation (#{issue})", cwd=root)
            cmd("git", "push", "-u", "origin", branch, cwd=root)
            body = (
                f"Central Backlog: https://github.com/{full.split('/')[0]}/Central-Backlog/issues/{issue}\\n\\n"
                "One atomic staging-promotion rollout change, containing only the "
                "workflow and accompanying scripts/tests.\\n\\n"
                "Merge uses regular GitHub branch protection; no admin bypass.\\n\\n"
                f"Files: {', '.join(changed)}"
            )
            url = cmd("gh", "pr", "create", "--repo", full, "--base", target,
                      "--head", branch,
                      "--title", f"Feature to {'Staging' if target == 'staging' else 'Main'}: Add staging promotion automation (#{issue})",
                      "--body", body, cwd=root)
            pr = pr_for_branch(full, branch)
            if not pr or pr["url"] != url:
                raise RuntimeError(f"{name}: cannot verify created PR {url}")
            pr_number = pr["number"]
            print(f"{name} {target}: CREATED {url}", flush=True)
        # The PR may have checks pending. Use normal merge, and stop if
        # GitHub rejects it. Do not create a main PR before staging merges.
        current_head = json.loads(cmd("gh", "pr", "view", str(pr_number),
                                     "--repo", full, "--json", "headRefOid"))
        sha = current_head["headRefOid"]
        merge_regular_pr(full, pr_number, target, sha)
        print(f"{name} {target}: MERGED PR #{pr_number}", flush=True)
        return "merged"


def install(full, name, issue):
    # One open PR per repository at a time: staging first, then main.
    install_phase(full, name, "staging", issue)
    return install_phase(full, name, "main", issue)


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
    print(f"Operation={operation} scanned={len(names)} backend-only sequential rollout", flush=True)
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
                install(f"{org}/{name}", name, issue)
        except Exception as exc:
            blocked += 1
            print(f"{name}: BLOCK {exc}", flush=True)
    print(f"Rollout complete. Blocked={blocked}; no application deployments triggered.", flush=True)
    if operation == "APPLY" and blocked:
        raise SystemExit("APPLY incomplete: one or more repositories blocked")


if __name__ == "__main__":
    main()
