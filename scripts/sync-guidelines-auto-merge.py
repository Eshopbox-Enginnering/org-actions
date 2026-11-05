#!/usr/bin/env python3
"""Sequential guideline rollout: merge staging before creating/merging main.

Automatic merge must honor branch protections; no administrator bypass is used.
Fail closed on unexpected files, stale branches, or merge errors.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "guideline_sync", ROOT / "scripts/sync-engineering-guidelines.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def require_regular_guideline_file(root, ref, destination):
    """Reject symlinks, submodules and executable files in guideline trees."""
    entry = sync.cmd("git", "ls-tree", ref, "--", destination, cwd=root)
    if not entry.startswith("100644 blob ") or not entry.endswith(
            "\\t" + destination):
        raise ValueError(
            f"Guideline must be a regular non-executable file: "
            f"{destination} at {ref}")


def verify_merged_document_commit(root, merge_sha, verified_base,
                                  verified_head, expected, entries, contents):
    """Validate the exact resulting merge tree before reporting success."""
    sync.cmd("git", "fetch", "origin", merge_sha, cwd=root)
    parents = sync.cmd("git", "rev-list", "--parents", "-n", "1",
                       merge_sha, cwd=root).split()
    if len(parents) != 3 or parents[1] != verified_base or parents[2] != verified_head:
        raise ValueError("Merge commit parents differ from inspected base/head; "
                         "a concurrent branch update may have changed the merge")
    changed = set(sync.cmd("git", "diff", "--name-only", parents[1],
                           merge_sha, cwd=root).splitlines())
    if changed != expected:
        raise ValueError(
            "Merged PR changed unexpected files: "
            f"actual={sorted(changed)} expected={sorted(expected)}")
    for entry in entries:
        destination = entry["destination"]
        require_regular_guideline_file(root, merge_sha, destination)
        result = subprocess.run(
            ("git", "show", f"{merge_sha}:{destination}"),
            cwd=root, capture_output=True)
        if result.returncode or result.stdout != contents[entry["source_url"]]:
            raise ValueError(f"Merged PR has noncanonical document: {destination}")


def merge_guideline_pr(full, number, target, entries, contents):
    """Verify the PR's effective patch, its base, and canonical bytes before merge."""
    pr = sync.gh_json("pr", "view", str(number), "--repo", full,
                      "--json", "number,baseRefName,headRefName,state,mergedAt,url")
    if pr["baseRefName"] != target:
        raise ValueError(f"{full} PR #{number}: unexpected base")
    if pr.get("mergedAt"):
        raise ValueError(f"{full} PR #{number}: merged before validation")
    if pr["state"] != "OPEN":
        raise ValueError(f"{full} PR #{number}: not open")
    head = pr["headRefName"]
    if not head.startswith("org-action/guidelines-"):
        raise ValueError(f"{full} PR #{number}: not a managed rollout")
    with tempfile.TemporaryDirectory(prefix="guideline-verify-") as tmp:
        root = Path(tmp)
        sync.cmd("git", "init", str(root))
        sync.cmd("git", "remote", "add", "origin", f"https://github.com/{full}.git", cwd=root)
        sync.fetch_remote_branch(root, target)
        sync.fetch_remote_branch(root, head)
        # The current target branch must be an ancestor of PR head, or a
        # merge may combine unrelated edits into a noncanonical document.
        ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor",
             f"origin/{target}", f"origin/{head}"), cwd=root)
        if ancestor.returncode:
            raise ValueError(f"{full} PR #{number}: head is behind {target}")
        verified_head = sync.cmd("git", "rev-parse", f"origin/{head}", cwd=root)
        verified_base = sync.cmd("git", "rev-parse", f"origin/{target}", cwd=root)
        changes = set(sync.cmd(
            "git", "diff", "--name-only", f"origin/{target}..origin/{head}",
            cwd=root).splitlines())
        expected = set()
        for entry in entries:
            dest = entry["destination"]
            before = subprocess.run(
                ("git", "show", f"origin/{target}:{dest}"),
                cwd=root, capture_output=True)
            canonical = contents[entry["source_url"]]
            if before.returncode or before.stdout != canonical:
                expected.add(dest)
            require_regular_guideline_file(root, f"origin/{head}", dest)
            actual = subprocess.run(
                ("git", "show", f"origin/{head}:{dest}"),
                cwd=root, capture_output=True)
            if actual.returncode or actual.stdout != canonical:
                raise ValueError(f"{full} PR #{number}: noncanonical document {dest}")
        if changes != expected or not changes:
            raise ValueError(
                f"{full} PR #{number}: changed files {sorted(changes)} "
                f"do not match required {sorted(expected)}")
        # Re-read the live target just before the administrative merge.
        # GitHub's --match-head-commit does not constrain the base branch.
        latest_base = sync.cmd(
            "git", "ls-remote", "origin", f"refs/heads/{target}",
            cwd=root).split()
        if len(latest_base) != 2 or latest_base[0] != verified_base:
            raise ValueError(
                f"{full} PR #{number}: {target} advanced during validation")

        # A GitHub PR base can be retargeted after validation. Do not
        # bypass required approvals/checks via --admin; normal protections
        # must authorize whichever base GitHub will actually merge.
        current_pr = sync.gh_json("pr", "view", str(number), "--repo", full,
                                  "--json", "baseRefName,headRefOid,state")
        if current_pr["baseRefName"] != target or current_pr["state"] != "OPEN":
            raise ValueError(f"{full} PR #{number}: base/state changed before merge")
        if current_pr["headRefOid"] != verified_head:
            raise ValueError(f"{full} PR #{number}: head changed before merge")
        sync.cmd("gh", "pr", "merge", str(number), "--repo", full,
                 "--merge", "--match-head-commit", verified_head)
        merged = sync.gh_json("pr", "view", str(number), "--repo", full,
                              "--json", "mergedAt,mergeCommit")
        if not merged.get("mergedAt") or not (merged.get("mergeCommit") or {}).get("oid"):
            raise ValueError(f"{full} PR #{number}: merge not confirmed")
        verify_merged_document_commit(
            root, merged["mergeCommit"]["oid"], verified_base, verified_head,
            expected, entries, contents)
    print(f"{full}: MERGED {target} PR #{number}")
    return "merged"


def rollout(repo, org, selected, contents, issue, single=False):
    name = repo["name"]
    full = f"{org}/{name}"
    holder = {}
    first = sync.process(repo, org, selected, contents, "staging", "APPLY",
                         issue, stage_pr_holder=holder,
                         require_production=single)
    if first == "blocked":
        return "blocked"
    if holder.get("number"):
        kind = holder.get('kind')
        # Read staging PR target (supports staging and aws-velocis-staging).
        staging_pr = sync.gh_json(
            "pr", "view", str(holder["number"]), "--repo", full,
            "--json", "baseRefName")
        staging_target = staging_pr["baseRefName"]
        # Identify the repository type by matching its selected guideline paths.
        # The rollout PR has already been restricted to this catalog by process().
        entries = selected.get(kind, []) if kind else []
        merge_guideline_pr(full, holder["number"], staging_target,
                           entries, contents)
    elif not holder.get("staging_current"):
        return first
    else:
        kind = holder.get('kind')
        entries = selected.get(kind, []) if kind else []

    # Recheck main only after staging is confirmed merged.
    prod_holder = {}
    second = sync.process(
        repo, org, selected, contents, "production", "APPLY",
        issue, allow_synced_staging=True, stage_pr_holder=prod_holder,
        require_production=True)
    if second == "already_synced":
        return "synced"
    if second not in ("created", "skipped_open_pr") or not prod_holder.get("number"):
        return "blocked"
    prod_pr = sync.gh_json("pr", "view", str(prod_holder["number"]),
                           "--repo", full, "--json", "baseRefName")
    if not entries:
        return "blocked"
    merge_guideline_pr(full, prod_holder["number"], prod_pr["baseRefName"],
                       entries, contents)
    return "merged"


def main():
    parser = argparse.ArgumentParser()
    for field in ("operation", "target", "repository", "phase", "file-id",
                  "source-url", "destination", "source-scope", "issue", "org"):
        parser.add_argument("--" + field, default="")
    args = parser.parse_args()
    if args.operation == "DRY_RUN" or args.phase == "production":
        sync.main()
        return
    if args.operation != "APPLY" or args.phase != "staging":
        raise ValueError("Unsupported operation")
    if os.environ.get("CONFIRMATION") != "APPLY":
        raise ValueError("APPLY confirmation required")
    if args.target not in ("single", "backend", "frontend", "all"):
        raise ValueError("Invalid target")
    if args.target == "single" and not re.fullmatch(r"[A-Za-z0-9_.-]+", args.repository):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[0-9]+", args.issue):
        raise ValueError("Invalid issue")
    catalog = json.loads((ROOT / "engineering-guidelines/catalog.json").read_text())
    selected = sync.select_files(
        catalog, args.file_id or "all", args.source_scope,
        args.source_url, args.destination)
    if args.target in ("backend", "frontend"):
        selected = {args.target: selected.get(args.target, [])}
    if not any(selected.values()):
        raise ValueError("No guidelines selected")
    contents = {entry["source_url"]: sync.source_bytes(entry["source_url"])
                for group in selected.values() for entry in group}
    if args.target == "single":
        repos = [sync.gh_json(
            "repo", "view", f"{args.org}/{args.repository}",
            "--json", "name,defaultBranchRef,isArchived,isFork")]
    else:
        repos = sync.gh_json("repo", "list", args.org, "--limit", "1000",
                             "--json", "name,defaultBranchRef,isArchived,isFork")
    counts = {}
    for repo in repos:
        if repo.get("isArchived") or repo.get("isFork"):
            continue
        name = repo["name"]
        try:
            outcome = rollout(repo, args.org, selected, contents, args.issue,
                              single=(args.target == "single"))
        except Exception as error:
            outcome = "error"
            print(f"{name}: ERROR {error}", file=sys.stderr)
        counts[outcome] = counts.get(outcome, 0) + 1
        print(f"{name}: {outcome}")
    print("SUMMARY " + json.dumps(counts, sort_keys=True))
    if counts.get("error") or counts.get("blocked") or (
            args.target == "single" and counts.get("skipped")):
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print("ERROR:", error, file=sys.stderr)
        sys.exit(1)
