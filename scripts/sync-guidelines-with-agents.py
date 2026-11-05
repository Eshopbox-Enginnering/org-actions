#!/usr/bin/env python3
"""Sequential guideline and AGENTS.md rollout with optional selected repository scope.

Each selected repository completes guideline sync before its AGENTS update.
The existing sync/updater scripts remain the owners of their respective content,
PR creation, merge checks, and DRY_RUN behavior.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALID_REPO = re.compile(r"[A-Za-z0-9_.-]+\Z")
MAX_SELECTED = 25


def selected_repositories(raw):
    names = [part.strip() for part in re.split(r"[,\n]", raw) if part.strip()]
    if not names or len(names) > MAX_SELECTED:
        raise ValueError(f"Choose 1 to {MAX_SELECTED} repositories for target=selected")
    if any(not VALID_REPO.fullmatch(name) for name in names):
        raise ValueError("Selected repositories must be names, not URLs or owner/name")
    if len(set(names)) != len(names):
        raise ValueError("Duplicate selected repository names are not allowed")
    return names


def run_checked(command, env):
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def rollout(args, env, target, repository):
    common = [
        "--operation", args.operation, "--target", target,
        "--repository", repository, "--phase", args.phase,
        "--file-id", args.file_id, "--source-url", args.source_url,
        "--destination", args.destination, "--source-scope", args.source_scope,
        "--issue", args.issue, "--org", args.org,
    ]
    run_checked([sys.executable, "scripts/sync-guidelines-auto-merge.py", *common], env)
    if not args.update_agents:
        print("AGENTS.md update disabled by workflow input.", flush=True)
        return
    agents_env = dict(env)
    agents_env.update({
        "ORG": args.org,
        "OPERATION": args.operation,
        "TARGET": target,
        "TARGET_REPO": repository,
        "CENTRAL_BACKLOG_ISSUE": args.issue,
        "CONTENT_FILE": "payload/agents-update.md",
        "BATCH_ID": os.environ.get("GITHUB_RUN_ID", "local"),
        "GITHUB_WORKSPACE": str(ROOT),
    })
    print(f"Syncing AGENTS.md after guidelines for {repository or target}", flush=True)
    run_checked(["bash", "scripts/update-agents.sh"], agents_env)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", choices=["DRY_RUN", "APPLY"], required=True)
    parser.add_argument("--target", choices=["single", "selected", "backend", "frontend", "all"], required=True)
    parser.add_argument("--repository", default="")
    parser.add_argument("--repositories", default="")
    parser.add_argument("--phase", choices=["staging", "production"], required=True)
    parser.add_argument("--file-id", default="all")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--destination", default="")
    parser.add_argument("--source-scope", choices=["backend", "frontend"], required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument("--update-agents", choices=["true", "false"], default="true")
    args = parser.parse_args()
    args.update_agents = args.update_agents == "true"

    if not re.fullmatch(r"[0-9]+", args.issue):
        parser.error("issue must be numeric")
    if args.operation == "APPLY" and os.getenv("CONFIRMATION") != "APPLY":
        parser.error("APPLY requires exact confirmation APPLY")
    if args.update_agents and args.phase != "staging":
        parser.error("AGENTS update requires phase=staging; disable update_agents for legacy production-only recovery")
    if args.target == "selected":
        if args.repository:
            parser.error("repository must be blank when target=selected")
        names = selected_repositories(args.repositories)
        plan = [("single", name) for name in names]
    else:
        if args.repositories.strip():
            parser.error("repositories is only supported with target=selected")
        if args.target == "single" and not VALID_REPO.fullmatch(args.repository):
            parser.error("single target requires a valid repository name")
        if args.target != "single" and args.repository:
            parser.error("repository must be blank for bulk targets")
        plan = [(args.target, args.repository)]

    env = os.environ.copy()
    # Validate all inputs before making any target-repository changes.
    for target, repo in plan:
        print(f"\n=== Target: {repo or target} | {args.operation} | "
              f"guidelines{' + AGENTS.md' if args.update_agents else ''} ===", flush=True)
        rollout(args, env, target, repo)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
