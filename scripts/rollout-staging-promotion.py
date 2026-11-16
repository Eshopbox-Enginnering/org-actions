#!/usr/bin/env python3
"""Backend-only staging-workflow distribution using shared Guidelines PR lifecycle."""
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
