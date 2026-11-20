#!/usr/bin/env python3
"""Install only the canonical /deploy-staging caller with verified staging-then-main App merges.

Run from the approved org-actions Actions environment with GH_TOKEN set to the
organization App token. No direct writes to protected branches.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
CALLER = (ROOT / "caller.yml").read_bytes()
DEST = ".github/workflows/auto-staging-deploy.yml"
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
ORG = "Eshopbox-Enginnering"


def cmd(*args, cwd=None):
    p = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(f"{' '.join(args)} failed ({p.returncode}): {p.stderr[-1500:]}")
    return p.stdout.strip()


def gh_json(*args):
    return json.loads(cmd("gh", *args))


def verify_merged(full, number, target, base, head, expected):
    """Confirm the exact original base/head and single-file result after admin merge."""
    pr = gh_json("pr", "view", str(number), "--repo", full,
                 "--json", "state,mergedAt,mergeCommit,baseRefName")
    merged_sha = (pr.get("mergeCommit") or {}).get("oid")
    if not pr.get("mergedAt") or not merged_sha or pr["baseRefName"] != target:
        raise RuntimeError(f"{full} PR #{number}: merge not confirmed")
    with tempfile.TemporaryDirectory(prefix="staging-caller-verify-") as temp:
        cmd("git", "init", "-q", temp)
        cmd("git", "remote", "add", "origin", f"https://github.com/{full}.git", cwd=temp)
        cmd("git", "fetch", "-q", "--no-tags", "origin", merged_sha, cwd=temp)
        parents = cmd("git", "rev-list", "--parents", "-n", "1", merged_sha, cwd=temp).split()
        if len(parents) != 3 or parents[1:] != [base, head]:
            raise RuntimeError(f"{full} PR #{number}: unexpected merge parents; stop")
        files = cmd("git", "diff", "--name-only", base, merged_sha, cwd=temp).splitlines()
        if files != [DEST]:
            raise RuntimeError(f"{full} PR #{number}: unexpected merged files {files}")
        merged = subprocess.run(["git", "show", f"{merged_sha}:{DEST}"],
                                cwd=temp, capture_output=True, check=True).stdout
        if merged != expected:
            raise RuntimeError(f"{full} PR #{number}: caller content mismatch")
    print(f"VERIFIED: {full} {target} PR #{number} merge {merged_sha}", flush=True)


def phase(full, target, issue, operation):
    """Produce exactly one file-only PR from a fresh clone of the target branch."""
    with tempfile.TemporaryDirectory(prefix="staging-caller-phase-") as tmp:
        root = Path(tmp) / "repository"
        cmd("gh", "repo", "clone", full, str(root), "--",
            "--depth=1", "--single-branch", "--branch", target)
        existing = root / DEST
        if existing.is_symlink() or (existing.exists() and not existing.is_file()):
            raise RuntimeError(f"{full}: unsafe existing caller path")
        if existing.exists() and existing.read_bytes() == CALLER:
            print(f"{full} {target}: ALREADY_CURRENT", flush=True)
            return "current"
        base = cmd("git", "rev-parse", "HEAD", cwd=root)
        old = cmd("git", "ls-files", "--stage", "--", DEST, cwd=root)
        if old and not old.startswith("100644 "):
            raise RuntimeError(f"{full}: caller must be a regular non-executable file")
        print(f"{full} {target}: {'WOULD_INSTALL' if operation == 'DRY_RUN' else 'INSTALL'} {DEST}", flush=True)
        if operation == "DRY_RUN":
            return "planned"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(CALLER)
        fingerprint = hashlib.sha256(CALLER).hexdigest()[:10]
        branch = f"org-action/install-staging-caller-{fingerprint}-{target}-{base[:10]}"
        if cmd("git", "ls-remote", "--heads", "origin", branch, cwd=root):
            raise RuntimeError(f"{full}: existing rollout branch {branch}; inspect before retrying")
        cmd("git", "checkout", "-q", "-b", branch, cwd=root)
        cmd("git", "add", "--", DEST, cwd=root)
        changed = cmd("git", "diff", "--cached", "--name-only", cwd=root).splitlines()
        if changed != [DEST]:
            raise RuntimeError(f"{full}: unexpected staged changes: {changed}")
        cmd("git", "-c", "user.name=eshopbox-staging-rollout[bot]",
            "-c", "user.email=eshopbox-staging-rollout[bot]@users.noreply.github.com",
            "commit", "-q", "-m", f"ci: install centralized staging deploy caller (#{issue})", cwd=root)
        head = cmd("git", "rev-parse", "HEAD", cwd=root)
        if cmd("git", "rev-list", "--parents", "-n", "1", head, cwd=root).split() != [head, base]:
            raise RuntimeError("Rollout must contain one commit based on the target branch")
        cmd("git", "push", "--quiet", "origin", f"{head}:refs/heads/{branch}", cwd=root)
        body = (f"Central-Backlog: https://github.com/{ORG}/Central-Backlog/issues/{issue}\n\n"
                f"Automated organization App rollout of the canonical {DEST}.\n"
                f"Only this workflow file may change. Phase: {target}.\n"
                "Staging PR is merged before the independent main PR is created.\n")
        title = f"Patch to {'Staging' if target == 'staging' else 'Main'}: Install central staging deploy caller (#{issue})"
        url = cmd("gh", "pr", "create", "--repo", full, "--base", target,
                  "--head", branch, "--title", title, "--body", body, cwd=root)
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        print(f"CREATED: {url}", flush=True)
        live = gh_json("pr", "view", str(number), "--repo", full,
                       "--json", "state,baseRefName,headRefOid")
        if live["state"] != "OPEN" or live["baseRefName"] != target or live["headRefOid"] != head:
            raise RuntimeError("PR changed before administrative merge")
        if cmd("git", "ls-remote", "origin", f"refs/heads/{target}", cwd=root).split()[0] != base:
            raise RuntimeError(f"{target} advanced before merge; abort")
        cmd("gh", "pr", "merge", str(number), "--repo", full, "--merge",
            "--admin", "--match-head-commit", head, cwd=root)
        verify_merged(full, number, target, base, head, CALLER)
        return "merged"


def assert_eligible(repository):
    """Repeat live CI/CD compatibility checks with the scoped App token.

    Queued bulk jobs must not act on eligibility determined minutes earlier.
    This also protects standalone single-repository APPLY executions.
    """
    spec = importlib.util.spec_from_file_location(
        "staging_caller_plan", ROOT / "plan-rollout.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state, reason = module.inspect(repository, "all")
    if state not in ("eligible", "current"):
        raise ValueError(f"Repository no longer eligible: {reason}")


def rollout(repository, operation, issue):
    if not REPO_NAME.fullmatch(repository):
        raise ValueError("Repository must be a single repository name, not owner/name or URL")
    if not re.fullmatch(r"[0-9]+", issue):
        raise ValueError("Central Backlog issue must be numeric")
    if operation == "APPLY" and os.environ.get("CONFIRMATION") != "APPLY":
        raise ValueError("APPLY requires exact confirmation APPLY")
    full = f"{ORG}/{repository}"
    info = gh_json("repo", "view", full, "--json",
                   "name,defaultBranchRef,isArchived,isFork")
    if info["isArchived"] or info["isFork"]:
        raise ValueError("Archived/forked repository cannot be updated")
    if info["defaultBranchRef"]["name"] != "main":
        raise ValueError("Expected default branch main")
    # Both branches must already exist; never create or change protected branches.
    for target in ("staging", "main"):
        cmd("gh", "api", f"repos/{full}/branches/{target}", "--jq", ".name")
    # Important: staging is merged and verified BEFORE independently cloning main.
    if operation == "APPLY":
        assert_eligible(repository)
    staging = phase(full, "staging", issue, operation)
    if operation == "APPLY":
        assert_eligible(repository)
    main = phase(full, "main", issue, operation)
    print(f"SUMMARY {full}: staging={staging} main={main}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--operation", choices=["DRY_RUN", "APPLY"], required=True)
    parser.add_argument("--issue", default="7064")
    args = parser.parse_args()
    try:
        rollout(args.repository, args.operation, args.issue)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
