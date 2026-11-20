#!/usr/bin/env python3
"""Plan an explicit, eligible, bounded central staging caller rollout.

Never change remote repositories in this script. All modes use this planner.
"""
import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

ORG = "Eshopbox-Enginnering"
MAX_SELECTED = 25
MAX_BATCH = 250
NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
CALLER = (Path(__file__).resolve().parent / "caller.yml").read_bytes()
DEST = ".github/workflows/auto-staging-deploy.yml"
DEPLOY = ("cicd.yml", "ci-cd.yml")


def gh(*args):
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[-700:] or "GitHub CLI request failed")
    return p.stdout.strip()


def api_json(path):
    return json.loads(gh("api", path))


def file_at(repo, path, ref):
    try:
        doc = api_json(f"repos/{ORG}/{repo}/contents/{path}?ref={ref}")
    except RuntimeError as exc:
        # A 404 is an absent file, but permission errors and other API
        # failures must not silently become missing-file decisions.
        if "404" in str(exc) or "Not Found" in str(exc):
            return None
        raise
    if not isinstance(doc, dict) or doc.get("type") != "file":
        return None
    return base64.b64decode(doc["content"].replace("\n", ""))


def names(args):
    if args.target == "single":
        if not NAME.fullmatch(args.repository) or args.repositories.strip():
            raise ValueError("single requires one repository name only")
        return [args.repository]
    if args.target == "selected":
        if args.repository.strip():
            raise ValueError("selected must not specify repository")
        selected = [x.strip() for x in args.repositories.split(",") if x.strip()]
        if not selected or len(selected) > MAX_SELECTED:
            raise ValueError("selected requires 1 to 25 comma-separated names")
        if len({x.casefold() for x in selected}) != len(selected) or any(not NAME.fullmatch(x) for x in selected):
            raise ValueError("selected contains duplicate or invalid names")
        return selected
    if args.repository.strip() or args.repositories.strip():
        raise ValueError("backend/all must not specify repository names")
    data = json.loads(gh("repo", "list", ORG, "--limit", "1000",
                         "--json", "name,isArchived,isFork,defaultBranchRef"))
    if len(data) == 1000:
        raise ValueError("Repository discovery reached the 1000-repository limit; fail closed")
    return sorted((x["name"] for x in data), key=str.casefold)


def inspect(repo, target):
    info = api_json(f"repos/{ORG}/{repo}")
    if info.get("archived") or info.get("fork") or info.get("disabled"):
        return "skipped", "archived, forked or disabled"
    if info.get("default_branch") != "main":
        return "skipped", "requires default branch main"
    try:
        api_json(f"repos/{ORG}/{repo}/branches/staging")
    except RuntimeError as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            return "skipped", "no staging branch"
        raise
    if target == "backend" and not (
        file_at(repo, "pom.xml", "main") or
        file_at(repo, "backend/pom.xml", "main")
    ):
        return "skipped", "not a Maven backend repository"
    # Mirror auto-staging-deploy.cjs: count all ACTIVE workflows, not just
    # workflow files that happen to advertise workflow_dispatch. A disabled
    # workflow is not usable and two active workflows are ambiguous.
    active = []
    for filename in DEPLOY:
        try:
            workflow = api_json(f"repos/{ORG}/{repo}/actions/workflows/{filename}")
        except RuntimeError as exc:
            if "404" in str(exc) or "Not Found" in str(exc):
                continue
            raise
        if workflow.get("state") == "active":
            active.append(filename)
    if len(active) != 1:
        return "skipped", f"expected exactly one active cicd.yml or ci-cd.yml, found {active}"
    workflow_file = active[0]
    for ref in ("staging", "main"):
        content = file_at(repo, f".github/workflows/{workflow_file}", ref)
        if not content or not re.search(rb"(?m)^\s+workflow_dispatch\s*:", content):
            return "skipped", f"{ref}: active {workflow_file} lacks workflow_dispatch"
    installed = all(file_at(repo, DEST, ref) == CALLER for ref in ("staging", "main"))
    return ("current", "already installed on staging and main") if installed else ("eligible", "ready for staged rollout")


def plan(args):
    found = names(args)
    if len(found) > MAX_BATCH:
        raise ValueError(f"More than {MAX_BATCH} repositories selected; choose smaller batches")
    report, candidates = [], []
    for repo in found:
        try:
            state, reason = inspect(repo, args.target)
        except Exception as exc:
            state, reason = "error", str(exc)[:280]
        report.append({"repository": repo, "status": state, "reason": reason})
        if state == "eligible":
            candidates.append({"repository": repo})
    print(json.dumps({"selected":len(found), "eligible":len(candidates),
                      "current":sum(x["status"]=="current" for x in report),
                      "skipped":sum(x["status"]=="skipped" for x in report),
                      "error":sum(x["status"]=="error" for x in report)},
                     sort_keys=True), flush=True)
    for entry in report:
        print(f"{entry['repository']}: {entry['status']} - {entry['reason']}", flush=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"include":candidates}, separators=(",",":")))
    Path(args.report).write_text(json.dumps(report, indent=2)+"\n")
    if any(x["status"]=="error" for x in report):
        raise RuntimeError("Repository eligibility inspection failed; no APPLY jobs dispatched")
    if args.target in ("single", "selected") and any(x["status"]=="skipped" for x in report):
        raise RuntimeError("Explicitly selected repository is ineligible")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("single","selected","backend","all"), required=True)
    parser.add_argument("--repository", default="")
    parser.add_argument("--repositories", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    plan(args)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print("FAILED:", exc, file=sys.stderr)
        sys.exit(1)
