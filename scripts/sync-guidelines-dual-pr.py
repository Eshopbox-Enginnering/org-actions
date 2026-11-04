#!/usr/bin/env python3
"""Create staging and production guideline PRs together, without merging either."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "guideline_sync", ROOT / "scripts/sync-engineering-guidelines.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def main():
    parser = argparse.ArgumentParser()
    for field in ('operation', 'target', 'repository', 'phase', 'file-id',
                  'source-url', 'destination', 'source-scope', 'issue', 'org'):
        parser.add_argument('--' + field, default='')
    args = parser.parse_args()
    if args.operation == 'DRY_RUN' or args.phase == 'production':
        sync.main()
        return
    if args.operation != 'APPLY' or args.phase != 'staging':
        raise ValueError('Invalid workflow mode')
    if os.environ.get('CONFIRMATION') != 'APPLY':
        raise ValueError('APPLY confirmation missing')
    if args.target not in ('single', 'backend', 'frontend', 'all'):
        raise ValueError('Invalid target')
    if args.target == 'single' and not re.fullmatch(r'[A-Za-z0-9_.-]+', args.repository):
        raise ValueError('Invalid repository')
    if not re.fullmatch(r'[0-9]+', args.issue):
        raise ValueError('Invalid issue')

    catalog = json.loads((ROOT / 'engineering-guidelines/catalog.json').read_text())
    selected = sync.select_files(
        catalog, args.file_id or 'all', args.source_scope,
        args.source_url, args.destination)
    if args.target in ('backend', 'frontend'):
        selected = {args.target: selected.get(args.target, [])}
    if not any(selected.values()):
        raise ValueError('No selected guidelines')
    contents = {entry['source_url']: sync.source_bytes(entry['source_url'])
                for entries in selected.values() for entry in entries}
    if args.target == 'single':
        repos = [sync.gh_json('repo', 'view', f'{args.org}/{args.repository}',
                              '--json', 'name,defaultBranchRef,isArchived,isFork')]
    else:
        repos = sync.gh_json('repo', 'list', args.org, '--limit', '1000',
                             '--json', 'name,defaultBranchRef,isArchived,isFork')

    results = {}
    for repo in repos:
        if repo.get('isArchived') or repo.get('isFork'):
            continue
        name = repo['name']
        try:
            holder = {}
            first = sync.process(repo, args.org, selected, contents, 'staging',
                                 'APPLY', args.issue, stage_pr_holder=holder)
            if holder.get('number'):
                second = sync.process(
                    repo, args.org, selected, contents, 'production',
                    'APPLY', args.issue, promotion_pr=holder['number'],
                    allow_open_staging=True)
                result = ('both_prs_ready' if second in
                          ('created', 'skipped_open_pr', 'already_synced') else 'blocked')
            elif holder.get('staging_current'):
                second = sync.process(
                    repo, args.org, selected, contents, 'production',
                    'APPLY', args.issue, allow_synced_staging=True)
                result = ('both_branches_synced_or_pr_ready' if second in
                          ('created', 'skipped_open_pr', 'already_synced')
                          else 'blocked')
            else:
                result = first
        except Exception as error:
            result = 'error'
            print(f"{name}: ERROR {error}", file=sys.stderr)
        results[result] = results.get(result, 0) + 1
    print('SUMMARY ' + json.dumps(results, sort_keys=True))
    if results.get('error') or results.get('blocked'):
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(1)
