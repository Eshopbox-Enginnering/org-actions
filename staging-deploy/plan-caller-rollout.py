#!/usr/bin/env python3
"""Select eligible repositories for the central staging caller; never mutate targets."""
import argparse
import base64
import json
import os
import re
import subprocess
import sys

ORG = "Eshopbox-Enginnering"
NAME = re.compile(r"[A-Za-z0-9_.-]+\\Z")
DEPLOY_FILES = ("cicd.yml", "ci-cd.yml")
CALLER_FILE = ".github/workflows/auto-staging-deploy.yml"
MAX_SELECTED = 25
MAX_MATRIX = 256


def gh_json(*args):
    proc = subprocess.run(("gh", *args), text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(f"gh {' '.join(args)}: {proc.stderr[-600:]}")
    return json.loads(proc.stdout)


def file_bytes(repo, path, ref):
    try:
        data = gh_json("api", f"repos/{ORG}/{repo}/contents/{path}?ref={ref}")
    except RuntimeError as error:
        if "HTTP 404" in str(error) or "Not Found" in str(error):
            return None
        raise
    if not isinstance(data, dict) or data.get("type") != "file":
        raise RuntimeError(f"Unsafe or unexpected file type: {repo}:{ref}:{path}")
    return base64.b64decode(data["content"])


def validate_selection(target, repository, repositories):
    if target == "single":
        if not NAME.fullmatch(repository) or repositories.strip():
            raise ValueError("single requires exactly one valid repository; repositories must be blank")
        return [repository]
    if target == "selected":
        if repository.strip():
            raise ValueError("repository must be blank for selected")
        names = [x.strip() for x in re.split(r"[,\\n]", repositories) if x.strip()]
        if not names or len(names) > MAX_SELECTED or len(set(names)) != len(names):
            raise ValueError("selected requires 1-25 distinct repositories")
        if any(not NAME.fullmatch(x) for x in names):
            raise ValueError("selected requires repository names, not owner/name or URL")
        return names
    if target not in ("backend", "all") or repository.strip() or repositories.strip():
        raise ValueError("bulk selection must leave repository and repositories blank")
    return None


def select(target, repository, repositories):
    explicit = validate_selection(target, repository, repositories)
    if explicit is not None:
        return explicit
    rows = gh_json("repo", "list", ORG, "--limit", "1000", "--json",
                   "name,isArchived,isFork,defaultBranchRef")
    if len(rows) >= 1000:
        raise RuntimeError("Repository discovery reached 1000-result limit; refuse incomplete all rollout")
    return sorted(x["name"] for x in rows)


def inspect(repo, backend_only, canonical):
    meta = gh_json("repo", "view", f"{ORG}/{repo}", "--json",
                   "name,isArchived,isFork,defaultBranchRef")
    if meta.get("isArchived") or meta.get("isFork"):
        return "SKIP", "archived or fork"
    if (meta.get("defaultBranchRef") or {}).get("name") != "main":
        return "BLOCKED", "default branch is not main"
    # Check both targets without creating any refs.
    for ref in ("staging", "main"):
        try:
            gh_json("api", f"repos/{ORG}/{repo}/branches/{ref}")
        except RuntimeError:
            return "BLOCKED", f"missing {ref} branch"
    if backend_only and not any(
        file_bytes(repo, path, "main") is not None for path in ("pom.xml", "backend/pom.xml")
    ):
        return "SKIP", "no Java Maven backend found"
    # Ensure one supported, dispatchable deployment workflow exists on both
    # targets; the runtime promotion checks the exact merged tree again.
    for ref in ("main", "staging"):
        deployed = []
        for name in DEPLOY_FILES:
            data = file_bytes(repo, f".github/workflows/{name}", ref)
            if data is not None:
                deployed.append((name, data))
        if len(deployed) != 1:
            return "BLOCKED", f"{ref}: expected one deployment workflow, found {len(deployed)}"
        if not re.search(rb"(?m)^\\s+workflow_dispatch\\s*:", deployed[0][1]):
            return "BLOCKED", f"{ref}: {deployed[0][0]} lacks workflow_dispatch"
    current = [file_bytes(repo, CALLER_FILE, ref) for ref in ("staging", "main")]
    if all(blob == canonical for blob in current):
        return "CURRENT", "caller installed on both branches"
    return "ELIGIBLE", "caller missing or outdated"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("single", "selected", "backend", "all"), required=True)
    parser.add_argument("--repository", default="")
    parser.add_argument("--repositories", default="")
    parser.add_argument("--caller", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    canonical = open(args.caller, "rb").read()
    names = select(args.target, args.repository, args.repositories)
    if len(names) > MAX_MATRIX:
        raise RuntimeError(f"{len(names)} discovered repositories exceed matrix capacity of {MAX_MATRIX}; use selected batches")
    statuses = []
    eligible = []
    for name in names:
        try:
            status, reason = inspect(name, args.target == "backend", canonical)
        except Exception as e:
            status, reason = "BLOCKED", f"inspection failed: {str(e)[:300]}"
        statuses.append(dict(repository=name, status=status, reason=reason))
        print(f"{name}: {status}: {reason}", flush=True)
        if status == "ELIGIBLE":
            eligible.append(name)
    with open(args.output, "w") as f:
        json.dump({"include": [{"repository": name} for name in eligible]}, f)
    print("SUMMARY " + json.dumps({
        status: sum(1 for row in statuses if row["status"] == status)
        for status in ("ELIGIBLE", "CURRENT", "BLOCKED", "SKIP")
    }, sort_keys=True), flush=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as out:
            out.write("## Staging caller rollout eligibility\\n\\n")
            out.write("| Repository | Status | Reason |\\n|---|---|---|\\n")
            for item in statuses:
                reason = item["reason"].replace("|", "/").replace("\\n", " ")
                out.write(f"| \\u0060{item['repository']}\\u0060 | {item['status']} | {reason} |\\n")
            out.write("\\n")
    if args.target in ("single", "selected") and any(x["status"] == "BLOCKED" for x in statuses):
        raise RuntimeError("Explicit selection contains blocked repositories; inspect report before applying")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        print("FAILED:", exc, file=sys.stderr)
        sys.exit(1)
