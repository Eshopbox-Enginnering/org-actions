#!/usr/bin/env python3
"""Controlled deployment-automation inventory and PR-only rollout (#7319).

DRY_RUN examines backend candidates. APPLY creates a staging PR, waits for
its standard-rule merge, and only then creates/merges a main PR. No bypass.
"""
import base64
import json
import os
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILES = {
    "staging-deploy/auto-staging-caller.yml": ".github/workflows/auto-staging-deploy.yml",
}
TOKEN_LAST_REFRESH = 0
TOKEN_REFRESHING = False

def refresh_app_token():
    """Refresh installation credentials when a batch outlives its original token."""
    global TOKEN_LAST_REFRESH, TOKEN_REFRESHING
    if not os.environ.get("APP_PRIVATE_KEY") or TOKEN_REFRESHING:
        return
    if time.monotonic() - TOKEN_LAST_REFRESH < 35 * 60:
        return
    TOKEN_REFRESHING = True
    try:
        now = int(time.time())
        encode = lambda obj: base64.urlsafe_b64encode(json.dumps(
            obj, separators=(",", ":")).encode()).decode().rstrip("=")
        data = encode({"alg": "RS256", "typ": "JWT"}) + "." + encode({
            "iat": now - 60, "exp": now + 540, "iss": os.environ["APP_ID"]})
        with tempfile.NamedTemporaryFile(mode="w", prefix="gh-app-key-") as key:
            os.chmod(key.name, 0o600)
            key.write(os.environ["APP_PRIVATE_KEY"])
            key.flush()
            signature = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", key.name],
                input=data.encode(), capture_output=True, check=True).stdout
        jwt = data + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")
        auth = ["-H", f"Authorization: Bearer {jwt}"]
        installed = subprocess.run(
            ["gh", "api", f"orgs/{os.environ['ORG']}/installation", *auth],
            capture_output=True, text=True, check=True)
        iid = json.loads(installed.stdout)["id"]
        minted = subprocess.run(
            ["gh", "api", "--method", "POST",
             f"app/installations/{iid}/access_tokens", *auth],
            capture_output=True, text=True, check=True)
        os.environ["GH_TOKEN"] = json.loads(minted.stdout)["token"]
        os.environ["GITHUB_TOKEN"] = os.environ["GH_TOKEN"]
        TOKEN_LAST_REFRESH = time.monotonic()
    finally:
        TOKEN_REFRESHING = False


SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def cmd(*args, cwd=None, check=True):
    refresh_app_token()
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


def pr_for_branch(full, branch, target):
    result = cmd("gh", "pr", "list", "--repo", full, "--head", branch,
                 "--state", "all", "--json", "number,url,state,baseRefName,mergedAt")
    found = [pr for pr in json.loads(result) if pr["baseRefName"] == target]
    if len(found) > 1:
        raise RuntimeError(f"Ambiguous {target} PRs for {branch}")
    return found[0] if found else None


def verify_pr(full, pr, target):
    if pr["baseRefName"] != target:
        raise RuntimeError(f"Existing PR targets {pr['baseRefName']}, expected {target}")
    if pr["state"] == "CLOSED" and not pr.get("mergedAt"):
        raise RuntimeError(f"Previous {target} PR was closed without merge: {pr['url']}")
    return bool(pr.get("mergedAt"))


def managed_open_prs(full, issue):
    prs = json.loads(cmd("gh", "pr", "list", "--repo", full, "--state",
                         "open", "--limit", "1000",
                         "--json", "number,url,headRefName,baseRefName"))
    prefix = f"org-action/staging-promotion-{issue}-"
    return [pr for pr in prs if pr["headRefName"].startswith(prefix)]


def validate_payload(root, branch, feature_sha, base_sha, changed):
    """Fail closed if the retained commit, ancestry or exact bytes change."""
    cmd("git", "fetch", "origin", branch, cwd=root)
    if cmd("git", "rev-parse", "FETCH_HEAD", cwd=root) != feature_sha:
        raise RuntimeError("Managed rollout branch HEAD changed")
    parents = cmd("git", "rev-list", "--parents", "-n", "1",
                  feature_sha, cwd=root).split()
    if len(parents) != 2 or parents[1] != base_sha:
        raise RuntimeError("Managed rollout commit ancestry differs from main")
    files = cmd("git", "diff", "--name-only", base_sha,
                feature_sha, cwd=root).splitlines()
    if set(files) != set(changed):
        raise RuntimeError("Managed rollout contains unexpected file changes")
    for src, target in SOURCE_FILES.items():
        actual = subprocess.run(["git", "show", f"{feature_sha}:{target}"],
                                cwd=root, capture_output=True, check=True).stdout
        if actual != (ROOT / src).read_bytes():
            raise RuntimeError(f"Managed rollout payload mismatch: {target}")


def merge_regular_pr(full, number, target, sha, validate=None):
    """Merge an installer-owned PR with the configured GitHub App token.

    The App is an explicitly configured organization ruleset bypass actor.
    A BLOCKED mergeStateStatus can indicate requirements intentionally bypassed
    for this App, so do not wait for CLEAN or request admin mode. GitHub remains
    authoritative: an unauthorized merge or real conflict fails immediately.
    """
    repo = gh_api(f"repos/{full}")
    if not repo.get("allow_merge_commit"):
        raise RuntimeError(f"{full}: merge commits disabled; cannot preserve staged feature SHA")
    # GitHub may need a few seconds to calculate mergeability after PR creation.
    # Only retry unknown (null) mergeability, never check/approval BLOCKED states.
    for attempt in range(6):
        pr = gh_api(f"repos/{full}/pulls/{number}")
        if pr.get("merged") or pr.get("mergeable") is not None:
            break
        if attempt < 5:
            time.sleep(2)
    if not pr.get("merged") and pr.get("mergeable") is None:
        raise RuntimeError(f"{full}: {target} PR mergeability is still unknown; retry later")
    if pr.get("merged"):
        if pr.get("head", {}).get("sha") != sha or pr.get("base", {}).get("ref") != target:
            raise RuntimeError(f"{full}: merged {target} PR differs from expected commit or base")
        return "already merged"
    if (pr.get("head", {}).get("sha") != sha or
            pr.get("base", {}).get("ref") != target or pr.get("state") != "open"):
        raise RuntimeError(f"{full}: {target} PR changed during validation")
    if pr.get("draft"):
        raise RuntimeError(f"{full}: {target} rollout PR is a draft")
    if pr.get("mergeable") is False:
        raise RuntimeError(f"{full}: {target} PR has merge conflicts")
    if validate:
        validate()
    # REST merge API enforces the exact expected head commit and authenticates
    # with the same installation token used to create the PR. No --admin, no
    # force-push, no bypass changes to organization rulesets.
    response = gh_api_merge(full, number, sha)
    if not response.get("merged"):
        raise RuntimeError(f"{full}: GitHub declined {target} PR merge: {response}")
    return "merged"


def gh_api_merge(full, number, sha):
    return json.loads(cmd("gh", "api", "--method", "PUT",
                          f"repos/{full}/pulls/{number}/merge",
                          "-f", "merge_method=merge", "-f", f"sha={sha}"))


def verify_staging_ancestry(root, feature_sha):
    """Require the exact rollout commit in current remote staging before main."""
    cmd("git", "fetch", "origin",
        "+refs/heads/staging:refs/remotes/origin/staging", cwd=root)
    result = cmd("git", "merge-base", "--is-ancestor", feature_sha,
                 "refs/remotes/origin/staging", cwd=root, check=False)
    if result.returncode:
        raise RuntimeError("Refusing main merge: rollout feature commit is not in remote staging")



def install(full, name, issue):
    """Delegate PR creation and App-authorized merges to working Guidelines rollout.

    Staging and main get independent, target-based commits. There is no custom
    checkout/merge lifecycle and only the one allowlisted workflow can change.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "shared_guideline_merger", ROOT / "scripts/sync-guidelines-auto-merge.py")
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    entry = {
        "id": "staging-deployment",
        "source_url": "staging-deploy/auto-staging-caller.yml",
        "destination": ".github/workflows/auto-staging-deploy.yml",
    }
    source = ROOT / entry["source_url"]
    selected = {"backend": [entry]}
    contents = {entry["source_url"]: source.read_bytes()}
    repository = {"name": name, "defaultBranchRef": {"name": "main"}}
    outcome = shared.rollout(
        repository, full.split("/")[0], selected, contents, issue,
        single=True, rollout_family="staging-deployment")
    if outcome not in ("merged", "synced"):
        raise RuntimeError(f"{name}: Guidelines rollout lifecycle returned {outcome}")
    return outcome

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
