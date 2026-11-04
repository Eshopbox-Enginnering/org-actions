#!/usr/bin/env python3
"""Reconcile merged, machine-tagged staging guideline PRs into reviewed main PRs.

Runs centrally in org-actions; no per-repository GitHub workflow installation.
Never merges pull requests or changes target branches directly.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

ORG = os.environ.get("ORG", "Eshopbox-Enginnering")
MARKER = re.compile(r"<!-- eshopbox-guideline-sync:v1 (\{[^\n]*\}) -->")
ALLOWED = {"file_id", "source_url", "destination", "source_scope", "issue", "kind", "digest"}


def gh_api(path):
    run = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(run.stderr.strip()[:500])
    return json.loads(run.stdout)


def decode_metadata(body):
    match = MARKER.search(body or "")
    if not match:
        return None
    value = json.loads(match.group(1))
    if not isinstance(value, dict) or not ALLOWED.issuperset(value):
        return None
    if not all(isinstance(v, str) for v in value.values()):
        return None
    if not re.fullmatch(r"[0-9]+", value.get("issue", "")):
        return None
    if value.get("kind") not in ("backend", "frontend"):
        return None
    if not re.fullmatch(r"[0-9a-f]{12}", value.get("digest", "")):
        return None
    if not value.get("file_id"):
        return None
    return value


def eligible(pr, repository):
    meta = decode_metadata(pr.get("body"))
    if not meta or not pr.get("merged_at"):
        return None
    if pr.get("base", {}).get("ref") not in ("staging", "aws-velocis-staging"):
        return None
    head = pr.get("head", {}).get("ref", "")
    prefix = f"org-action/guidelines-{meta['kind']}-{meta['digest']}-"
    if not head.startswith(prefix):
        return None
    if not head[len(prefix):].isalnum():
        return None
    if (pr.get("head", {}).get("repo") or {}).get("full_name") not in (None, repository):
        return None
    return meta


def recent_prs():
    # Search narrows to tagged, merged PRs and prevents scans over all repos.
    since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    query = f'org:{ORG} is:pr is:merged in:body "eshopbox-guideline-sync:v1" merged:>={since}'
    results = gh_api("search/issues?q=" + quote(query) + "&per_page=100&sort=updated&order=desc")
    return results.get("items", [])


def promote_one(repository, number, meta):
    arguments = [
        sys.executable, "scripts/sync-engineering-guidelines.py",
        "--operation", "APPLY", "--target", "single",
        "--repository", repository.split("/", 1)[1],
        "--phase", "production", "--file-id", meta["file_id"],
        "--source-url", meta.get("source_url", ""),
        "--destination", meta.get("destination", ""),
        "--source-scope", meta["source_scope"],
        "--issue", meta["issue"], "--org", ORG,
        "--promotion-pr", str(number),
    ]
    result = subprocess.run(arguments, text=True, capture_output=True,
                            env={**os.environ, "CONFIRMATION": "APPLY"})
    print(f"{repository} staging PR #{number}:\n{result.stdout}", flush=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def main():
    errors = 0
    for item in recent_prs():
        repository = item.get("repository_url", "").split("/repos/", 1)[-1]
        number = item.get("number")
        if not number or not repository.startswith(ORG + "/"):
            continue
        try:
            pr = gh_api(f"repos/{repository}/pulls/{number}")
            metadata = eligible(pr, repository)
            if metadata is None:
                continue
            # Do not act on a stale catalog or changed source content.
            # The promotion script revalidates staging and recomputes hashes.
            errors += promote_one(repository, number, metadata) != 0
        except Exception as error:
            errors += 1
            print(f"{repository} PR #{number}: {error}", file=sys.stderr)
    if errors:
        print(f"{errors} promotion(s) need operator attention", file=sys.stderr)
        sys.exit(1)
    print("All eligible merged guideline PRs have been reconciled.")


if __name__ == "__main__":
    main()
