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
    # PHP backend projects often bundle Vite-generated frontend assets.
    # Treat composer.json as an explicit backend signal, retaining the
    # previous frontend-first classification for other mixed projects.
    if (root / 'composer.json').exists():
        return 'backend'
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
                  '--state', state, '--json', 'number,url,state,mergedAt', '--limit', '100')
    return prs[0] if prs else None


def fetch_remote_branch(root, branch):
    # A single-branch shallow clone does not populate origin/<other-branch>
    # when fetching a branch by name. Install its remote-tracking ref explicitly.
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', branch) or '..' in branch:
        raise ValueError('Invalid remote branch')
    refspec = f'+refs/heads/{branch}:refs/remotes/origin/{branch}'
    cmd('git', 'fetch', '--depth=100', 'origin', refspec, cwd=root)


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


def find_merged_staging_candidates(full, staging, branch_prefix, promotion_pr=None, allow_open=False):
    if promotion_pr is not None:
        # Explicit numbered lookup survives more than 100 staging PRs.
        exact = gh_json('pr', 'view', str(promotion_pr), '--repo', full,
                        '--json', 'number,headRefName,baseRefName,mergedAt,url')
        if ((allow_open or exact.get('mergedAt')) and exact.get('baseRefName') == staging
                and exact.get('headRefName', '').startswith(branch_prefix)):
            return [exact]
        return []
    merged = gh_json('pr', 'list', '--repo', full, '--base', staging,
                     '--state', 'merged', '--json',
                     'number,headRefName,mergedAt,url', '--limit', '100')
    return [item for item in merged
            if item['headRefName'].startswith(branch_prefix)]


def production_pr_covers_pending_edits(changed_paths, edits):
    """Only reuse an open main PR whose file scope is still complete."""
    pending = {dest for dest, _, _, _ in edits}
    return bool(changed_paths) and set(changed_paths) == pending


def find_existing_production_guideline_pr(full, production, kind, digest):
    """Find an open production PR for this exact guideline content."""
    prs = gh_json('pr', 'list', '--repo', full, '--base', production,
                  '--state', 'open', '--json', 'number,headRefName,url',
                  '--limit', '100')
    pattern = re.compile(
        rf'^org-action/guidelines-prod-{re.escape(kind)}-'
        rf'{re.escape(digest)}-(?:pr[0-9]+|synced)$')
    matches = [pr for pr in prs if pattern.fullmatch(pr['headRefName'])]
    if len(matches) > 1:
        raise ValueError('Multiple open production guideline PRs for the same content')
    return matches[0] if matches else None


def process(repo, org, selected, contents, phase, operation, issue,
            promotion_pr=None, allow_open_staging=False,
            stage_pr_holder=None, require_production=False,
            allow_synced_staging=False):
    name = repo['name']
    production = (repo.get('defaultBranchRef') or {}).get('name', '')
    if production not in ALLOWED_BRANCHES:
        print(f'{name}: BLOCK unsupported production branch {production}')
        return 'blocked' if promotion_pr is not None else 'skipped'
    with tempfile.TemporaryDirectory(prefix='guidelines-') as temp:
        root = Path(temp) / name
        full = f'{org}/{name}'
        cmd('gh', 'repo', 'clone', full, str(root), '--', '--depth=1', '--branch', production)
        kind = detect_type(root)
        if kind not in selected or not selected[kind]:
            print(f'{name}: BLOCK type={kind}, no matching selected guidelines')
            return 'blocked' if (promotion_pr is not None or require_production) else 'skipped'
        entries = selected[kind]
        if stage_pr_holder is not None:
            stage_pr_holder['kind'] = kind
        staging = find_staging(root, production)
        if not staging:
            print(f'{name}: BLOCK no supported staging branch')
            return 'blocked'
        if phase == 'staging':
            # Never bring production-only code into a staging PR.
            fetch_remote_branch(root, staging)
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
            if phase == 'staging' and stage_pr_holder is not None:
                # No new staging PR needed. The orchestrator must still check
                # whether production requires a separate guidelines PR.
                stage_pr_holder['staging_current'] = True
            return 'already_synced' if (promotion_pr is not None or require_production) else 'skipped'

         # Stable branch across staging + production runs, including retries.
        digest = hashlib.sha256(b''.join(
            e['destination'].encode() + b'\0' + contents[e['source_url']]
            for e in sorted(entries, key=lambda x: x['destination'])
        )).hexdigest()[:12]
        branch_prefix = f'org-action/guidelines-{kind}-{digest}-'
        target_base = staging if phase == 'staging' else production
        base_sha = cmd('git', 'rev-parse', 'HEAD', cwd=root)[:10]
        if phase == 'staging':
            branch = branch_prefix + base_sha
        else:
            # A production PR must be based on production, not on the
            # staging rollout branch (which can include unrelated commits).
            if allow_synced_staging:
                # No staging changes are required. Production may still lag.
                # Verify all selected canonical bytes on the staging branch
                # before creating the independently reviewable main PR.
                fetch_remote_branch(root, staging)
                check_ref = f'origin/{staging}'
                staging_pr = None
            else:
                candidates = find_merged_staging_candidates(
                    full, staging, branch_prefix, promotion_pr,
                    allow_open=allow_open_staging)
                if not candidates:
                    print(f'{name}: BLOCK matching guideline staging PR not found')
                    return 'blocked'
                candidates.sort(key=lambda item: item['mergedAt'] or '', reverse=True)
                staging_pr = candidates[0]
            if allow_open_staging and not allow_synced_staging:
                head_branch = staging_pr['headRefName']
                if not cmd('git', 'ls-remote', '--heads', 'origin', head_branch,
                           cwd=root, check=False):
                    return 'blocked'
                fetch_remote_branch(root, staging)
                fetch_remote_branch(root, head_branch)
                base = cmd('git', 'merge-base', f'origin/{staging}',
                           f'origin/{head_branch}', cwd=root)
                changes = set(cmd('git', 'diff', '--name-only', base,
                                  f'origin/{head_branch}', cwd=root).splitlines())
                # Catalog entries already canonical on staging are not
                # changed by the staging PR. Require exactly the remaining
                # managed paths, never the entire selected catalog.
                expected = set()
                for entry in entries:
                    dest = entry['destination']
                    existing = subprocess.run(
                        ('git', 'show', f'origin/{staging}:{dest}'),
                        cwd=root, capture_output=True)
                    if (existing.returncode or
                            existing.stdout != contents[entry['source_url']]):
                        expected.add(dest)
                if not expected or changes != expected:
                    print(f'{name}: BLOCK staging PR file scope mismatch: '
                          f'actual={sorted(changes)} expected={sorted(expected)}')
                    return 'blocked'
                check_ref = f'origin/{head_branch}'
            elif not allow_synced_staging:
                fetch_remote_branch(root, staging)
                check_ref = f'origin/{staging}'
            for entry in entries:
                destination = entry['destination']
                staged = subprocess.run(
                    ('git', 'show', f'{check_ref}:{destination}'),
                    cwd=root, capture_output=True)
                if staged.returncode or staged.stdout != contents[entry['source_url']]:
                    print(f'{name}: BLOCK staged guideline content differs: {destination}')
                    return 'blocked'
            if allow_synced_staging:
                # A previous invocation may have opened the production PR
                # while staging was still pending. Reuse that same PR.
                prior = find_existing_production_guideline_pr(
                    full, production, kind, digest)
                if prior:
                    prior_branch = prior['headRefName']
                    fetch_remote_branch(root, prior_branch)
                    base = cmd('git', 'merge-base', f'origin/{production}',
                               f'origin/{prior_branch}', cwd=root)
                    changed = set(cmd('git', 'diff', '--name-only', base,
                                      f'origin/{prior_branch}', cwd=root).splitlines())
                    # The PR must update *every* file that is currently
                    # noncanonical on production. An old PR can have a valid
                    # payload but omit a document changed on main after it
                    # was opened; reusing it would silently leave drift.
                    if not production_pr_covers_pending_edits(changed, edits):
                        pending = {dest for dest, _, _, _ in edits}
                        print(f'{name}: BLOCK existing production PR scope '
                              f'no longer matches pending edits: '
                              f'PR={sorted(changed)} pending={sorted(pending)}')
                        return 'blocked'
                    for entry in entries:
                        dest = entry['destination']
                        actual = subprocess.run(
                            ('git', 'show', f'origin/{prior_branch}:{dest}'),
                            cwd=root, capture_output=True)
                        if actual.returncode or actual.stdout != contents[entry['source_url']]:
                            print(f'{name}: BLOCK existing production PR content mismatch: {dest}')
                            return 'blocked'
                    print(f"{name}: EXISTING OPEN PR {prior['url']}")
                    if stage_pr_holder is not None:
                        stage_pr_holder['number'] = prior['number']
                    return 'skipped_open_pr'
            # The production base can drift after a completed rollout.
            # A base-specific catch-up branch is safe to retry, while a
            # new base SHA yields a fresh PR for later reconciliation.
            branch = (f'org-action/guidelines-prod-{kind}-{digest}-synced-{base_sha}'
                      if allow_synced_staging else
                      f'org-action/guidelines-prod-{kind}-{digest}-pr{staging_pr["number"]}')
            # Idempotency across changes to the main branch: lookup by merged
            # staging PR identity rather than the moving production SHA.
        existing_pr = find_pr(full, target_base, branch)
        if existing_pr:
            print(f"{name}: EXISTING OPEN PR {existing_pr['url']}")
            if stage_pr_holder is not None:
                stage_pr_holder['number'] = existing_pr['number']
            return 'skipped_open_pr' if promotion_pr is not None else 'skipped'
        # Reusing an old historical branch would hide drift. A new staging
        # base SHA normally yields a fresh branch; fail closed if it does not.
        historical_pr = find_pr(full, target_base, branch, state='all')
        if historical_pr:
            print(f'{name}: BLOCK historical PR already used this exact branch: {branch}')
            return 'blocked'

        branch_exists = bool(cmd('git', 'ls-remote', '--heads', 'origin', branch,
                                 cwd=root, check=False))
        if branch_exists:
            if phase != 'production':
                print(f'{name}: BLOCK existing staging branch without PR: {branch}')
                return 'blocked'
            fetch_remote_branch(root, branch)
            # An interrupted production PR creation is safe to resume only
            # when the branch is exactly the production-based intended diff.
            base = cmd('git', 'merge-base', f'origin/{production}',
                       f'origin/{branch}', cwd=root)
            diff_names = cmd('git', 'diff', '--name-only', base,
                             f'origin/{branch}', cwd=root).splitlines()
            if set(diff_names) != {item[0] for item in edits}:
                print(f'{name}: BLOCK existing production branch differs in file scope')
                return 'blocked'
            for dest, _, _, data in edits:
                actual = subprocess.run(('git', 'show', f'origin/{branch}:{dest}'),
                                        cwd=root, capture_output=True)
                if actual.returncode or actual.stdout != data:
                    print(f'{name}: BLOCK existing production branch content mismatch: {dest}')
                    return 'blocked'
            if cmd('git', 'rev-list', '--count', f'{base}..origin/{branch}', cwd=root) != '1':
                print(f'{name}: BLOCK existing production branch has unexpected commits')
                return 'blocked'
            print(f'{name}: RESUME verified production branch {branch}')
        else:
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

        body = ('Central Backlog: https://github.com/' + org + '/Central-Backlog/issues/' + issue
                + '\n\nCentral guideline sync, type: ' + kind + ', phase: ' + phase + '.\n\n'
                + '\n'.join(f'- {action}: `{dest}` (SHA256 {sha})' for dest, action, sha, _ in edits)
                + '\n\nOnly selected docs/engineering-guidelines files may change. Automated guideline rollout; only selected files may change.')
        if phase == 'staging':
            details = {
                'file_id': next((entry['id'] for entry in entries), 'all') if len(entries) == 1 else 'all',
                'source_url': entries[0]['source_url'] if len(entries) == 1 and entries[0]['id'] == 'custom' else '',
                'destination': entries[0]['destination'] if len(entries) == 1 and entries[0]['id'] == 'custom' else '',
                'source_scope': kind,
                'issue': issue,
                'kind': kind,
                'digest': digest,
            }
            body += '\n\n<!-- eshopbox-guideline-sync:v1 ' + json.dumps(details, sort_keys=True) + ' -->'
        else:
            body += (f'\n\nStaging PR: {staging_pr["url"]}' if staging_pr
                     else '\n\nStaging already contains all selected canonical guidelines.')
        url = cmd('gh', 'pr', 'create', '--repo', full, '--base', target_base,
                  '--head', branch, '--title', f'feature to {target_base}: Sync engineering guidelines (#{issue})',
                  '--body', body)
        print(f'{name}: CREATED PR {url}')
        if stage_pr_holder is not None:
            stage_pr_holder['number'] = int(url.rstrip('/').rsplit('/', 1)[-1])
        return 'created'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--promotion-pr', type=int, default=None)
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
            if args.promotion_pr is not None:
                counts['blocked'] = counts.get('blocked', 0) + 1
                print(f"{repo['name']}: BLOCK archived or forked during exact promotion")
            continue
        try:
            outcome = process(repo, args.org, selected, contents, args.phase,
                              args.operation, args.issue, promotion_pr=args.promotion_pr)
        except Exception as error:
            outcome = 'error'
            print(f"{repo['name']}: ERROR {error}", file=sys.stderr)
        counts[outcome] = counts.get(outcome, 0) + 1
    print('SUMMARY ' + json.dumps(counts, sort_keys=True))
    if counts.get('error') or counts.get('blocked') or (
            args.promotion_pr is not None and not
            (counts.get('created') or counts.get('already_synced') or
             counts.get('skipped_open_pr'))):
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(1)
