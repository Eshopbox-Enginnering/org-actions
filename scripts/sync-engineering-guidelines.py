#!/usr/bin/env python3
"""Review-only, create-or-replace Markdown guidelines in selected repositories."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_BRANCHES = ('main', 'master', 'aws-velocis-main')
MAX_BYTES = 1024 * 1024


def cmd(*args, cwd=None, check=True):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args[:4])}: {result.stderr.strip()[:700]}")
    return result.stdout.strip()


def validate_destination(dest):
    p = PurePosixPath(dest)
    if (not dest.startswith('docs/engineering-guidelines/') or p.is_absolute()
            or '..' in p.parts or not dest.endswith('.md')
            or not re.fullmatch(r'[A-Za-z0-9_./-]+', dest) or len(p.parts) != 3):
        raise ValueError(f'Unsafe destination: {dest}')
    return dest


def validate_source(source):
    if source.startswith('engineering-guidelines/'):
        path = (ROOT / source).resolve()
        if not path.is_relative_to((ROOT / 'engineering-guidelines').resolve()) or path.suffix != '.md':
            raise ValueError('Unsafe local guideline path')
        return 'file', path
    url = urlsplit(source)
    if (url.scheme != 'https' or url.hostname != 'github.com' or url.username
            or url.password or url.query or url.fragment
            or not re.fullmatch(r'/user-attachments/files/[0-9]+/[A-Za-z0-9_.-]+\.md', url.path)):
        raise ValueError('Source must be a local engineering-guidelines/ Markdown file or GitHub issue attachment')
    return 'url', source


def source_bytes(source):
    kind, location = validate_source(source)
    if kind == 'file':
        data = location.read_bytes()
    else:
        # The GitHub App token is never passed to the attachment host.
        with urlopen(Request(location, headers={'User-Agent': 'Engineering-Guideline-Sync/1.0'}), timeout=30) as response:
            redirected = urlsplit(response.geturl())
            host = redirected.hostname or ''
            approved = (host == 'github.com' or host == 'objects.githubusercontent.com'
                        or host.endswith('.githubusercontent.com')
                        or re.fullmatch(r'github-production-user-asset-[a-z0-9]+\.s3\.amazonaws\.com', host))
            if redirected.scheme != 'https' or not approved:
                raise ValueError('Attachment redirected to an unapproved host')
            data = response.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES or b'\0' in data:
        raise ValueError('Source is empty, binary or larger than 1 MiB')
    data.decode('utf-8')
    return data


def select_files(catalog, file_id, scope, source, dest):
    if bool(source) != bool(dest):
        raise ValueError('Supply both source_url and destination for a one-off file')
    if source:
        if scope not in ('backend', 'frontend'):
            raise ValueError('A one-off file requires backend or frontend source_scope')
        validate_source(source)
        validate_destination(dest)
        return {scope: [{'id': 'custom', 'source_url': source, 'destination': dest}]}
    selected = {k: [f for f in catalog[k] if file_id == 'all' or f['id'] == file_id]
                for k in ('backend', 'frontend')}
    if not any(selected.values()):
        raise ValueError('Unknown catalog ID or empty catalog')
    for entries in selected.values():
        paths = set()
        for entry in entries:
            validate_source(entry['source_url'])
            validate_destination(entry['destination'])
            if entry['destination'] in paths:
                raise ValueError('Duplicate guideline destination')
            paths.add(entry['destination'])
    return selected


def detect_type(root):
    frontend = ('angular.json', 'vite.config.js', 'vite.config.ts', 'vite.config.mjs',
                'vue.config.js', 'next.config.js', 'next.config.ts', 'next.config.mjs')
    backend = ('pom.xml', 'build.gradle', 'build.gradle.kts', 'go.mod',
               'requirements.txt', 'pyproject.toml', 'composer.json')
    if any((root / name).exists() for name in frontend):
        return 'frontend'
    if any((root / name).exists() for name in backend):
        return 'backend'
    pkg = root / 'package.json'
    if pkg.exists():
        contents = pkg.read_text(errors='replace')
        # Match update-agents.sh: backend wins for mixed Node projects.
        if any(x in contents for x in ('"express"', '"fastify"', '"koa"', '"nestjs"', '"@nestjs/core"')):
            return 'backend'
        if any(x in contents for x in ('"react"', '"react-dom"', '"@angular/core"', '"vue"', '"next"', '"vite"')):
            return 'frontend'
    return 'unknown'


def diff_plan(root, entries, contents):
    plan = []
    for entry in entries:
        dest = validate_destination(entry['destination'])
        target = root / dest
        if target.is_symlink() or any(x.is_symlink() for x in (target.parent, target.parent.parent)):
            raise ValueError('Refusing symlink at managed path')
        before = target.read_bytes() if target.exists() else None
        after = contents[entry['source_url']]
        action = 'CREATE' if before is None else 'SKIP' if before == after else 'REPLACE'
        plan.append((dest, action, hashlib.sha256(after).hexdigest()[:12], after))
    return plan


def gh_json(*args):
    return json.loads(cmd('gh', *args))


def find_pr(repo, base, branch, state='open'):
    prs = gh_json('pr', 'list', '--repo', repo, '--base', base, '--head', branch,
                  '--state', state, '--json', 'url,state,mergedAt', '--limit', '100')
    return prs[0] if prs else None


def find_staging(root, production):
    choices = ('aws-velocis-staging', 'staging') if production == 'aws-velocis-main' else ('staging', 'aws-velocis-staging')
    for name in choices:
        if cmd('git', 'ls-remote', '--heads', 'origin', name, cwd=root, check=False):
            return name
    return None


def preview(before, after, dest):
    if before == after:
        return []
    delta = list(difflib.unified_diff(
        before.decode('utf-8').splitlines(keepends=True),
        after.decode('utf-8').splitlines(keepends=True),
        fromfile=dest + ' (existing)', tofile=dest + ' (central)'))
    # splitlines(keepends=True) retains CRLF and terminal newline distinctions.
    return delta or [f'Byte-only difference for {dest}: {len(before)} -> {len(after)} bytes\n']


def process(repo, org, selected, contents, phase, operation, issue):
    name = repo['name']
    production = (repo.get('defaultBranchRef') or {}).get('name', '')
    if production not in ALLOWED_BRANCHES:
        print(f'{name}: SKIP unsupported production branch {production}')
        return 'skipped'
    with tempfile.TemporaryDirectory(prefix='guidelines-') as temp:
        root = Path(temp) / name
        full = f'{org}/{name}'
        cmd('gh', 'repo', 'clone', full, str(root), '--', '--depth=1', '--branch', production)
        kind = detect_type(root)
        if kind not in selected or not selected[kind]:
            print(f'{name}: SKIP type={kind}, no matching selected guidelines')
            return 'skipped'
        entries = selected[kind]
        staging = find_staging(root, production)
        if not staging:
            print(f'{name}: BLOCK no supported staging branch')
            return 'blocked'
        if phase == 'staging':
            # Never bring production-only code into a staging PR.
            cmd('git', 'fetch', 'origin', staging, '--depth=100', cwd=root)
            cmd('git', 'checkout', '-B', 'guidelines-staging-base', f'origin/{staging}', cwd=root)
        plan = diff_plan(root, entries, contents)
        edits = [item for item in plan if item[1] != 'SKIP']
        for dest, action, sha, _ in plan:
            print(f'{name}: {action} {dest} sha256={sha}')
        if operation == 'DRY_RUN':
            for dest, action, _, after in edits:
                target = root / dest
                before = target.read_bytes() if target.exists() else b''
                diff = preview(before, after, dest)
                print(''.join(diff[:120]))
                if len(diff) > 120:
                    print(f'... {len(diff) - 120} more diff lines omitted')
            return 'planned' if edits else 'skipped'
        if not edits:
            return 'skipped'

         # Stable branch across staging + production runs, including retries.
        digest = hashlib.sha256(b''.join(
            e['destination'].encode() + b'\0' + contents[e['source_url']]
            for e in sorted(entries, key=lambda x: x['destination'])
        )).hexdigest()[:12]
        branch = f'org-action/guidelines-{kind}-{digest}'
        target_base = staging if phase == 'staging' else production
        if phase == 'production':
            staging_pr = find_pr(full, staging, branch, state='merged')
            if not staging_pr or not staging_pr['mergedAt']:
                print(f'{name}: BLOCK staging PR for {branch} has not merged')
                return 'blocked'
            # Verify every selected file is actually staged with current canonical
            # bytes before producing a PR against production.
            cmd('git', 'fetch', 'origin', staging, '--depth=100', cwd=root)
            for entry in entries:
                destination = entry['destination']
                staged = subprocess.run(
                    ('git', 'show', f'origin/{staging}:{destination}'),
                    cwd=root, capture_output=True)
                if staged.returncode or staged.stdout != contents[entry['source_url']]:
                    print(f'{name}: BLOCK staging file is missing or differs from canonical: {destination}')
                    return 'blocked'
            # Production head already exists, but must represent all desired edits.
            branch_exists = bool(cmd('git', 'ls-remote', '--heads', 'origin', branch, cwd=root, check=False))
            if not branch_exists:
                print(f'{name}: BLOCK staged source branch not available: {branch}')
                return 'blocked'
            cmd('git', 'fetch', 'origin', branch, '--depth=100', cwd=root)
            for entry in entries:
                destination = entry['destination']
                head = subprocess.run(('git', 'show', f'origin/{branch}:{destination}'),
                                      cwd=root, capture_output=True)
                if head.returncode or head.stdout != contents[entry['source_url']]:
                    print(f'{name}: BLOCK production PR head does not contain canonical file: {destination}')
                    return 'blocked'
        existing_pr = find_pr(full, target_base, branch)
        if existing_pr:
            print(f"{name}: EXISTING OPEN PR {existing_pr['url']}")
            return 'skipped'
        # Previously merged or closed PRs do not count as current synchronization.
        historical_pr = find_pr(full, target_base, branch, state='all')
        if historical_pr:
            print(f'{name}: BLOCK historical branch name must be renewed after drift: {branch}')
            return 'blocked'

        branch_exists = bool(cmd('git', 'ls-remote', '--heads', 'origin', branch, cwd=root, check=False))
        if phase == 'staging':
            if branch_exists:
                print(f'{name}: BLOCK existing branch without PR needs manual inspection: {branch}')
                return 'blocked'
            cmd('git', 'checkout', '-b', branch, cwd=root)
            for dest, _, _, data in edits:
                path = root / dest
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                cmd('git', 'add', '--', dest, cwd=root)
            cmd('git', '-c', 'user.name=eshopbox-guidelines[bot]',
                '-c', 'user.email=eshopbox-guidelines[bot]@users.noreply.github.com',
                'commit', '-m', f'docs: sync {kind} engineering guidelines', cwd=root)
            cmd('git', 'push', '-u', 'origin', branch, cwd=root)
        elif not branch_exists:
            print(f'{name}: BLOCK staged branch was deleted; restore it from staging PR before production')
            return 'blocked'

        body = ('Central Backlog: https://github.com/' + org + '/Central-Backlog/issues/' + issue
                + '\n\nCentral guideline sync, type: ' + kind + ', phase: ' + phase + '.\n\n'
                + '\n'.join(f'- {action}: `{dest}` (SHA256 {sha})' for dest, action, sha, _ in edits)
                + '\n\nOnly selected docs/engineering-guidelines files may change. Manual review and merge required.')
        url = cmd('gh', 'pr', 'create', '--repo', full, '--base', target_base,
                  '--head', branch, '--title', f'docs to {"Staging" if phase == "staging" else "Main"}: Sync engineering guidelines (#{issue})',
                  '--body', body)
        print(f'{name}: CREATED PR {url}')
        return 'created'


def main():
    parser = argparse.ArgumentParser()
    for field in ('operation', 'target', 'repository', 'phase', 'file-id',
                  'source-url', 'destination', 'source-scope', 'issue', 'org'):
        parser.add_argument('--' + field, default='')
    args = parser.parse_args()
    if (args.operation not in ('DRY_RUN', 'APPLY') or
            args.target not in ('single', 'backend', 'frontend', 'all') or
            args.phase not in ('staging', 'production')):
        raise ValueError('Invalid operation, target or phase')
    if not re.fullmatch(r'[0-9]+', args.issue):
        raise ValueError('Central Backlog issue must be numeric')
    if args.target == 'single' and not re.fullmatch(r'[A-Za-z0-9_.-]+', args.repository):
        raise ValueError('Invalid or missing single repository name')
    if args.operation == 'APPLY' and os.environ.get('CONFIRMATION') != 'APPLY':
        raise ValueError('APPLY requires confirmation APPLY')

    catalog = json.loads((ROOT / 'engineering-guidelines/catalog.json').read_text())
    selected = select_files(catalog, args.file_id or 'all', args.source_scope,
                            args.source_url, args.destination)
    if args.target in ('backend', 'frontend'):
        selected = {args.target: selected.get(args.target, [])}
    if not any(selected.values()):
        raise ValueError('No guidelines selected for the requested target')
    contents = {e['source_url']: source_bytes(e['source_url'])
                for entries in selected.values() for e in entries}
    print(f'Verified {len(contents)} sources, total bytes {sum(len(v) for v in contents.values())}')
    if args.target == 'single':
        repos = [gh_json('repo', 'view', f'{args.org}/{args.repository}',
                         '--json', 'name,defaultBranchRef,isArchived,isFork')]
    else:
        repos = gh_json('repo', 'list', args.org, '--limit', '1000',
                        '--json', 'name,defaultBranchRef,isArchived,isFork')
    counts = {}
    for repo in repos:
        if repo.get('isArchived') or repo.get('isFork'):
            continue
        try:
            outcome = process(repo, args.org, selected, contents, args.phase,
                              args.operation, args.issue)
        except Exception as error:
            outcome = 'error'
            print(f"{repo['name']}: ERROR {error}", file=sys.stderr)
        counts[outcome] = counts.get(outcome, 0) + 1
    print('SUMMARY ' + json.dumps(counts, sort_keys=True))
    if counts.get('error') or counts.get('blocked'):
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(1)
