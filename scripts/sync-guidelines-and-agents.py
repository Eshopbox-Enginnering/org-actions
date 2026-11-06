#!/usr/bin/env python3
"""Create one verified staging PR and one verified production PR per repository.

Both the canonical docs and the centrally managed AGENTS.md content are
committed together. The pre-existing updater remains available for AGENTS-only
rollouts; the same audited inline migrator is invoked here to preserve its
marker validation, historical migration, and idempotency behavior.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = load("combined_sync", "sync-engineering-guidelines.py")
auto = load("combined_merge", "sync-guidelines-auto-merge.py")
AGENTS_SOURCE = ROOT / "payload/agents-update.md"
TRUSTED_LEGACY = ROOT / "payload/agents-legacy-v1.md"


def migrator_source():
    """Reuse the exact logic exercised by test_agents_legacy_migration.py."""
    script = (ROOT / "scripts/update-agents.sh").read_text()
    return script.split("apply_agents_content() {", 1)[1].split(
        "<<'PY'\n", 1)[1].split("\nPY", 1)[0]


def expected_agents(root):
    target = root / "AGENTS.md"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("AGENTS.md must be a regular file, not a symlink")
    previous = target.read_bytes() if target.exists() else None
    payload = AGENTS_SOURCE.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()[:12]
    result = subprocess.run(
        [sys.executable, "-", str(AGENTS_SOURCE), digest, "apply",
         str(TRUSTED_LEGACY)],
        input=migrator_source(), text=True, cwd=root, capture_output=True)
    if result.returncode not in (0, 2):
        raise ValueError(f"AGENTS.md managed-content validation failed: "
                         f"{result.stdout[-600:]} {result.stderr[-300:]}")
    updated = target.read_bytes()
    if result.returncode == 2 and updated != previous:
        raise ValueError("AGENTS.md skip unexpectedly modified the file")
    if result.returncode == 0 and updated == previous:
        raise ValueError("AGENTS.md updater reported a change without a diff")
    return updated, previous != updated


def select_catalog(args):
    catalog = json.loads((ROOT / "engineering-guidelines/catalog.json").read_text())
    selected = sync.select_files(
        catalog, args.file_id or "all", args.source_scope,
        args.source_url, args.destination)
    contents = {entry["source_url"]: sync.source_bytes(entry["source_url"])
                for entries in selected.values() for entry in entries}
    return selected, contents


def check_path_modes(root, paths):
    for name in paths:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Non-regular or symbolic-link target: {name}")
        mode = sync.cmd("git", "ls-files", "--stage", "--", name, cwd=root)
        if mode and not mode.startswith("100644 "):
            raise ValueError(f"Unexpected tracked file mode: {name}")


def phase(repo, args, selected, contents, branch_name):
    full = f"{args.org}/{repo['name']}"
    with sync.rollout_workspace(prefix="combined-guidelines-") as temporary:
        root = Path(temporary) / repo["name"]
        sync.cmd("gh", "repo", "clone", full, str(root), "--",
                 "--depth=1", "--branch", branch_name)
        kind = sync.detect_type(root)
        entries = selected.get(kind, [])
        if not entries:
            raise ValueError(f"No selected guidelines for repository type={kind}")
        doc_plan = sync.diff_plan(root, entries, contents)
        doc_edits = [row for row in doc_plan if row[1] != "SKIP"]
        agents_before = (root / "AGENTS.md").read_bytes() if (
            root / "AGENTS.md").exists() else None
        agents_expected, agents_changed = expected_agents(root)
        changed = {dest for dest, _, _, _ in doc_edits}
        if agents_changed:
            changed.add("AGENTS.md")
        if not changed:
            print(f"{repo['name']} {branch_name}: already current", flush=True)
            return "current"

        for dest, _, _, desired in doc_edits:
            path = root / dest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(desired)
        check_path_modes(root, changed)
        base_sha = sync.cmd("git", "rev-parse", "HEAD", cwd=root)
        payload_identity = hashlib.sha256(
            b"".join(dest.encode() + b"\0" + desired
                     for dest, _, _, desired in sorted(doc_plan))
            + AGENTS_SOURCE.read_bytes()).hexdigest()[:12]
        branch = (f"org-action/guidelines-combined-{kind}-"
                  f"{payload_identity}-{base_sha[:10]}")
        existing = sync.find_pr(full, branch_name, branch)
        if existing:
            number = existing["number"]
            print(f"{repo['name']}: rechecking open PR #{number}", flush=True)
        else:
            if sync.cmd("git", "ls-remote", "--heads", "origin", branch,
                        cwd=root, check=False):
                raise ValueError(f"Unclaimed existing rollout branch {branch}")
            if sync.find_pr(full, branch_name, branch, state="all"):
                raise ValueError(f"Historical rollout branch already used: {branch}")
            sync.cmd("git", "checkout", "-b", branch, cwd=root)
            sync.cmd("git", "add", "--", *sorted(changed), cwd=root)
            staged = set(sync.cmd(
                "git", "diff", "--cached", "--name-only", cwd=root).splitlines())
            if staged != changed:
                raise ValueError(f"Unexpected staged files: {sorted(staged)}")
            sync.cmd("git", "-c", "user.name=eshopbox-guidelines[bot]",
                     "-c", "user.email=eshopbox-guidelines[bot]@users.noreply.github.com",
                     "commit", "-m", "docs: sync engineering guidelines and AGENTS.md",
                     cwd=root)
            sync.cmd("git", "push", "-u", "origin", branch, cwd=root)
            summary = "\n".join(
                f"- {action}: `{dest}` (SHA256 {digest})"
                for dest, action, digest, _ in doc_edits)
            body = (f"Central Backlog: https://github.com/{args.org}/Central-Backlog/issues/{args.issue}"
                    f"\n\nAtomic guideline + AGENTS.md rollout, phase: {branch_name}."
                    f"\n\n{summary}\n"
                    + ("- UPDATE: `AGENTS.md` (validated organization-managed content)\n"
                       if agents_changed else "")
                    + "\nOnly selected docs/engineering-guidelines/ files and AGENTS.md may change.")
            url = sync.cmd(
                "gh", "pr", "create", "--repo", full,
                "--base", branch_name, "--head", branch,
                "--title", f"feature to {branch_name}: Sync guidelines and AGENTS.md (#{args.issue})",
                "--body", body)
            number = int(url.rstrip("/").rsplit("/", 1)[-1])
            print(f"{repo['name']}: CREATED combined PR {url}", flush=True)

        # Fail closed unless the exact expected docs and AGENTS bytes are
        # present. Validation also pins head SHA, base SHA, file scope/modes,
        # and verifies the merged tree.
        auto.merge_guideline_pr(
            full, number, branch_name, entries, contents,
            agents_expected=agents_expected)
        return "merged"


def rollout(args):
    if args.operation != "APPLY" or args.phase != "staging":
        raise ValueError("Combined PR rollout requires APPLY and phase=staging")
    if not re.fullmatch(r"[0-9]+", args.issue):
        raise ValueError("Central Backlog issue must be numeric")
    if args.target != "single" or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.repository):
        raise ValueError("Combined PR runner requires a single repository per invocation")
    if __import__("os").environ.get("CONFIRMATION") != "APPLY":
        raise ValueError("APPLY requires confirmation APPLY")
    selected, contents = select_catalog(args)
    repo = sync.gh_json(
        "repo", "view", f"{args.org}/{args.repository}",
        "--json", "name,defaultBranchRef,isArchived,isFork")
    if repo.get("isArchived") or repo.get("isFork"):
        raise ValueError("Cannot update archived or forked repository")
    production = (repo.get("defaultBranchRef") or {}).get("name", "")
    if production not in sync.ALLOWED_BRANCHES:
        raise ValueError(f"Unsupported production branch: {production}")
    with sync.rollout_workspace(prefix="combined-discover-") as temporary:
        root = Path(temporary) / repo["name"]
        sync.cmd("gh", "repo", "clone", f"{args.org}/{repo['name']}",
                 str(root), "--", "--depth=1", "--branch", production)
        staging = sync.find_staging(root, production)
    if not staging:
        raise ValueError("Cannot find supported staging branch")
    first = phase(repo, args, selected, contents, staging)
    second = phase(repo, args, selected, contents, production)
    print(f"SUMMARY {repo['name']}: staging={first}, production={second}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    for name in ("operation", "target", "repository", "phase", "file-id",
                 "source-url", "destination", "source-scope", "issue", "org"):
        parser.add_argument("--" + name, default="")
    rollout(parser.parse_args())


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
