const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { deploy, verifyCodex, verifyChecks, nextTag, StagingGit } = require('./auto-staging-deploy.cjs');

const HEAD = 'a'.repeat(40);
const STAGING = 'b'.repeat(40);
const COMMIT = 'c'.repeat(40);
const BOT = { login: 'chatgpt-codex-connector[bot]', type: 'Bot' };
const stamp = '2026-10-25T10:00:00Z';
const clean = { user: BOT, body: `Codex Review: Didn't find any major issues.\n**Reviewed commit:** \`${HEAD.slice(0, 10)}\``, created_at: stamp };
const stagingRun = { id: 1, name: 'Check PR commits exist in staging', path: '.github/workflows/check-commits-in-staging.yml', head_sha: HEAD, check_suite_id: 10, event: 'pull_request', pull_requests: [{ number: 42 }], status: 'completed', conclusion: 'failure' };
const validationRun = { id: 2, name: 'Validate tests', path: '.github/workflows/unit-tests.yml', head_sha: HEAD, check_suite_id: 20, event: 'pull_request', pull_requests: [{ number: 42 }], status: 'completed', conclusion: 'success' };
const stagingCheck = { id: 1, name: 'check-commits', check_suite: { id: 10 }, app: { id: 15368, slug: 'github-actions' }, status: 'completed', conclusion: 'failure' };
const validationCheck = { id: 2, name: 'unit-tests', check_suite: { id: 20 }, app: { id: 15368 }, status: 'completed', conclusion: 'success' };

function fixture(overrides = {}) {
  const calls = [];
  const data = {
    pr: { number: 42, state: 'open', base: { ref: 'main' }, head: { sha: HEAD, ref: 'feature/7064' }, merge_commit_sha: null },
    permission: 'write', comments: [clean], reviews: [], reactions: [], threads: [],
    rules: [
      { type: 'required_status_checks', parameters: { required_status_checks: [{ context: 'check-commits' }, { context: 'unit-tests' }] } },
      { type: 'workflows', parameters: { workflows: [{ path: '.github/workflows/check-commits-in-staging.yml' }, { path: '.github/workflows/unit-tests.yml' }] } },
    ],
    protection: null, checks: [stagingCheck, validationCheck], statuses: [], runs: [stagingRun, validationRun],
    deployment: { id: 30, head_sha: COMMIT, head_branch: 'dev-v1.2.4', event: 'workflow_dispatch', status: 'completed', conclusion: 'success', html_url: 'https://example.test/run/30' },
    labelExists: true,
    ...overrides,
  };
  const github = {
    request: async (route, args) => {
      calls.push({ route, args });
      if (route.endsWith('/collaborators/{username}/permission')) return { data: { permission: data.permission } };
      if (route === 'GET /repos/{owner}/{repo}/pulls/{pull_number}') return { data: { ...data.pr, head: { ...data.pr.head } } };
      if (route === 'POST /repos/{owner}/{repo}/issues/{issue_number}/comments') return { data: {} };
      if (route.endsWith('/actions/runs/{run_id}')) return { data: data.deployment };
      if (route.endsWith('/actions/workflows/{workflow_id}')) return { data: { id: 10, state: 'active' } };
      if (route.endsWith('/actions/jobs/{job_id}/rerun')) return { data: {} };
      if (route === 'GET /repos/{owner}/{repo}/labels/{name}') {
        if (!data.labelExists) throw Object.assign(new Error('Label not found'), { status: 404 });
        return { data: { name: 'force-deployed' } };
      }
      if (route === 'POST /repos/{owner}/{repo}/labels') { data.labelExists = true; return { data: {} }; }
      if (route === 'POST /repos/{owner}/{repo}/issues/{issue_number}/labels') return { data: {} };
      throw new Error(`Unexpected request: ${route}`);
    },
    paginate: async (route, args) => {
      calls.push({ route, args });
      if (route.endsWith('/issues/{issue_number}/comments')) return data.comments;
      if (route.endsWith('/pulls/{pull_number}/reviews')) return data.reviews;
      if (route.endsWith('/comments/{comment_id}/reactions')) return data.reactions;
      if (route.endsWith('/rules/branches/{branch}')) return data.rules;
      if (route.endsWith('/commits/{ref}/check-runs')) return data.checks;
      if (route.endsWith('/commits/{ref}/statuses')) return data.statuses;
      if (route.endsWith('/actions/runs')) return data.runs;
      if (route.endsWith('/actions/workflows/{workflow_id}/runs')) return data.dispatched || data.existing ? [data.deployment] : [];
      if (route.endsWith('/actions/runs/{run_id}/jobs')) return [{ id: 90, name: 'check-commits', status: 'completed', conclusion: 'failure' }];
      throw new Error(`Unexpected pagination: ${route}`);
    },
    graphql: async (query) => {
      calls.push({ route: 'graphql', query });
      if (query.includes('reviewThreads')) return { repository: { pullRequest: { reviewThreads: { nodes: data.threads, pageInfo: { hasNextPage: false } } } } };
      if (query.includes('branchProtectionRule')) return { repository: { pullRequest: { headRefOid: data.pr.head.sha, baseRef: { branchProtectionRule: data.protection } } } };
      throw new Error('Unexpected GraphQL query');
    },
  };
  const git = {
    staging: STAGING,
    prepare: async () => { calls.push({ route: 'git.prepare' }); return STAGING; },
    existing: async () => data.existing,
    publish: async () => { calls.push({ route: 'git.publish' }); if (data.conflict) throw new Error('Merge conflict in staging.\nFiles: `a.txt`\nUse conflict/resolved/7064'); return { tag: 'dev-v1.2.4', commit: COMMIT }; },
    assertStaging: async () => { if (data.stagingMoved) throw new Error('Staging advanced before dispatch'); },
  };
  const context = { eventName: 'issue_comment', repo: { owner: 'org', repo: 'repo' }, payload: { action: 'created', issue: { number: 42, pull_request: {} }, comment: { id: 99, body: '/deploy-staging', user: { login: 'writer' } } } };
  const core = { setFailed: (message) => calls.push({ route: 'failed', message }), setOutput: () => {} };
  const command = async (file, args) => { calls.push({ route: file, args }); data.dispatched = true; return ''; };
  return { data, github, context, core, git, command, calls, sleep: async () => {}, now: () => Date.parse(stamp) };
}

function forceFixture(overrides = {}, reason = 'Verify urgent staging fix') {
  const f = fixture(overrides);
  f.context.payload.comment.body = `/force-deploy-staging${reason === undefined ? '' : ` ${reason}`}`;
  f.forceDeployUsers = 'writer';
  return f;
}

test('temporary exemption ignores validate-tests and its required workflow in normal and force mode', async () => {
  for (const make of [fixture, forceFixture]) {
    const f = make();
    f.data.rules.push(
      { type: 'required_status_checks', parameters: { required_status_checks: [{ context: 'validate-tests' }] } },
      { type: 'workflows', parameters: { workflows: [{ path: '.github/workflows/pr-test-validation.yml' }] } },
    );
    f.data.runs.push({ ...validationRun, id: 50, path: '.github/workflows/pr-test-validation.yml', check_suite_id: 50, conclusion: 'failure' });
    f.data.checks.push({ ...validationCheck, id: 50, name: 'validate-tests', check_suite: { id: 50 }, conclusion: 'failure' });
    f.data.statuses.push({ id: 50, context: 'validate-tests', state: 'failure' });
    await deploy(f);
    assert(!f.calls.some(c => c.route === 'failed'), JSON.stringify(f.calls));
    assert(f.calls.some(c => c.route === 'gh'));
  }
});

test('temporary test-validation exemption does not exempt other required checks or workflows', () => {
  const f = fixture();
  const rules = [...f.data.rules,
    { type: 'required_status_checks', parameters: { required_status_checks: [{ context: 'validate-tests' }] } },
    { type: 'workflows', parameters: { workflows: [{ path: '.github/workflows/pr-test-validation.yml' }] } },
  ];
  const args = { head: HEAD, number: 42, protection: null, rules, checks: f.data.checks, statuses: [], runs: f.data.runs };
  assert.doesNotThrow(() => verifyChecks(args));
  assert.throws(() => verifyChecks({ ...args, checks: [stagingCheck, { ...validationCheck, conclusion: 'failure' }] }), /unit-tests/);
  assert.throws(() => verifyChecks({ ...args, runs: [stagingRun] }), /unit-tests.yml/);
});

test('allowlisted force command skips only Codex and human-decision gates and audits success', async () => {
  const f = forceFixture({ comments: [], threads: [{ isResolved: false, comments: { nodes: [{ body: '⚠️ Needs human decision: tax' }] } }] });
  await deploy(f);
  assert(!f.calls.some(c => c.route === 'failed'), JSON.stringify(f.calls));
  assert(f.calls.some(c => c.route === 'gh'));
  assert(!f.calls.some(c => c.route.endsWith('/reviews') || c.route.endsWith('/comments') && c.route.startsWith('GET ') || c.query?.includes('reviewThreads')));
  assert(f.calls.some(c => c.route.endsWith('/rules/branches/{branch}')));
  const success = f.calls.findIndex(c => c.args?.body?.startsWith('Deployed to staging:'));
  const audit = f.calls.findIndex(c => c.args?.body === '⚠️ Force-deployed by @writer: Verify urgent staging fix, skipped: Codex review, human-decision threads');
  const label = f.calls.findIndex(c => c.route === 'POST /repos/{owner}/{repo}/issues/{issue_number}/labels');
  assert(success >= 0 && audit > success && label > success);
  assert.deepEqual(f.calls[label].args.labels, ['force-deployed']);
  assert(f.calls.some(c => c.route.endsWith('/actions/jobs/{job_id}/rerun')));
});

test('force commands without a configured allowlist or exact user membership comment a reason and stop', async () => {
  for (const users of ['', 'writer-other', 'someone, another']) {
    const f = forceFixture(); f.forceDeployUsers = users; await deploy(f);
    assert(f.calls.some(c => c.route === 'failed' && c.message.includes('FORCE_DEPLOY_USERS')));
    assert(f.calls.some(c => c.args?.body?.startsWith('Staging deployment stopped:')));
    assert(!f.calls.some(c => c.route.startsWith('git.') || c.route === 'gh' || c.route.endsWith('/labels')));
  }
});

test('missing or whitespace-only force reasons are rejected before merge', async () => {
  for (const body of ['/force-deploy-staging', '/force-deploy-staging ', '/force-deploy-staging \t\n']) {
    const f = forceFixture(); f.context.payload.comment.body = body; await deploy(f);
    assert(f.calls.some(c => c.route === 'failed' && /reason/i.test(c.message)));
    assert(f.calls.some(c => c.args?.body?.startsWith('Staging deployment stopped:')));
    assert(!f.calls.some(c => c.route.startsWith('git.') || c.route === 'gh'));
  }
});

test('force allowlist accepts case-insensitive comma, space, and newline separated GitHub usernames', async () => {
  for (const users of ['someone, WRITER', 'someone\nwriter\nanother', 'someone writer another']) {
    const f = forceFixture(); f.forceDeployUsers = users; await deploy(f);
    assert(f.calls.some(c => c.route === 'gh'), users);
    assert(!f.calls.some(c => c.route === 'failed'), JSON.stringify(f.calls));
  }
});

test('force still requires repository write access and an open PR targeting main', async () => {
  for (const overrides of [{ permission: 'read' }, { pr: { state: 'closed', base: { ref: 'main' }, head: { sha: HEAD } } }, { pr: { state: 'open', base: { ref: 'staging' }, head: { sha: HEAD } } }]) {
    const f = forceFixture(overrides); await deploy(f);
    assert(f.calls.some(c => c.route === 'failed'));
    assert(!f.calls.some(c => c.route.startsWith('git.') || c.route === 'gh'));
  }
});

test('force does not bypass failed, pending, skipped, or missing required checks', async () => {
  for (const checks of [[stagingCheck], ...['failure', 'skipped', null].map(conclusion => [stagingCheck, { ...validationCheck, conclusion }])]) {
    const f = forceFixture({ comments: [], checks }); await deploy(f);
    assert(f.calls.some(c => c.route === 'failed' && c.message.includes('unit-tests')));
    assert(!f.calls.some(c => c.route === 'git.publish' || c.route === 'gh' || c.route.endsWith('/labels')));
  }
});

test('forced conflicts or failed CI never post force-deployed success or apply its label', async () => {
  for (const fail of [f => f.data.conflict = true, f => f.data.deployment.conclusion = 'failure']) {
    const f = forceFixture({ comments: [] }); fail(f); await deploy(f);
    assert(f.calls.some(c => c.route === 'failed'));
    assert(!f.calls.some(c => c.args?.body?.startsWith('⚠️ Force-deployed') || c.route.endsWith('/labels')));
  }
});

test('force success creates the label if absent, and preserves the supplied reason as plain data', async () => {
  const reason = 'QA requested verification; $(touch /tmp/never-run) `literal`';
  const f = forceFixture({ comments: [], labelExists: false }, reason); await deploy(f);
  assert(!f.calls.some(c => c.route === 'failed'), JSON.stringify(f.calls));
  assert(f.calls.some(c => c.route === 'POST /repos/{owner}/{repo}/labels' && c.args.name === 'force-deployed'));
  assert(f.calls.some(c => c.args?.body === `⚠️ Force-deployed by @writer: ${reason}, skipped: Codex review, human-decision threads`));
  assert(f.calls.filter(c => c.route === 'gh').every(c => !c.args.includes(reason)));
});

test('force prefix lookalikes, edited comments, and non-PR comments cannot deploy', async () => {
  for (const mutate of [f => f.context.payload.comment.body = '/force-deploy-stagingevil reason', f => f.context.payload.action = 'edited', f => delete f.context.payload.issue.pull_request]) {
    const f = forceFixture(); mutate(f); await deploy(f); assert.equal(f.calls.length, 0);
  }
});

test('workflow uses only explicit staging command and validates both test suites', () => {
  const text = fs.readFileSync(path.join(__dirname, '../workflows/auto-staging-deploy.yml'), 'utf8');
  assert(!text.includes('FORCE_DEPLOY_USERS:'));
  assert(text.includes("github.event.comment.body == '/deploy-staging'"));
  assert(text.includes('auto-staging-deploy.test.cjs'));
  assert(text.includes('auto-staging-pr.test.cjs'));
});

test('only a newly created exact command on a PR can trigger deployment', async () => {
  for (const mutate of [f => f.context.payload.action = 'edited', f => f.context.payload.comment.body = '/deploy-staging please', f => delete f.context.payload.issue.pull_request, f => f.context.eventName = 'schedule']) {
    const f = fixture(); mutate(f); await deploy(f); assert.equal(f.calls.length, 0);
  }
});

test('existing organization GitHub App credentials are used for staging promotion', () => {
  const yaml = fs.readFileSync(path.join(__dirname, '../workflows/auto-staging-deploy.yml'), 'utf8');
  assert.match(yaml, /app-id: \$\{\{ vars\.ORG_AGENTS_APP_ID \}\}/);
  assert.match(yaml, /private-key: \$\{\{ secrets\.ORG_AGENTS_APP_PRIVATE_KEY \}\}/);
  assert.match(yaml, /permission-contents: write/);
  assert.match(yaml, /permission-pull-requests: write/);
  assert.doesNotMatch(yaml, /STAGING_DEPLOY_APP_ID|STAGING_DEPLOY_APP_PRIVATE_KEY/);
  assert.match(yaml, /STAGING_PUSH_TOKEN: \$\{\{ steps\.staging-app\.outputs\.token \}\}/);
});

test('staging PR creation uses App-authenticated Octokit instead of runner CLI', () => {
  const source = fs.readFileSync(path.join(__dirname, 'auto-staging-pr.cjs'), 'utf8');
  assert.match(source, /appRequest\('POST \/repos\/\{owner\}\/\{repo\}\/pulls'/);
  assert.match(source, /appRequest\('PUT \/repos\/\{owner\}\/\{repo\}\/pulls\/\{pull_number\}\/merge'/);
  assert.doesNotMatch(source, /command\('gh'/);
});

test('workflow has exactly one comment trigger and uses the self-hosted runner', () => {
  const text = fs.readFileSync(path.join(__dirname, '../workflows/auto-staging-deploy.yml'), 'utf8');
  assert.match(text, /on:\n  issue_comment:\n    types: \[created\]\n\npermissions:/);
  assert.match(text, /runs-on: self-hosted/);
  assert.match(text, /ref: \$\{\{ github.sha \}\}/);
  assert.match(text, /group: auto-staging-deploy\n      cancel-in-progress: false/);
});

test('read access, closed PRs, and PRs targeting another branch are rejected with reasons', async () => {
  for (const overrides of [{ permission: 'read' }, { pr: { state: 'closed', base: { ref: 'main' }, head: { sha: HEAD } } }, { pr: { state: 'open', base: { ref: 'staging' }, head: { sha: HEAD } } }]) {
    const f = fixture(overrides); await deploy(f);
    assert(f.calls.some(c => c.route === 'failed'));
    assert(f.calls.some(c => c.route.startsWith('POST ') && c.args.body.includes('Staging deployment stopped:')));
    assert(!f.calls.some(c => c.route.startsWith('git.')));
  }
});

test('clean Codex text must come from the bot and match the current head; newer findings invalidate it', () => {
  assert.doesNotThrow(() => verifyCodex({ head: HEAD, comments: [clean], reviews: [], reactions: [] }));
  for (const comments of [[], [{ ...clean, user: { login: 'human', type: 'User' } }], [{ ...clean, body: clean.body.replace(HEAD.slice(0, 10), 'deadbeef00') }], [clean, { ...clean, created_at: '2026-10-25T10:01:00Z', body: `Codex Review: found problems. Reviewed commit: \`${HEAD}\`` }]]) {
    assert.throws(() => verifyCodex({ head: HEAD, comments, reviews: [], reactions: [] }), /Codex/);
  }
});

for (const login of ['chatgpt-codex-connector[bot]', 'codex[agent]']) {
  for (const format of ['Reviewed commit: SHA', '**Reviewed commit:** `SHA`', 'Reviewed at SHA', 'Reviewed at `SHA`']) {
    test(`${login} clean receipt accepts ${format} with a short head SHA`, () => {
      const body = `Codex Review: Didn't find any major issues.\n${format.replace('SHA', HEAD.slice(0, 10))}`;
      const receipt = { ...clean, user: { login, type: login === 'codex[agent]' ? 'Agent' : 'Bot' }, body };
      assert.doesNotThrow(() => verifyCodex({ head: HEAD, comments: [receipt], reviews: [], reactions: [] }));
    });
  }
}

test('Codex agent receipts still reject stale SHAs, unknown authors, and newer findings', () => {
  const agent = { login: 'codex[agent]', type: 'Agent' };
  const good = { ...clean, user: agent, body: `Didn't find any major issues. Reviewed at ${HEAD.slice(0, 7)}` };
  for (const comments of [
    [{ ...good, body: "Didn't find any major issues. Reviewed at deadbee" }],
    [{ ...good, user: { login: 'someone-else', type: 'Agent' } }],
    [good, { ...good, created_at: '2026-10-25T10:01:00Z', body: `Found a blocking issue. Reviewed at ${HEAD}` }],
  ]) {
    assert.throws(() => verifyCodex({ head: HEAD, comments, reviews: [], reactions: [] }), /Codex/);
  }
});

test('thumbs-up is accepted only from Codex on the latest review request and bound to this head', () => {
  const request = { id: 100, body: '@codex review', created_at: stamp };
  const summary = { user: BOT, body: `<!-- codex-review-state:v1 ${JSON.stringify({ code_review: { commit_sha: HEAD, status: 'completed', has_any_findings: false, has_unresolved_findings: false } })} -->`, updated_at: stamp, created_at: stamp };
  const reaction = { user: BOT, content: '+1', created_at: stamp };
  assert.doesNotThrow(() => verifyCodex({ head: HEAD, comments: [request, summary], reviews: [], reactions: [reaction], requestId: 100 }));
  for (const changes of [{ requestId: 99 }, { reactions: [{ ...reaction, user: { login: 'writer' } }] }, { comments: [request, { ...summary, body: summary.body.replace(HEAD, 'd'.repeat(40)) }] }]) {
    assert.throws(() => verifyCodex({ head: HEAD, comments: [request, summary], reviews: [], reactions: [reaction], requestId: 100, ...changes }), /Codex/);
  }
});

test('failed, pending, skipped, neutral and missing non-exempt required checks block', () => {
  const f = fixture();
  const args = { head: HEAD, number: 42, protection: null, rules: f.data.rules, checks: f.data.checks, statuses: [], runs: f.data.runs };
  assert.doesNotThrow(() => verifyChecks(args));
  for (const conclusion of ['failure', 'skipped', 'neutral', null]) {
    assert.throws(() => verifyChecks({ ...args, checks: [stagingCheck, { ...validationCheck, conclusion }] }), /unit-tests/);
  }
  assert.throws(() => verifyChecks({ ...args, checks: [stagingCheck] }), /unit-tests/);
  assert.throws(() => verifyChecks({ ...args, runs: [stagingRun] }), /unit-tests.yml/);
  assert.throws(() => verifyChecks({ ...args, rules: [...args.rules, { type: 'required_status_checks', parameters: { required_status_checks: [{ context: 'missing' }] } }] }), /missing/);
});

test('latest check attempt and required app are honored, and unrelated failed checks do not block', () => {
  const f = fixture();
  const args = { head: HEAD, number: 42, protection: null, rules: f.data.rules, checks: f.data.checks, statuses: [], runs: f.data.runs };
  assert.doesNotThrow(() => verifyChecks({ ...args, checks: [{ ...validationCheck, id: 0, conclusion: 'failure' }, ...args.checks, { id: 3, name: 'optional', conclusion: 'failure' }] }));
  assert.throws(() => verifyChecks({ ...args, checks: [...args.checks, { ...validationCheck, id: 10, conclusion: 'failure' }] }), /unit-tests/);
  assert.throws(() => verifyChecks({ ...args, rules: [{ type: 'required_status_checks', parameters: { required_status_checks: [{ context: 'unit-tests', integration_id: 123 }] } }] }), /unit-tests/);
  assert.throws(() => verifyChecks({ ...args, statuses: [{ id: 8, context: 'unit-tests', state: 'failure' }] }), /unit-tests/);
});

test('only unresolved threads containing the human-decision marker block deployment', async () => {
  const marker = { id: 'thread', isResolved: false, comments: { nodes: [{ body: '⚠️ Needs human decision: confirm tax', url: 'https://example.test/thread' }], pageInfo: { hasNextPage: false } } };
  const f = fixture({ threads: [marker] }); await deploy(f);
  assert(f.calls.some(c => c.route === 'failed' && /human decision/.test(c.message)));
  assert(!f.calls.some(c => c.route === 'git.publish'));
  for (const thread of [{ ...marker, isResolved: true }, { ...marker, comments: { nodes: [{ body: 'Please rename this' }], pageInfo: { hasNextPage: false } } }]) {
    const ready = fixture({ threads: [thread] }); await deploy(ready); assert(ready.calls.some(c => c.route === 'git.publish'));
  }
});

test('ready PR dispatches the tag, waits for success, comments exact result, then reruns staging check', async () => {
  const f = fixture(); await deploy(f);
  assert(!f.calls.some(c => c.route === 'failed'), JSON.stringify(f.calls));
  const dispatch = f.calls.findIndex(c => c.route === 'gh');
  assert.deepEqual(f.calls[dispatch].args, ['workflow', 'run', 'cicd.yml', '--ref', 'dev-v1.2.4', '--repo', 'org/repo']);
  const comment = f.calls.findIndex(c => c.args?.body === `Deployed to staging: dev-v1.2.4, ${COMMIT}, please verify`);
  const rerun = f.calls.findIndex(c => c.route.endsWith('/actions/jobs/{job_id}/rerun'));
  assert(dispatch > 0 && comment > dispatch && rerun > comment);
});

test('failed deployment never comments success or reruns staging check', async () => {
  const f = fixture(); f.data.deployment.conclusion = 'failure'; await deploy(f);
  assert(f.calls.some(c => c.route === 'failed'));
  assert(!f.calls.some(c => c.args?.body?.startsWith('Deployed to staging:')));
  assert(!f.calls.some(c => c.route.endsWith('/rerun')));
});

test('conflict stops before dispatch and reports manual recovery', async () => {
  const f = fixture({ conflict: true }); await deploy(f);
  assert(f.calls.some(c => c.args?.body?.includes('conflict/resolved/7064')));
  assert(!f.calls.some(c => c.route === 'gh'));
});

test('an already successful tag is not deployed again', async () => {
  const f = fixture({ existing: { tag: 'dev-v1.2.4', commit: COMMIT } }); await deploy(f);
  assert(!f.calls.some(c => c.route === 'git.publish' || c.route === 'gh'));
  assert(f.calls.some(c => c.route.endsWith('/rerun')));
});

test('a PR head that changes during preparation cannot be merged or dispatched', async () => {
  const f = fixture(); f.git.prepare = async () => { f.data.pr.head.sha = 'd'.repeat(40); return STAGING; };
  await deploy(f); assert(f.calls.some(c => c.route === 'failed' && /head changed/.test(c.message)));
  assert(!f.calls.some(c => c.route === 'git.publish' || c.route === 'gh'));
});

test('a concurrent staging advance stops dispatch rather than deploying an older snapshot', async () => {
  const f = fixture({ stagingMoved: true }); await deploy(f);
  assert(f.calls.some(c => c.route === 'failed' && c.message.includes('Staging advanced')));
  assert(!f.calls.some(c => c.route === 'gh'));
});

test('every page of thread comments is checked, including marker replies after the first hundred', async () => {
  const f = fixture({ threads: [{ id: 'thread', isResolved: false, comments: { nodes: [], pageInfo: { hasNextPage: true, endCursor: 'page1' } } }] });
  const original = f.github.graphql;
  f.github.graphql = async (query, args) => query.includes('node(id:$id)')
    ? { node: { comments: { nodes: [{ body: '⚠️ Needs human decision: business rule', url: 'https://example.test/late-reply' }], pageInfo: { hasNextPage: false } } } }
    : original(query, args);
  await deploy(f);
  assert(f.calls.some(c => c.route === 'failed' && c.message.includes('late-reply')));
  assert(!f.calls.some(c => c.route === 'git.publish'));
});

test('a checks API failure stops before merge and comments the reason', async () => {
  const f = fixture(); const original = f.github.paginate;
  f.github.paginate = async (route, args) => {
    if (route.endsWith('/rules/branches/{branch}')) throw new Error('Rules API unavailable');
    return original(route, args);
  };
  await deploy(f);
  assert(f.calls.some(c => c.args?.body?.includes('Rules API unavailable')));
  assert(!f.calls.some(c => c.route === 'git.publish'));
});

test('required test-merge results take precedence over green head checks', async () => {
  const f = fixture(); f.data.pr.merge_commit_sha = 'e'.repeat(40);
  const original = f.github.paginate;
  f.github.paginate = async (route, args) => route.endsWith('/check-runs') && args.ref === f.data.pr.merge_commit_sha
    ? [stagingCheck, { ...validationCheck, conclusion: 'failure' }] : original(route, args);
  await deploy(f);
  assert(f.calls.some(c => c.route === 'failed' && c.message.includes('unit-tests')));
  assert(!f.calls.some(c => c.route === 'git.publish'));
});

test('staging App credentials are only used for the staging branch; tag push keeps GITHUB_TOKEN', async () => {
  const calls = [];
  const run = async (file, args, options) => {
    calls.push({ file, args, options });
    if (args.includes('merge-tree')) return `${'d'.repeat(40)}\0`;
    if (args.includes('tag') && args.includes('--list')) return 'dev-v1.2.3\n';
    if (args.includes('show')) return 'on:\n  workflow_dispatch:\njobs:\n';
    return '';
  };
  const git = new StagingGit(run, { useStagingApp: true }); git.staging = STAGING;
  await git.publish({ number: 42, head: HEAD, issue: '7064', assertCurrent: async () => {} });
  const pushes = calls.filter(c => c.args.includes('push'));
  assert.equal(pushes.length, 2);
  assert.equal(pushes[0].options.stagingAuth, true);
  assert(pushes[0].args.at(-1).endsWith(':refs/heads/staging'));
  assert.equal(pushes[1].options, undefined);
  assert.equal(pushes[1].args.at(-1), 'refs/tags/dev-v1.2.4:refs/tags/dev-v1.2.4');
});

test('a merge tree without a dispatchable cicd.yml is rejected before pushing', async () => {
  const calls = [];
  const git = new StagingGit(async (file, args) => {
    calls.push(args);
    if (args.includes('merge-tree')) return `${'d'.repeat(40)}\0`;
    if (args.includes('show')) return 'on:\n  push:\n    tags: [dev-*]\n';
    return '';
  });
  git.staging = STAGING;
  await assert.rejects(git.publish({ number: 42, head: HEAD, issue: '7064', assertCurrent: async () => {} }), /workflow_dispatch/);
  assert(!calls.some(args => args.includes('push')));
});

test('tag sequence increments the greatest semantic version patch, ignoring unrelated tags', () => {
  assert.equal(nextTag([]), 'dev-v0.0.1');
  assert.equal(nextTag(['dev-v1.9.99', 'dev-v1.10.3', 'prod-v9.0.0', 'dev-foo']), 'dev-v1.10.4');
  assert.equal(nextTag(['dev-v9007199254740993.0.9']), 'dev-v9007199254740993.0.10');
});

test('real git merge publishes staging and an annotated tag without altering feature or main', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'staging-git-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const cmd = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  const remote = path.join(dir, 'origin.git'); cmd(dir, 'init', '--bare', remote);
  const repo = path.join(dir, 'repo'); cmd(dir, 'clone', remote, repo);
  cmd(repo, 'config', 'user.name', 'test'); cmd(repo, 'config', 'user.email', 'test@example.test');
  fs.mkdirSync(path.join(repo, '.github/workflows'), { recursive: true });
  fs.writeFileSync(path.join(repo, '.github/workflows/cicd.yml'), 'on:\n  workflow_dispatch:\njobs:\n');
  fs.writeFileSync(path.join(repo, 'base'), 'base\n'); cmd(repo, 'add', '.'); cmd(repo, 'commit', '-m', 'base'); cmd(repo, 'branch', '-M', 'main');
  cmd(repo, 'branch', 'staging'); cmd(repo, 'switch', '-c', 'feature/7064');
  fs.writeFileSync(path.join(repo, 'feature'), 'feature\n'); cmd(repo, 'add', '.'); cmd(repo, 'commit', '-m', 'feature');
  const head = cmd(repo, 'rev-parse', 'HEAD'); const main = cmd(repo, 'rev-parse', 'main');
  cmd(repo, 'push', 'origin', 'main', 'staging', 'feature/7064'); cmd(repo, 'update-ref', 'refs/pull/42/head', head); cmd(repo, 'push', 'origin', 'refs/pull/42/head');
  const run = async (file, args) => execFileSync(file, args, { cwd: repo, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  const git = new StagingGit(run);
  await git.prepare(42, head);
  const result = await git.publish({ number: 42, head, issue: '7064', assertCurrent: async () => {} });
  assert.equal(result.tag, 'dev-v0.0.1');
  assert.equal(cmd(remote, 'rev-parse', 'main'), main);
  assert.equal(cmd(remote, 'rev-parse', 'feature/7064'), head);
  assert.equal(cmd(repo, 'branch', '--show-current'), 'feature/7064');
  assert.equal(cmd(remote, 'rev-parse', 'staging'), result.commit);
  assert.equal(cmd(remote, 'rev-parse', `${result.tag}^{}`), result.commit);
  assert.equal(cmd(remote, 'cat-file', '-t', result.tag), 'tag');
  assert.deepEqual(await git.existing(42, head), result);
});

test('real git conflict lists files and manual steps, and pushes no refs', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'staging-conflict-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const cmd = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  const remote = path.join(dir, 'origin.git'); cmd(dir, 'init', '--bare', remote);
  const repo = path.join(dir, 'repo'); cmd(dir, 'clone', remote, repo);
  cmd(repo, 'config', 'user.name', 'test'); cmd(repo, 'config', 'user.email', 'test@example.test');
  fs.writeFileSync(path.join(repo, 'clash.txt'), 'base\n'); cmd(repo, 'add', '.'); cmd(repo, 'commit', '-m', 'base'); cmd(repo, 'branch', '-M', 'main');
  cmd(repo, 'switch', '-c', 'staging'); fs.writeFileSync(path.join(repo, 'clash.txt'), 'staging\n'); cmd(repo, 'commit', '-am', 'staging');
  cmd(repo, 'switch', '-c', 'feature/7064', 'main'); fs.writeFileSync(path.join(repo, 'clash.txt'), 'feature\n'); cmd(repo, 'commit', '-am', 'feature');
  const head = cmd(repo, 'rev-parse', 'HEAD'); const staging = cmd(repo, 'rev-parse', 'staging');
  cmd(repo, 'push', 'origin', 'main', 'staging', 'feature/7064'); cmd(repo, 'update-ref', 'refs/pull/42/head', head); cmd(repo, 'push', 'origin', 'refs/pull/42/head');
  const run = async (file, args) => execFileSync(file, args, { cwd: repo, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  const git = new StagingGit(run); await git.prepare(42, head);
  await assert.rejects(git.publish({ number: 42, head, issue: '7064', assertCurrent: async () => {} }), error => /clash.txt/.test(error.message) && /conflict\/resolved\/7064/.test(error.message) && /gh pr create --base staging/.test(error.message));
  assert.equal(cmd(remote, 'rev-parse', 'staging'), staging);
  assert.equal(cmd(remote, 'rev-parse', 'feature/7064'), head);
  assert.equal(cmd(remote, 'tag'), '');
});
