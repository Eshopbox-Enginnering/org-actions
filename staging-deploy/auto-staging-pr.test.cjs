'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { deploy, nextStagingTag, normalizeTagTargets, selectPromotionTag, activeChangeRequests, conflictMessage, checkReady, promotionSnapshot, hasWorkflowDispatch } = require('./auto-staging-pr.cjs');
const { verifyChecks, requiredWorkflowMatches, verifyCodex, verifySecurity, resolveWorkflowSources } = require('./auto-staging-deploy.cjs');

test('staging bot posts one readable progress comment and edits it on failure', async () => {
  const calls = [];
  const github = {
    request: async (route, args) => {
      calls.push({ route, args });
      if (route === 'POST /repos/{owner}/{repo}/issues/{issue_number}/comments') {
        return { data: { id: 742 } };
      }
      if (route === 'PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}') return { data: {} };
      if (route === 'GET /repos/{owner}/{repo}/collaborators/{username}/permission') {
        return { data: { permission: 'read' } };
      }
      throw new Error('Unexpected request: ' + route);
    },
  };
  const errors = [];
  await deploy({
    github,
    context: {
      eventName: 'issue_comment', runId: 43230110279, repo: { owner: 'org', repo: 'repo' },
      payload: { action: 'created', issue: { number: 452, pull_request: {} },
        comment: { id: 123, body: '/deploy-staging', user: { login: 'dev' } } },
    },
    core: { setFailed: x => errors.push(x), setOutput: () => {} },
  });
  const created = calls.filter(c => c.route === 'POST /repos/{owner}/{repo}/issues/{issue_number}/comments');
  const edited = calls.filter(c => c.route === 'PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}');
  assert.equal(created.length, 1);
  assert.equal(edited.length, 1);
  assert.equal(edited[0].args.comment_id, 742);
  assert.match(created[0].args.body, /⏳ Staging deployment — In progress/);
  assert.match(edited[0].args.body, /❌ Staging deployment — Failed/);
  assert.match(edited[0].args.body, /\*\*Step:\*\* Validating checks and reviews/);
  assert.match(edited[0].args.body, /github.com\/org\/repo\/actions\/runs\/43230110279/);
  assert.match(edited[0].args.body, /needs write access/);
  assert.doesNotMatch(edited[0].args.body, /\\\\n/);
  assert.equal(errors.length, 1);
});

test('staging status progresses through validation, merge, deployment and verification', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const source = fs.readFileSync(path.join(__dirname, 'auto-staging-pr.cjs'), 'utf8');
  for (const step of [
    'Validating checks and reviews',
    'Preparing staging promotion',
    'Validating and merging staging PR',
    'Creating deployment tag',
    'Deploying to staging',
    'Refreshing required staging-presence check',
  ]) assert(source.includes(step), step);
  assert(source.includes("await report('success'"));
  assert(source.includes("await report('failure'"));
});


test('App Octokit creates and merges staging PR without gh and reports successful deployment', async () => {
  const head = 'a'.repeat(40), originalStaging = 'b'.repeat(40);
  const mergedSHA = 'c'.repeat(40), tree = 'd'.repeat(40);
  const actions = [], appActions = [];
  const pr = {
    number: 42, state: 'open', draft: false, html_url: 'https://github.com/org/repo/pull/42',
    base: { ref: 'main' }, head: { sha: head, ref: 'feature/7319-smoke', repo: { full_name: 'org/repo' } },
    user: { login: 'author' },
  };
  const stage = {
    number: 66, state: 'open', html_url: 'https://github.com/org/repo/pull/66',
    base: { ref: 'staging' }, head: { sha: head }, merged_at: null,
  };
  const deployment = {
    id: 77, head_sha: mergedSHA, head_branch: 'dev-v1.1.25',
    status: 'completed', conclusion: 'success',
    html_url: 'https://github.com/org/repo/actions/runs/77',
  };
  const appGithub = {
    request: async (route, args) => {
      appActions.push({ route, args });
      if (route === 'POST /repos/{owner}/{repo}/pulls') return { data: stage };
      if (route === 'PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge') {
        assert.equal(args.pull_number, 66);
        assert.equal(args.sha, head);
        return { data: { merged: true, sha: mergedSHA } };
      }
      throw new Error('Unexpected App API: ' + route);
    },
  };
  let statusId = 88;
  const github = {
    request: async (route, args) => {
      actions.push({ route, args });
      if (route === 'POST /repos/{owner}/{repo}/issues/{issue_number}/comments') {
        return { data: { id: statusId++ } };
      }
      if (route === 'PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}') return { data: {} };
      if (route === 'GET /repos/{owner}/{repo}/collaborators/{username}/permission') {
        return { data: { permission: 'write' } };
      }
      if (route === 'GET /repos/{owner}/{repo}/pulls/{pull_number}') {
        return { data: args.pull_number === 66
          ? { ...stage, state: 'closed', merged: true, merged_at: '2026-11-13T07:00:00Z', merge_commit_sha: mergedSHA }
          : pr };
      }
      if (route === 'GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}') {
        return { data: { state: 'active' } };
      }
      if (route === 'POST /repos/{owner}/{repo}/git/refs') return { data: {} };
      if (route === 'POST /repos/{owner}/{repo}/actions/jobs/{job_id}/rerun') return { data: {} };
      throw new Error('Unexpected default-token API: ' + route);
    },
    paginate: async (route) => {
      actions.push({ route });
      if (route.endsWith('/pulls/{pull_number}/reviews')) return [{
        state: 'APPROVED', commit_id: head, user: { login: 'reviewer' },
      }];
      if (route.endsWith('/issues/{issue_number}/comments')) return [];
      if (route.endsWith('/rules/branches/{branch}')) return [];
      if (route.endsWith('/actions/runs')) return [{
        id: 16, head_sha: head, event: 'pull_request',
        name: 'Check PR commits exist in staging',
        path: '.github/workflows/check-commits-in-staging.yml',
        pull_requests: [{ number: 42 }],
      }];
      if (route.endsWith('/actions/runs/{run_id}/jobs')) return [
        { id: 99, name: 'check-commits' },
      ];
      if (route.endsWith('/commits/{ref}/check-runs')) return [];
      if (route.endsWith('/commits/{ref}/statuses')) return [];
      if (route.endsWith('/pulls')) return [];
      if (route.endsWith('/git/matching-refs/{ref}')) return [
        { ref: 'refs/tags/dev-v1.1.24', object: { sha: 'e'.repeat(40) } },
        { ref: 'refs/tags/dev-auto-pr100-ffffffffffff', object: { sha: 'f'.repeat(40) } },
      ];
      if (route.endsWith('/actions/workflows/{workflow_id}/runs')) return [deployment];
      throw new Error('Unexpected pagination: ' + route);
    },
    graphql: async query => query.includes('reviewThreads')
      ? { repository: { pullRequest: { reviewThreads: {
          nodes: [], pageInfo: { hasNextPage: false },
        } } } }
      : { repository: { pullRequest: {
          headRefOid: head, baseRef: { branchProtectionRule: null },
        } } },
  };
  const git = {
    staging: originalStaging,
    prepare: async () => originalStaging,
    git: async (...args) => {
      const cmd = args[0];
      if (cmd === 'merge-base' && args[1] === '--is-ancestor' && args[2] === head &&
          args[3] === originalStaging) throw new Error('Not contained');
      if (cmd === 'ls-remote') return originalStaging;
      if (cmd === 'merge-tree') return tree + '\0';
      if (cmd === 'show') return 'name: test\non:\n  workflow_dispatch:\njobs: {}';
      if (cmd === 'fetch') return '';
      if (cmd === 'rev-parse') return mergedSHA;
      if (cmd === 'merge-base') return '';
      throw new Error('Unexpected Git operation: ' + args.join(' '));
    },
  };
  const failures = [];
  await deploy({
    github, appGithub, git, context: {
      eventName: 'issue_comment', runId: 123, repo: { owner: 'org', repo: 'repo' },
      payload: { action: 'created', issue: { number: 42, pull_request: {} },
        comment: { id: 5, body: '/deploy-staging', user: { login: 'author' } } },
    },
    core: { info: () => {}, setFailed: msg => failures.push(msg), setOutput: () => {} },
    sleep: async () => {},
  });
  assert.deepEqual(failures, []);
  assert.deepEqual(appActions.map(c => c.route), [
    'POST /repos/{owner}/{repo}/pulls',
    'PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge',
  ]);
  assert(!actions.some(c => /POST .*\/pulls$|PUT .*\/merge$/.test(c.route)));
  assert(actions.some(c => c.route === 'POST /repos/{owner}/{repo}/git/refs'));
  assert(actions.some(c => c.route === 'POST /repos/{owner}/{repo}/actions/jobs/{job_id}/rerun'));
  const messages = actions.filter(c => /^(?:POST|PATCH) \/repos\/\{owner\}\/\{repo\}\/issues\/(?:\{issue_number\}\/comments|comments\/\{comment_id\})$/.test(c.route))
    .map(c => c.args.body);
  assert.equal(messages.filter(body => body.startsWith('<!-- staging-promotion-status:')).length > 1, true);
  assert(messages.at(-1).includes('✅ Staging deployment — Successful'));
  assert(messages.at(-1).includes('https://github.com/org/repo/pull/66'));
  assert(messages.at(-1).includes('https://github.com/org/repo/actions/runs/77'));
  assert(messages.at(-1).includes('dev-v1.1.25'));
  assert(messages.at(-1).includes('/tree/refs/tags/dev-v1.1.25'));
  assert(!messages.at(-1).includes('**Main PR:**'));
  assert(!messages.at(-1).includes('**Requested by:**'));
});

test('version tag increments the highest dev-v patch and ignores non-version tags', () => {
  const refs = [
    { ref: 'refs/tags/dev-v1.1.9', object: { sha: 'a'.repeat(40) } },
    { ref: 'refs/tags/dev-auto-pr42-abcdef', object: { sha: 'b'.repeat(40) } },
    { ref: 'refs/tags/dev-v1.1.24', object: { sha: 'c'.repeat(40) } },
    { ref: 'refs/tags/prod-v8.0.0', object: { sha: 'd'.repeat(40) } },
    { ref: 'refs/tags/dev-v1.1.23', object: { sha: 'e'.repeat(40) } },
  ];
  assert.deepEqual(nextStagingTag(refs, 'f'.repeat(40)), { tag: 'dev-v1.1.25', existing: false });
  assert.deepEqual(nextStagingTag(refs, 'c'.repeat(40)), { tag: 'dev-v1.1.24', existing: true });
  assert.throws(() => nextStagingTag([{ ref: 'refs/tags/dev-old' }], 'f'.repeat(40)), /No dev-vX.Y.Z/);
});

test('version components use exact BigInt ordering beyond Number.MAX_SAFE_INTEGER', () => {
  const refs = [
    { ref: 'refs/tags/dev-v9007199254740992.0.9', object: { sha: 'a'.repeat(40) } },
    { ref: 'refs/tags/dev-v9007199254740993.0.9', object: { sha: 'b'.repeat(40) } },
    { ref: 'refs/tags/dev-v1.1.24', object: { sha: 'c'.repeat(40) } },
  ];
  assert.equal(nextStagingTag(refs, 'd'.repeat(40)).tag, 'dev-v9007199254740993.0.10');
});

test('annotated version tags peel to commit SHA and reuse existing version', async () => {
  const target = 'a'.repeat(40), tagObject = 'b'.repeat(40);
  const refs = [
    { ref: 'refs/tags/dev-v1.1.24', object: { type: 'tag', sha: tagObject } },
    { ref: 'refs/tags/dev-v1.1.23', object: { type: 'commit', sha: 'c'.repeat(40) } },
  ];
  const git = { git: async (...args) => {
    assert.deepEqual(args, ['for-each-ref', '--format=%(refname)%09%(*objectname)', 'refs/tags/dev-v*']);
    return 'refs/tags/dev-v1.1.24\t' + target + '\n';
  } };
  const normalized = await normalizeTagTargets(refs, git);
  assert.equal(normalized[0].commitSha, target);
  assert.deepEqual(nextStagingTag(normalized, target), { tag: 'dev-v1.1.24', existing: true });
  await assert.rejects(normalizeTagTargets(refs, { git: async () => '' }), /Cannot verify annotated tag target/);
});

test('legacy dev-auto promotion is reused for the exact PR and commit, never retagged', () => {
  const mergedSHA = 'b4a9992b97a826f4749480089c90afe3836fc317';
  const refs = [
    { ref: 'refs/tags/dev-v1.1.24', object: { type: 'commit', sha: '1'.repeat(40) } },
    { ref: 'refs/tags/dev-auto-pr452-b4a9992b97a8',
      object: { type: 'commit', sha: mergedSHA } },
  ];
  assert.deepEqual(selectPromotionTag(refs, mergedSHA, 452),
    { tag: 'dev-auto-pr452-b4a9992b97a8', existing: true, legacy: true });
  assert.deepEqual(selectPromotionTag(refs, mergedSHA, 999),
    { tag: 'dev-v1.1.25', existing: false, legacy: false });
  assert.throws(() => selectPromotionTag([
    { ...refs[1], object: { type: 'commit', sha: 'f'.repeat(40) } },
    refs[0],
  ], mergedSHA, 452), /unexpected commit/);
});

test('failure status retains review thread links, file lists and actionable commands', async () => {
  const calls = [];
  await deploy({
    github: {
      request: async (route, args) => {
        calls.push({ route, args });
        if (route === 'POST /repos/{owner}/{repo}/issues/{issue_number}/comments')
          return { data: { id: 99 } };
        if (route === 'PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}')
          return { data: {} };
        if (route === 'GET /repos/{owner}/{repo}/collaborators/{username}/permission')
          return { data: { permission: 'write' } };
        if (route === 'GET /repos/{owner}/{repo}/pulls/{pull_number}')
          return { data: { state: 'open', base: { ref: 'main' }, head: {
            repo: { full_name: 'org/repo' }, ref: 'feature/7319', sha: 'a'.repeat(40),
          } } };
        throw Error('Unexpected API: ' + route);
      },
      paginate: async route => route.endsWith('/reviews') ? [] : [],
      graphql: async q => q.includes('reviewThreads') ? {
        repository: { pullRequest: { reviewThreads: {
          nodes: [{ isResolved: false, comments: { nodes: [{
            url: 'https://github.com/org/repo/pull/42#discussion_r777',
          }] } }], pageInfo: { hasNextPage: false },
        } } },
      } : { repository: { pullRequest: { headRefOid: 'a'.repeat(40),
        baseRef: { branchProtectionRule: null } } } },
    },
    context: { eventName: 'issue_comment', runId: 456, repo: { owner: 'org', repo: 'repo' },
      payload: { action: 'created', issue: { number: 42, pull_request: {} },
        comment: { id: 123, body: '/deploy-staging', user: { login: 'author' } } } },
    core: { setFailed: () => {}, setOutput: () => {} },
  });
  const last = calls.filter(c => c.route.startsWith('PATCH ')).at(-1).args.body;
  assert.match(last, /https:\/\/github.com\/org\/repo\/pull\/42#discussion_r777/);
  assert.match(last, /Resolve all review conversations/);
});

test('staging PR write refuses missing App client before any PR mutation', async () => {
  const source = require('node:fs').readFileSync(
    require('node:path').join(__dirname, 'auto-staging-pr.cjs'), 'utf8');
  assert.match(source, /if \(!appGithub\?\.request\) throw new Error/);
  assert.doesNotMatch(source, /command\('gh'/);
  assert.match(source, /appRequest\('POST \/repos\/\{owner\}\/\{repo\}\/pulls'/);
  assert.match(source, /appRequest\('PUT \/repos\/\{owner\}\/\{repo\}\/pulls\/\{pull_number\}\/merge'/);
});

test('latest review supersedes previous changes requested', () => {
  const reviews = [
    { user: { login: 'dev' }, state: 'CHANGES_REQUESTED', submitted_at: '2026-11-01T10:00:00Z' },
    { user: { login: 'dev' }, state: 'APPROVED', submitted_at: '2026-11-01T11:00:00Z' },
    { user: { login: 'other' }, state: 'CHANGES_REQUESTED', submitted_at: '2026-11-01T12:00:00Z' },
  ];
  assert.deepEqual(activeChangeRequests(reviews), ['other']);
});

test('conflict instructions keep original main branch untouched', () => {
  const text = conflictMessage(42, '7319', ['src/A.java']);
  assert.match(text, /conflict\/resolved\/7319/);
  assert.match(text, /src\/A\.java/);
  assert.match(text, /never merge staging into the original main feature branch/);
  assert.match(text, /\/deploy-staging again/);
});

function fixture({ unresolved = false, reviews = [], prOverride = {} } = {}) {
  const pr = {
    state: 'open', draft: false, base: { ref: 'main' },
    head: { sha: 'a'.repeat(40) }, user: { login: 'pr-author' }, merge_commit_sha: null, ...prOverride,
  };
  const github = {
    request: async () => ({ data: { permission: 'write' } }),
    graphql: async (query) => query.includes('reviewThreads') ? {
      repository: { pullRequest: { reviewThreads: {
        nodes: unresolved ? [{ isResolved: false, comments: { nodes: [{ url: 'https://example.test/thread/1' }] } }] : [],
        pageInfo: { hasNextPage: false },
      } } },
    } : { repository: { pullRequest: { headRefOid: pr.head.sha, baseRef: { branchProtectionRule: null } } } },
  };
  const pages = async (route) => {
    if (route.includes('/reviews')) return reviews;
    return [];
  };
  return { github, repo: { owner: 'org', repo: 'repo' }, number: 42, head: 'a'.repeat(40), getPR: async () => pr, pages };
}

test('unresolved review threads block staging PR creation', async () => {
  await assert.rejects(checkReady(fixture({ unresolved: true })), /Resolve all review conversations/);
});

test('outstanding CHANGES_REQUESTED blocks staging PR creation', async () => {
  await assert.rejects(checkReady(fixture({
    reviews: [{ user: { login: 'reviewer' }, state: 'CHANGES_REQUESTED', submitted_at: '2026-11-01T12:00:00Z' }],
  })), /Outstanding changes requested/);
});

test('green checks alone cannot promote without a current-head reviewer', async () => {
  await assert.rejects(checkReady(fixture()), /Codex has not cleared this PR head/);
});

test('an approved current SHA from another reviewer permits promotion', async () => {
  const head = 'a'.repeat(40);
  const reviews = [{ state: 'APPROVED', commit_id: head, user: { login: 'reviewer' },
    submitted_at: '2026-11-12T11:00:00Z' }];
  await assert.doesNotReject(checkReady(fixture({ reviews })));
});

test('approvals from the PR author or older commits are not sufficient', async () => {
  for (const review of [
    { state: 'APPROVED', commit_id: 'b'.repeat(40), user: { login: 'reviewer' } },
    { state: 'APPROVED', commit_id: 'a'.repeat(40), user: { login: 'pr-author' } },
  ]) await assert.rejects(checkReady(fixture({ reviews: [review] })), /Codex has not cleared this PR head/);
});

test('draft, changed commit, and non-main PRs cannot promote', async () => {
  for (const prOverride of [{ draft: true }, { base: { ref: 'staging' } }, { head: { sha: 'b'.repeat(40) } }]) {
    await assert.rejects(checkReady(fixture({ prOverride })), /open, non-draft, targeting main, and at the reviewed commit/);
  }
});

test('strict readiness rejects required workflow without an actual successful PR workflow run', () => {
  const rules = [{ type: 'workflows', parameters: { workflows: [{ path: '.github/workflows/pr-test-validation.yml' }] } }];
  assert.throws(() => verifyChecks({
    head: 'a'.repeat(40), number: 42, protection: null, rules,
    checks: [{ id: 1, name: 'validate-tests', app: { id: 1 }, status: 'completed', conclusion: 'success' }],
    statuses: [{ id: 2, context: 'validate-tests', state: 'success' }], runs: [],
    strictTestValidation: true,
  }), /missing required workflow/);
});

test('strict readiness rejects status checks from the wrong required app', () => {
  const rules = [{ type: 'required_status_checks', parameters: { required_status_checks: [
    { context: 'validate-tests', integration_id: 123 },
  ] } }];
  assert.throws(() => verifyChecks({
    head: 'a'.repeat(40), number: 42, protection: null, rules,
    checks: [{ id: 1, name: 'validate-tests', app: { id: 456 }, status: 'completed', conclusion: 'success' }],
    statuses: [{ id: 2, context: 'validate-tests', state: 'success' }], runs: [],
    strictTestValidation: true,
  }), /required app 123/);
});

test('strict readiness accepts a completed required workflow with successful job checks', () => {
  const head = 'a'.repeat(40);
  const path = '.github/workflows/pr-test-validation.yml';
  assert.doesNotThrow(() => verifyChecks({
    head, number: 42, protection: null,
    rules: [{ type: 'workflows', parameters: { workflows: [{ path }] } }],
    runs: [{ id: 10, head_sha: head, event: 'pull_request', path, check_suite_id: 99,
      pull_requests: [{ number: 42 }], status: 'completed', conclusion: 'success' }],
    checks: [{ id: 11, name: 'validate-tests', check_suite: { id: 99 },
      status: 'completed', conclusion: 'success', app: { id: 123 } }],
    statuses: [], strictTestValidation: true,
  }));
});

test('merge-SHA required workflow runs match their check suite when selected', () => {
  const head = 'a'.repeat(40), merge = 'b'.repeat(40);
  const path = '.github/workflows/pr-test-validation.yml';
  const checks = [{ id: 31, name: 'validate-tests', check_suite: { id: 57 },
    status: 'completed', conclusion: 'success', app: { id: 123 } }];
  const data = {
    head, workflowRunSha: merge, number: 42, protection: null,
    rules: [{ type: 'workflows', parameters: { workflows: [{ path }] } }],
    runs: [{ id: 21, head_sha: merge, event: 'pull_request', path,
      check_suite_id: 57, pull_requests: [{ number: 42 }],
      status: 'completed', conclusion: 'success' }],
    checks, statuses: [], strictTestValidation: true,
  };
  assert.doesNotThrow(() => verifyChecks(data));
  assert.throws(() => verifyChecks({ ...data, workflowRunSha: head }), /missing required workflow/);
});

test('conflict resolution candidates must identify the exact original main PR and SHA', () => {
  const source = require('node:fs').readFileSync(
    require('node:path').join(__dirname, 'auto-staging-pr.cjs'), 'utf8');
  assert.match(source, /p\.head\.ref === `conflict\/resolved\/\$\{issue \|\| number\}`/);
  assert.match(source, /\(p\.body \|\| ''\)\.includes\(pr\.html_url\)/);
  assert.match(source, /\(p\.body \|\| ''\)\.includes\(head\)/);
});

test('idempotent staging promotions reuse the original merged snapshot after staging advances', () => {
  assert.equal(promotionSnapshot({
    stagingSHA: 'b'.repeat(40), mergedSHA: 'a'.repeat(40), previouslyMerged: true,
  }), 'a'.repeat(40));
  assert.equal(promotionSnapshot({
    stagingSHA: 'a'.repeat(40), mergedSHA: 'a'.repeat(40), previouslyMerged: false,
  }), 'a'.repeat(40));
});

test('newly merged promotions reject concurrent staging changes', () => {
  assert.throws(() => promotionSnapshot({
    stagingSHA: 'b'.repeat(40), mergedSHA: 'a'.repeat(40), previouslyMerged: false,
  }), /Staging changed/);
});


test('review IDs decide latest approval vs changes requested at equal timestamps', () => {
  const submitted_at = '2026-11-12T10:00:00Z';
  assert.deepEqual(activeChangeRequests([
    { id: 100, user: { login: 'reviewer' }, state: 'APPROVED', submitted_at },
    { id: 101, user: { login: 'reviewer' }, state: 'CHANGES_REQUESTED', submitted_at },
  ]), ['reviewer']);
  assert.deepEqual(activeChangeRequests([
    { id: 101, user: { login: 'reviewer' }, state: 'CHANGES_REQUESTED', submitted_at },
    { id: 102, user: { login: 'reviewer' }, state: 'APPROVED', submitted_at },
  ]), []);
});

test('required workflow source repo and ref must match, not just filename', () => {
  const expected = { path: '.github/workflows/validate.yml', repository_id: 123, ref: 'refs/heads/main' };
  const sources = { '123': { name: 'Eshopbox-Enginnering/org-actions', sha: 'a'.repeat(40) } };
  const run = { path: 'Eshopbox-Enginnering/org-actions/.github/workflows/validate.yml@refs/heads/main' };
  assert.equal(requiredWorkflowMatches({ run, expected, sources }), true);
  assert.equal(requiredWorkflowMatches({ run: { path: 'Eshopbox-Enginnering/esb-product-engine-jobs/.github/workflows/validate.yml@refs/heads/main' }, expected, sources }), false);
  assert.equal(requiredWorkflowMatches({ run: { path: 'Eshopbox-Enginnering/org-actions/.github/workflows/validate.yml@refs/heads/staging' }, expected, sources }), false);
  assert.equal(requiredWorkflowMatches({ run, expected, sources: {} }), false);
});

test('trusted workflow binding preserves heads versus tags namespaces', () => {
  const sources = { '1041381819': { name: 'Eshopbox-Enginnering/org-actions', sha: 'a'.repeat(40), workflowId: 400272849, ref: 'refs/heads/main', path: '.github/workflows/java-test-coverage.yml' } };
  const run = {
    path: '.github/workflows/java-test-coverage.yml',
    workflow_id: 400272849,
    workflow_url: 'https://api.github.com/repos/Eshopbox-Enginnering/esb-product-engine-jobs/actions/required_workflows/400272849',
  };
  const expected = { path: run.path, repository_id: 1041381819, ref: 'refs/heads/main' };
  assert.equal(requiredWorkflowMatches({ run, expected, sources }), true);
  assert.equal(requiredWorkflowMatches({ run, expected: { ...expected, ref: 'refs/tags/main' }, sources }), false);
  assert.equal(requiredWorkflowMatches({ run, expected: { ...expected, ref: 'refs/heads/main' }, sources }), true);
});

test('SHA-pinned qualified run requires matching source revision proof', () => {
  const sha = 'a'.repeat(40);
  const expected = { path: '.github/workflows/validate.yml', repository_id: 123, sha };
  const sources = { '123': { name: 'Eshopbox-Enginnering/org-actions', sha } };
  const run = { path: 'Eshopbox-Enginnering/org-actions/.github/workflows/validate.yml@refs/heads/attacker' };
  assert.equal(requiredWorkflowMatches({ run, expected, sources }), false);
  assert.equal(requiredWorkflowMatches({ run: { ...run, workflow_sha: sha }, expected, sources }), true);
  assert.equal(requiredWorkflowMatches({
    run: { path: 'Eshopbox-Enginnering/org-actions/.github/workflows/validate.yml@' + sha },
    expected, sources,
  }), true);
});

test('GitHub organization-required workflow run is recognized despite local-looking path', () => {
  const expected = { path: '.github/workflows/java-test-coverage.yml', repository_id: 1041381819, id: 400272849,
    ref: 'refs/heads/main' };
  const sources = { '1041381819': { name: 'Eshopbox-Enginnering/org-actions', sha: 'a'.repeat(40) } };
  const run = {
    workflow_sha: 'a'.repeat(40),
    path: '.github/workflows/java-test-coverage.yml',
    workflow_url: 'https://api.github.com/repos/Eshopbox-Enginnering/esb-product-engine-jobs/actions/required_workflows/400272849',
  };
  assert.equal(requiredWorkflowMatches({ run, expected, sources }), true);
  assert.equal(requiredWorkflowMatches({ run: { ...run, workflow_sha: 'b'.repeat(40) }, expected, sources }), false);
  assert.equal(requiredWorkflowMatches({ run, expected: { ...expected, id: undefined }, sources }), true);
  assert.equal(requiredWorkflowMatches({
    run: { ...run, workflow_url: 'https://api.github.com/repos/Eshopbox-Enginnering/esb-product-engine-jobs/actions/workflows/400272849' },
    expected, sources,
  }), false, 'ordinary local workflow must not satisfy org-required workflow rule');
  assert.equal(requiredWorkflowMatches({
    run: { ...run, path: '.github/workflows/other.yml' }, expected, sources,
  }), false, 'another required workflow must not satisfy the wrong path');
});

test('strict workflow rules accept a successful GitHub required-workflow run', () => {
  const head = 'a'.repeat(40);
  const expected = { path: '.github/workflows/java-test-coverage.yml',
    repository_id: 1041381819, id: 400272849, ref: 'refs/heads/main' };
  assert.doesNotThrow(() => verifyChecks({
    head, number: 440, protection: null,
    rules: [{ type: 'workflows', parameters: { workflows: [expected] } }],
    runs: [{ id: 43198519093, head_sha: head, event: 'pull_request',
      path: expected.path, workflow_sha: 'a'.repeat(40),
      workflow_url: 'https://api.github.com/repos/Eshopbox-Enginnering/esb-product-engine-jobs/actions/required_workflows/400272849',
      check_suite_id: 99, pull_requests: [{ number: 440 }],
      status: 'completed', conclusion: 'success' }],
    checks: [
      { id: 10, name: 'Detect supported Java repository', check_suite: { id: 99 },
        status: 'completed', conclusion: 'success' },
      { id: 11, name: 'coverage', check_suite: { id: 99 },
        status: 'completed', conclusion: 'success' },
      { id: 12, name: 'Coverage not applicable', check_suite: { id: 99 },
        status: 'completed', conclusion: 'skipped' },
    ],
    statuses: [], strictTestValidation: true,
    workflowSources: { '1041381819': { name: 'Eshopbox-Enginnering/org-actions', sha: 'a'.repeat(40) } },
  }));
});


test('unknown required-workflow ID without source SHA fails closed', () => {
  const head = '8'.repeat(40), expected = {
    path: '.github/workflows/java-test-coverage.yml',
    repository_id: 1041381819, ref: 'refs/heads/main',
  };
  const source = { name: 'Eshopbox-Enginnering/org-actions', sha: 'a'.repeat(40) };
  const workflowSources = { '1041381819': source, '1041381819@main': source };
  const run = { id: 43198519093, head_sha: head, event: 'pull_request',
    path: expected.path, workflow_id: 999000001,
    workflow_url: 'https://api.github.com/repos/Eshopbox-Enginnering/esb-product-engine-jobs/actions/required_workflows/999000001',
    pull_requests: [{ number: 440 }], check_suite_id: 99,
    status: 'completed', conclusion: 'success' };
  const checks = [
    { id: 10, name: 'Detect supported Java repository', check_suite: { id: 99 },
      status: 'completed', conclusion: 'success' },
    { id: 11, name: 'coverage', check_suite: { id: 99 },
      status: 'completed', conclusion: 'success' },
    { id: 12, name: 'Coverage not applicable', check_suite: { id: 99 },
      status: 'completed', conclusion: 'skipped' },
  ];
  const rules = [{ type: 'workflows', parameters: { workflows: [expected] } }];
  const data = { head, number: 440, protection: null, rules, runs: [run],
    checks, statuses: [], strictTestValidation: true, workflowSources };
  assert.throws(() => verifyChecks(data), /missing required workflow/);
  assert.doesNotThrow(() => verifyChecks({ ...data,
    runs: [{ ...run, workflow_sha: 'a'.repeat(40) }],
  }));
  assert.throws(() => verifyChecks({ ...data,
    runs: [{ ...run, workflow_sha: 'b'.repeat(40) }],
  }), /missing required workflow/);
  assert.throws(() => verifyChecks({ ...data,
    workflowSources: { '1041381819': { name: source.name } },
  }), /missing required workflow/);
});


test('observed GitHub required workflow run without workflow_sha matches reviewed identity', () => {
  const head = '8'.repeat(40);
  const source = { name: 'Eshopbox-Enginnering/org-actions', sha: 'a'.repeat(40), workflowId: 400272849, ref: 'refs/heads/main', path: '.github/workflows/java-test-coverage.yml' };
  const expected = { path: '.github/workflows/java-test-coverage.yml',
    repository_id: 1041381819, ref: 'refs/heads/main' };
  const run = { id: 43198519093, head_sha: head, event: 'pull_request',
    path: expected.path, workflow_id: 400272849,
    workflow_url: 'https://api.github.com/repos/Eshopbox-Enginnering/esb-product-engine-jobs/actions/required_workflows/400272849',
    check_suite_id: 99, pull_requests: [{ number: 440 }],
    status: 'completed', conclusion: 'success' };
  const checks = [
    { id: 1, name: 'Detect supported Java repository',
      check_suite: { id: 99 }, status: 'completed', conclusion: 'success' },
    { id: 2, name: 'Coverage not applicable',
      check_suite: { id: 99 }, status: 'completed', conclusion: 'success' },
    { id: 3, name: 'coverage',
      check_suite: { id: 99 }, status: 'completed', conclusion: 'skipped' },
  ];
  const workflowSources = { '1041381819': source, '1041381819@main': source };
  const data = { head, number: 440, protection: null,
    rules: [{ type: 'workflows', parameters: { workflows: [expected] } }],
    checks, statuses: [], runs: [run], strictTestValidation: true, workflowSources };
  assert.doesNotThrow(() => verifyChecks(data));
  for (const runOverride of [
    { workflow_id: 777 },
    { workflow_url: run.workflow_url.replace('required_workflows', 'workflows') },
    { workflow_id: 999, workflow_url: run.workflow_url.replace('400272849', '999') },
  ]) assert.throws(() => verifyChecks({ ...data, runs: [{ ...run, ...runOverride }] }), /missing required workflow/);
  for (const ruleOverride of [
    { repository_id: 999 },
    { ref: 'refs/heads/staging' },
    { path: '.github/workflows/pr-ticket-validation.yml' },
    { sha: 'b'.repeat(40) },
  ]) assert.throws(() => verifyChecks({ ...data, rules: [{
    type: 'workflows', parameters: { workflows: [{ ...expected, ...ruleOverride }] },
  }] }), /missing required workflow|cannot verify required workflow source/);
  assert.throws(() => verifyChecks({ ...data, runs: [{ ...run, conclusion: 'failure' }] }), /failure/);
});

test('strict required workflow refuses green lookalike from different source', () => {
  const head = 'a'.repeat(40);
  const expected = { path: '.github/workflows/pr-test-validation.yml', repository_id: 123, ref: 'main' };
  assert.throws(() => verifyChecks({
    head, number: 42, protection: null,
    rules: [{ type: 'workflows', parameters: { workflows: [expected] } }],
    runs: [{ id: 10, head_sha: head, event: 'pull_request',
      path: 'Eshopbox-Enginnering/esb-product-engine-jobs/.github/workflows/pr-test-validation.yml@main',
      check_suite_id: 99, pull_requests: [{ number: 42 }], status: 'completed', conclusion: 'success' }],
    checks: [{ id: 11, name: 'validate-tests', check_suite: { id: 99 }, status: 'completed', conclusion: 'success' }],
    statuses: [], strictTestValidation: true,
    workflowSources: { '123': { name: 'Eshopbox-Enginnering/org-actions', sha: 'a'.repeat(40) } },
  }), /missing required workflow/);
});


function liveRequiredChecksFixture() {
  const head = '8'.repeat(40);
  const source = { name: 'Eshopbox-Enginnering/org-actions', sha: 'a'.repeat(40), workflowId: 400272849, ref: 'refs/heads/main', path: '.github/workflows/java-test-coverage.yml' };
  const makeRun = (id, workflowId, path, check_suite_id, conclusion = 'success') => ({
    id, name: path.split('/').pop(), path, check_suite_id, workflow_id: workflowId,
    workflow_url: `https://api.github.com/repos/Eshopbox-Enginnering/esb-product-engine-jobs/actions/required_workflows/${workflowId}`,
    head_sha: head, event: 'pull_request', pull_requests: [{ number: 440 }],
    status: 'completed', conclusion,
  });
  const check = (id, name, suiteId, conclusion = 'success') => ({
    id, name, status: 'completed', conclusion, app: { id: 15368, slug: 'github-actions' },
    check_suite: { id: suiteId },
  });
  const coverage = '.github/workflows/java-test-coverage.yml';
  const title = '.github/workflows/pr-ticket-validation.yml';
  const staging = '.github/workflows/check-commits-in-staging.yml';
  const runs = [
    makeRun(43198519093, 400272849, coverage, 116977410534),
    makeRun(43198519057, 251714341, title, 116977410453),
    { ...makeRun(43198519026, 184691454, staging, 116977410367, 'failure'),
      name: 'Check PR commits exist in staging' },
  ];
  const checks = [
    check(10, 'Detect supported Java repository', 116977410534),
    check(11, 'Coverage not applicable', 116977410534),
    check(12, 'coverage', 116977410534, 'skipped'),
    check(13, 'validate-pr-title-and-ticket', 116977410453),
    check(14, 'check-commits', 116977410367, 'failure'),
    // Unrelated optional check should not influence required-workflow identity.
    check(15, 'review / AI Code Review', 116977412867, 'skipped'),
  ];
  const rules = [
    { type: 'workflows', parameters: { workflows: [
      { path: coverage, repository_id: 1041381819, ref: 'refs/heads/main' },
      { path: title, repository_id: 1041381819, ref: 'refs/heads/main' },
      { path: staging },
    ] } },
    { type: 'required_status_checks', parameters: {
      required_status_checks: [{ context: 'check-commits', integration_id: 15368 }],
    } },
  ];
  return { head, number: 440, protection: null, rules, checks, statuses: [],
    runs, strictTestValidation: true,
    workflowSources: { '1041381819': source, '1041381819@main': source,
      ['1041381819@main@' + coverage]: source,
      ['1041381819@main@' + title]: { ...source, workflowId: 251714341, path: title } } };
}

test('live PR #440 check matrix accepts successful workflow with conditional skipped coverage', () => {
  assert.doesNotThrow(() => verifyChecks(liveRequiredChecksFixture()));
});

test('conditional job skips are safe only inside successful required workflows', () => {
  const data = liveRequiredChecksFixture();
  const coverage = data.checks.find(c => c.name === 'coverage');
  const notApplicable = data.checks.find(c => c.name === 'Coverage not applicable');
  const detect = data.checks.find(c => c.name === 'Detect supported Java repository');
  assert.doesNotThrow(() => verifyChecks({ ...data, checks: data.checks.map(c =>
    c.id === coverage.id ? { ...c, conclusion: 'success' } :
    c.id === notApplicable.id ? { ...c, conclusion: 'skipped' } : c) }));
  assert.throws(() => verifyChecks({ ...data, checks: data.checks.map(c =>
    c.id === notApplicable.id ? { ...c, conclusion: 'skipped' } : c) }),
    /coverage|Coverage not applicable/);
  assert.throws(() => verifyChecks({ ...data, checks: data.checks.filter(c =>
    ![coverage.id, notApplicable.id].includes(c.id)) }), /coverage|Coverage not applicable/);
  assert.throws(() => verifyChecks({ ...data, checks: data.checks.map(c =>
    c.id === detect.id ? { ...c, conclusion: 'failure' } : c) }), /Detect supported Java repository: failure/);
  assert.throws(() => verifyChecks({ ...data, checks: data.checks.map(c =>
    c.id === detect.id ? { ...c, conclusion: 'skipped' } : c) }),
    /Java applicability detection did not succeed/);
});

test('explicitly required skipped status check never inherits a workflow exemption', () => {
  const data = liveRequiredChecksFixture();
  const rules = [...data.rules, { type: 'required_status_checks', parameters: {
    required_status_checks: [{ context: 'coverage', integration_id: 15368 }],
  } }];
  assert.throws(() => verifyChecks({ ...data, rules }), /coverage: skipped/);
});

test('required workflows reject failed or missing results, but ignore optional skipped jobs', () => {
  const data = liveRequiredChecksFixture();
  assert.throws(() => verifyChecks({ ...data, runs: data.runs.map(r =>
    r.workflow_id === 400272849 ? { ...r, conclusion: 'failure' } : r) }), /java-test-coverage.yml: failure/);
  assert.throws(() => verifyChecks({ ...data, runs: data.runs.filter(r =>
    r.workflow_id !== 251714341) }), /pr-ticket-validation.yml: missing required workflow/);
  assert.throws(() => verifyChecks({ ...data, checks: data.checks.map(c =>
    c.name === 'validate-pr-title-and-ticket' ? { ...c, conclusion: 'skipped' } : c) }),
    /pr-ticket-validation.yml|validate-pr-title-and-ticket/);
  assert.throws(() => verifyChecks({ ...data, checks: data.checks.map(c =>
    c.name === 'validate-pr-title-and-ticket' ? { ...c, status: 'in_progress', conclusion: null } : c) }),
    /validate-pr-title-and-ticket/);
  assert.throws(() => verifyChecks({ ...data, checks: data.checks.filter(c =>
    c.name !== 'validate-pr-title-and-ticket') }), /pr-ticket-validation.yml: missing job checks/);
  assert.doesNotThrow(() => verifyChecks(data));
});

test('only top-level on workflow_dispatch is accepted', () => {
  assert.equal(hasWorkflowDispatch('name: test\non:\n  push:\n  workflow_dispatch:\njobs:\n  work:\n    runs-on: ubuntu-latest'), true);
  assert.equal(hasWorkflowDispatch("on: [push, workflow_dispatch]\njobs: {}"), true);
  assert.equal(hasWorkflowDispatch("on:\n  push:\njobs:\n  workflow_dispatch:\n    runs-on: ubuntu-latest"), false);
  assert.equal(hasWorkflowDispatch("on:\n  workflow_dispatch: false\njobs: {}"), false);
  assert.equal(hasWorkflowDispatch("on: push\njobs:\n  workflow_dispatch:\n    runs-on: ubuntu-latest"), false);
  assert.equal(hasWorkflowDispatch("on:\n  push:\non:\n  workflow_dispatch:\njobs: {}"), false);
});


test('auto-triggered clean Codex summary clears current PR head without explicit review command', () => {
  const head = '3'.repeat(40);
  const bot = { login: 'chatgpt-codex-connector[bot]' };
  const summary = (sha, codeReview) => ({
    id: 20, user: bot, created_at: '2026-11-13T06:25:20Z',
    updated_at: '2026-11-13T06:25:50Z',
    body: '<!-- codex-review-state:v1 ' + JSON.stringify({
      code_review: { commit_sha: sha, status: 'completed', has_any_findings: false,
        has_unresolved_findings: false, ...codeReview },
    }) + ' -->',
  });
  assert.doesNotThrow(() => verifyCodex({
    head, comments: [summary(head)], reviews: [], reactions: [],
  }));
  for (const changed of [
    summary('4'.repeat(40)),
    summary(head, { has_any_findings: true }),
    summary(head, { has_unresolved_findings: true }),
    summary(head, { status: 'in_progress' }),
  ]) assert.throws(() => verifyCodex({
    head, comments: [changed], reviews: [], reactions: [],
  }), /Codex has not cleared/);
  assert.throws(() => verifyCodex({
    head, comments: [summary(head), { id: 21, body: '@codex review',
      created_at: '2026-11-13T06:26:00Z' }], reviews: [], reactions: [],
    requestId: 21,
  }), /Codex has not cleared/);
});

test('Codex code-review clearance survives later security-review receipt', () => {
  const head = '8'.repeat(40);
  const user = { login: 'chatgpt-codex-connector[bot]' };
  const comments = [
    { id: 10, body: '@codex review', created_at: '2026-11-12T10:14:19Z' },
    { id: 11, user, body: "Codex Review: Didn't find any major issues. Hooray!\n**Reviewed commit:** " + head.slice(0,10),
      created_at: '2026-11-12T10:15:24Z' },
    { id: 12, user, body: '### 🛡️ Codex Security Review · Automatically triggered\nSecurity review completed. No security issues were found.\n**Reviewed commit:** ' + head.slice(0,10),
      created_at: '2026-11-12T10:16:01Z' },
  ];
  assert.doesNotThrow(() => verifyCodex({ head, comments, reviews: [], reactions: [], requestId: 10 }));
  assert.throws(() => verifyCodex({ head: '9'.repeat(40), comments, reviews: [], reactions: [], requestId: 10 }),
    /Codex has not cleared/);
});

test('later blocker mentioning Security Review is not ignored', () => {
  const head = '8'.repeat(40);
  const user = { login: 'chatgpt-codex-connector[bot]' };
  const comments = [
    { id: 10, body: '@codex review', created_at: '2026-11-12T10:14:19Z' },
    { id: 11, user, body: "Codex Review: Didn't find any major issues. Hooray!\\nReviewed commit: " + head.slice(0,10), created_at: '2026-11-12T10:15:24Z' },
    { id: 12, user, body: 'Codex Review: Security Review: handling is unsafe\\nReviewed commit: ' + head.slice(0,10), created_at: '2026-11-12T10:16:01Z' },
  ];
  assert.throws(() => verifyCodex({ head, comments, reviews: [], reactions: [], requestId: 10 }), /Codex has not cleared/);
});

test('blocking security review overrides an older clean code review', () => {
  const head = '8'.repeat(40);
  const user = { login: 'chatgpt-codex-connector[bot]' };
  const comments = [
    { id: 1, body: '@codex review', created_at: '2026-11-12T10:00:00Z' },
    { id: 2, user, body: "Codex Review: Didn't find any major issues. Hooray!", created_at: '2026-11-12T10:01:00Z' },
  ];
  const reviews = [{ user, commit_id: head, submitted_at: '2026-11-12T10:02:00Z', body: '### 🛡️ Codex Security Review · Automatically triggered\nHere are some automated security review suggestions' }];
  assert.throws(() => verifyCodex({ head, comments, reviews, reactions: [], requestId: 1 }), /security review/);
  assert.throws(() => verifySecurity({ head, comments, reviews }), /security review/);
});

test('clean security receipt allows clean review', () => {
  const head = '8'.repeat(40);
  const user = { login: 'chatgpt-codex-connector[bot]' };
  const comments = [{ user, body: '### 🛡️ Codex Security Review · Automatically triggered\nNo security issues were found\n**Reviewed commit:** '+head.slice(0,10), created_at: '2026-11-12T10:02:00Z' }];
  assert.doesNotThrow(() => verifySecurity({ head, comments }));
});

test('unknown required workflow ID is rejected even with a matching workflow path', () => {
  const expected = { path: '.github/workflows/java-test-coverage.yml', repository_id: 1041381819, ref: 'refs/heads/main' };
  const sources = { '1041381819@main@.github/workflows/java-test-coverage.yml':
    { name: 'Eshopbox-Enginnering/org-actions', sha: 'a'.repeat(40), workflowId: 400272849, ref: 'refs/heads/main', path: '.github/workflows/java-test-coverage.yml' } };
  const run = { workflow_id: 999000001, path: expected.path,
    workflow_url: 'https://api.github.com/repos/Eshopbox-Enginnering/service/actions/required_workflows/999000001' };
  assert.equal(requiredWorkflowMatches({ run, expected, sources }), false);
  assert.equal(requiredWorkflowMatches({ run: { ...run, workflow_id: 400272849,
    workflow_url: run.workflow_url.replace('999000001', '400272849') }, expected, sources }), true);
});

test('required workflow source ref is resolved without an undocumented workflow ID', async () => {
  const rules = [{ type: 'workflows', parameters: { workflows: [
    { path: '.github/workflows/validate.yml', repository_id: 123, ref: 'refs/heads/main' }
  ] } }];
  const request = async (route) => {
    if (route.includes('/repositories/')) return { data: { id: 123, full_name: 'Eshopbox-Enginnering/org-actions' } };
    if (route.includes('/actions/workflows/')) return { data: { id: 400272849, path: '.github/workflows/validate.yml' } };
    return { data: { sha: 'a'.repeat(40) } };
  };
  const sources = await resolveWorkflowSources({ rules, request });
  assert.equal(sources['123@main'].sha, 'a'.repeat(40));
  assert.equal(sources['123@main@.github/workflows/validate.yml'].workflowId, 400272849);
  const run = { path: '.github/workflows/validate.yml', workflow_sha: 'a'.repeat(40),
    workflow_url: 'https://api.github.com/repos/example/service/actions/required_workflows/400272849' };
  assert.equal(requiredWorkflowMatches({ run, expected: rules[0].parameters.workflows[0], sources }), true);
  assert.equal(requiredWorkflowMatches({ run: { ...run, workflow_sha: 'b'.repeat(40) },
    expected: rules[0].parameters.workflows[0], sources }), false);
});

test('later clean security review submitted as review does not override clean code review', () => {
  const head = '8'.repeat(40), user = { login: 'chatgpt-codex-connector[bot]' };
  const comments = [
    { id: 10, body: '@codex review', created_at: '2026-11-12T10:00:00Z' },
    { id: 11, user, body: "Codex Review: Didn't find any major issues. Hooray!\nReviewed commit: " + head.slice(0,10),
      created_at: '2026-11-12T10:01:00Z' },
  ];
  const reviews = [{ id: 12, user, commit_id: head, submitted_at: '2026-11-12T10:02:00Z',
    body: '### 🛡️ Codex Security Review · Automatically triggered\nNo security issues were found' }];
  assert.doesNotThrow(() => verifyCodex({ head, comments, reviews, reactions: [], requestId: 10 }));
});

test('metadata-only security receipt with findings blocks older clean code review', () => {
  const head = '8'.repeat(40), user = { login: 'chatgpt-codex-connector[bot]' };
  const comments = [
    { id: 10, body: '@codex review', created_at: '2026-11-12T10:00:00Z' },
    { id: 11, user, body: "Codex Review: Didn't find any major issues. Hooray!\\nReviewed commit: " + head.slice(0,10),
      created_at: '2026-11-12T10:01:00Z' },
    { id: 12, user, body: '<!-- codex-security-review:v1 '+JSON.stringify({headSha:head,status:'completed'})+' -->\\nHere are some automated security review suggestions',
      created_at: '2026-11-12T10:02:00Z' },
  ];
  assert.throws(() => verifyCodex({ head, comments, reviews: [], reactions: [], requestId: 10 }), /security review/);
});
