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
    "staging-deploy/auto-staging-deploy.cjs": ".github/scripts/auto-staging-deploy.cjs",
    "staging-deploy/auto-staging-pr.cjs": ".github/scripts/auto-staging-pr.cjs",
    "staging-deploy/auto-staging-deploy.test.cjs": ".github/scripts/auto-staging-deploy.test.cjs",
    "staging-deploy/auto-staging-pr.test.cjs": ".github/scripts/auto-staging-pr.test.cjs",
    "staging-deploy/auto-staging-deploy.yml": ".github/workflows/auto-staging-deploy.yml",
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
    repo = gh_api(f"repos/{full}")
    if not repo.get("allow_merge_commit"):
        # Rebase/squash could rewrite the staged feature SHA and fail the main gate.
        raise RuntimeError(f"{full}: merge commits disabled; cannot preserve staged feature SHA")
    deadline = time.monotonic() + 40 * 60
    submitted = False
    while time.monotonic() < deadline:
        pr = json.loads(cmd("gh", "pr", "view", str(number), "--repo", full,
                            "--json", "headRefOid,baseRefName,state,mergeStateStatus,mergedAt"))
        if pr.get("mergedAt"):
            return "already merged"
        if pr["headRefOid"] != sha or pr["baseRefName"] != target or pr["state"] != "OPEN":
            raise RuntimeError(f"{full}: {target} PR changed during validation")
        if pr["mergeStateStatus"] == "DIRTY":
            raise RuntimeError(f"{full}: {target} PR has conflicts")
        if not submitted and pr["mergeStateStatus"] == "CLEAN":
            if validate:
                validate()
            cmd("gh", "pr", "merge", str(number), "--repo", full, "--merge",
                "--match-head-commit", sha)
            submitted = True
        # A merge queue may accept a PR before mergedAt is set.
        time.sleep(10)
    raise RuntimeError(f"{full}: {target} PR not merged within 40 minutes; left open")



def install(full, name, issue):
    # One shared feature commit from main is proposed to staging, then main.
    # GitHub's main-branch required check can therefore prove that the exact
    # non-merge feature commit already exists in staging. No exemptions needed.
    with tempfile.TemporaryDirectory(prefix="staging-rollout-") as directory:
        cmd("gh", "repo", "clone", full, directory, "--", "--depth=1",
            "--branch", "main")
        root = Path(directory)
        changed = stage_payload(root)
        # The full staging PR diff must contain ONLY managed files, not unrelated
        # commits present on main but missing from staging. Fail closed before
        # any branch push or PR creation. This also preserves main's exact
        # feature commit for the required staging-presence check.
        # A depth-1 main clone cannot calculate the staging...main merge base.
        # Recover complete history before inspecting the full effective PR diff.
        # Never interpret missing history as an empty or safe staging diff.
        cmd("git", "fetch", "--unshallow", "origin", cwd=root)
        cmd("git", "fetch", "origin",
            "+refs/heads/staging:refs/remotes/origin/staging", cwd=root)
        merge_base = cmd("git", "merge-base", "origin/staging", "HEAD",
                         cwd=root, check=False)
        if merge_base.returncode or not merge_base.stdout.strip():
            raise RuntimeError(f"{name}: staging and main have no verified merge base")
        pending_main = set(cmd("git", "diff", "--name-only",
                               "origin/staging...HEAD", cwd=root).splitlines())
        if pending_main - set(SOURCE_FILES.values()):
            raise RuntimeError(
                f"{name}: staging is missing unrelated main changes; "
                f"refusing polluted staging PR: {sorted(pending_main - set(SOURCE_FILES.values()))}")
        if not changed:
            print(f"{name}: NO_CHANGE already installed on main", flush=True)
            return "current"
        base_sha = cmd("git", "rev-parse", "HEAD", cwd=root)
        payload_hash = hashlib.sha256(b"".join(
            (ROOT / src).read_bytes() for src in SOURCE_FILES)).hexdigest()[:10]
        branch = f"org-action/staging-promotion-{issue}-{base_sha[:10]}-{payload_hash}"
        active = managed_open_prs(full, issue)
        foreign = [pr for pr in active if pr["headRefName"] != branch]
        if foreign:
            raise RuntimeError(f"{name}: older rollout PR still open; reconcile: " +
                               ", ".join(pr["url"] for pr in foreign))
        if len(active) > 1:
            raise RuntimeError(f"{name}: multiple open rollout PRs need reconciliation")
        existing = {target: pr_for_branch(full, branch, target)
                    for target in ("staging", "main")}
        if existing["main"] and not (existing["staging"] and existing["staging"].get("mergedAt")):
            raise RuntimeError(f"{name}: main PR exists before staging was merged")
        remote = cmd("git", "ls-remote", "--heads", "origin", branch, cwd=root)
        if remote:
            if not any(existing.values()):
                raise RuntimeError(f"{name}: orphaned rollout branch {branch}")
            # Discard only the temporary clone's uncommitted staging bytes.
            # Git will then safely check out the previously reviewed branch.
            cmd("git", "reset", "--hard", "HEAD", cwd=root)
            cmd("git", "fetch", "origin", branch, cwd=root)
            cmd("git", "checkout", "-B", branch, "FETCH_HEAD", cwd=root)
            actual_files = cmd("git", "diff", "--name-only", "origin/main...HEAD", cwd=root).splitlines()
            if set(actual_files) != set(changed):
                raise RuntimeError(f"{name}: existing branch payload differs from reviewed rollout")
            for src, target in SOURCE_FILES.items():
                actual = cmd("git", "show", f"HEAD:{target}", cwd=root)
                if actual != (ROOT / src).read_text().strip():
                    raise RuntimeError(f"{name}: existing branch file differs: {target}")
        else:
            cmd("git", "checkout", "-b", branch, cwd=root)
            cmd("git", "add", "--", *changed, cwd=root)
            cmd("git", "-c", "user.name=eshopbox-org-actions[bot]",
                "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit", "-m", f"feat: enable staging promotion automation (#{issue})", cwd=root)
            cmd("git", "push", "-u", "origin", branch, cwd=root)
        feature_sha = cmd("git", "rev-parse", "HEAD", cwd=root)
        validate_payload(root, branch, feature_sha, base_sha, changed)
        for target in ("staging", "main"):
            if target == "main" and not cmd("git", "ls-remote", "--heads",
                                             "origin", branch, cwd=root):
                # GitHub may auto-delete the head branch after staging merges.
                # Re-create only this exact, already-reviewed feature SHA;
                # do not change its contents or force-push anything.
                cmd("git", "push", "origin",
                    f"{feature_sha}:refs/heads/{branch}", cwd=root)
            prior = existing[target]
            if prior:
                if verify_pr(full, prior, target):
                    print(f"{name} {target}: ALREADY_MERGED {prior['url']}", flush=True)
                    continue
                number = prior["number"]
                print(f"{name} {target}: RESUME {prior['url']}", flush=True)
            else:
                # On main, staged feature commit is now in staging by ancestry.
                # Do not open the main PR until staging merged successfully.
                body = (
                    f"Central Backlog: https://github.com/{full.split('/')[0]}/Central-Backlog/issues/{issue}\\n\\n"
                    "One atomic rollout: workflow plus four scripts/tests. "
                    "Exact feature commit goes to staging first, then main; "
                    "normal GitHub checks and branch protection apply.\\n\\n"
                    f"Files: {', '.join(changed)}"
                )
                url = cmd("gh", "pr", "create", "--repo", full,
                          "--base", target, "--head", branch,
                          "--title", f"Feature to {'Staging' if target == 'staging' else 'Main'}: Add staging promotion automation (#{issue})",
                          "--body", body, cwd=root)
                pr = pr_for_branch(full, branch, target)
                if not pr or pr["url"] != url:
                    raise RuntimeError(f"{name}: cannot verify created PR {url}")
                number = pr["number"]
                print(f"{name} {target}: CREATED {url}", flush=True)
            merge_regular_pr(full, number, target, feature_sha,
                             validate=lambda: validate_payload(
                                 root, branch, feature_sha, base_sha, changed))
            print(f"{name} {target}: MERGED PR #{number}", flush=True)
        return "merged"


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
