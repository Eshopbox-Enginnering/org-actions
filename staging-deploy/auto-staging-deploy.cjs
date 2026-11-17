'use strict';

const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const execute = promisify(execFile);
const STAGING_CHECK = 'Check PR commits exist in staging';
const STAGING_WORKFLOW = '.github/workflows/check-commits-in-staging.yml';
const CODEX_AUTHORS = new Set(['chatgpt-codex-connector[bot]', 'codex[agent]']);
const WORKFLOW = 'cicd.yml';
const time = value => Date.parse(value || '') || 0;
// GitHub supplies the authenticated login. Codex agent identities need not have
// the same REST user.type as the connector bot.
const bot = value => CODEX_AUTHORS.has(value.user?.login);
const workflowPath = run => (run.path || '').split('@')[0];
const stagingWorkflow = run => run.name === STAGING_CHECK && workflowPath(run) === STAGING_WORKFLOW;
const newest = values => [...values].sort((a, b) => Number(b.id) - Number(a.id))[0];
const latestChecks = checks => {
  const groups = new Map();
  for (const check of checks) {
    const key = `${check.name}:${check.app?.id}`;
    groups.set(key, newest([...(groups.has(key) ? [groups.get(key)] : []), check]));
  }
  return [...groups.values()];
};

async function command(file, args, cwd, { stagingAuth = false } = {}) {
  const env = { ...process.env, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', GIT_ATTR_NOSYSTEM: '1' };
  if (stagingAuth) {
    if (!env.STAGING_PUSH_TOKEN) throw new Error('Staging branch writer token is missing.');
    const key = `http.${new URL(process.env.GITHUB_SERVER_URL || 'https://github.com').origin}/.extraheader`;
    // Reset checkout's header, then override it in this process only. Never
    // persist the App credential or use it to push a tag (which would auto-run CI).
    env.GIT_CONFIG_COUNT = '2';
    env.GIT_CONFIG_KEY_0 = key;
    env.GIT_CONFIG_VALUE_0 = '';
    env.GIT_CONFIG_KEY_1 = key;
    env.GIT_CONFIG_VALUE_1 = `AUTHORIZATION: basic ${Buffer.from(`x-access-token:${env.STAGING_PUSH_TOKEN}`).toString('base64')}`;
  }
  const { stdout } = await execute(file, args, {
    cwd, maxBuffer: 16 * 1024 * 1024,
    // Do not allow runner-global hooks, attributes, or custom merge drivers.
    env,
  });
  return stdout;
}

function verifyCodex({ head, comments, reviews, reactions, requestId }) {
  const requests = comments.filter(c => /(^|\n)\s*@codex\s+review\b/i.test(c.body || ''));
  const latestRequest = [...requests].sort((a, b) => time(b.created_at) - time(a.created_at) || Number(b.id) - Number(a.id))[0];
  const receipts = [
    ...reviews.filter(bot).map(r => ({ ...r, date: r.submitted_at, sha: r.commit_id })),
    ...comments.filter(bot).filter(c => !c.body.includes('codex-review-state:v1')).map(c => ({ ...c, date: c.created_at })),
  ].sort((a, b) => time(b.date) - time(a.date) || Number(b.id) - Number(a.id));
  const latest = receipts[0];
  const reviewed = latest?.sha || latest?.body.match(/\bReviewed\s+(?:commit\s*:|at\b)[\s*`_]*([a-f0-9]{7,40})\b/i)?.[1];
  if (latest && reviewed && head.startsWith(reviewed) &&
      /\bDidn['’]t find any major issues\b/i.test(latest.body) &&
      time(latest.date) >= time(latestRequest?.created_at)) return;

  // A reaction has no commit_id. Bind it to the current head through Codex's
  // authenticated summary; never infer its SHA from a commit's author date.
  const summaries = comments.filter(bot).flatMap(c => {
    const match = c.body.match(/<!-- codex-review-state:v1 (.*?) -->/s);
    if (!match) return [];
    try { return [{ state: JSON.parse(match[1]).code_review, date: c.updated_at || c.created_at }]; }
    catch { return []; }
  }).sort((a, b) => time(b.date) - time(a.date));
  const summary = summaries[0];
  const approval = reactions.filter(r => bot(r) && r.content === '+1' && time(r.created_at) >= time(latestRequest?.created_at))
    .sort((a, b) => time(b.created_at) - time(a.created_at))[0];
  const explicitHead = latestRequest?.body.match(/@codex\s+review\s+([a-f0-9]{40})\b/i)?.[1] === head;
  const bound = summary?.state?.commit_sha === head && summary.state.status === 'completed' &&
    summary.state.has_any_findings === false && summary.state.has_unresolved_findings === false &&
    time(summary.date) >= time(latestRequest?.created_at);
  if (latestRequest && latestRequest.id === requestId && approval && (bound || explicitHead) &&
      (!latest || time(latest.date) <= time(approval.created_at))) return;
  throw new Error(`Codex has not cleared PR head ${head}. The latest Codex review must say "Didn't find any major issues" for this SHA, or Codex must react 👍 to the latest @codex review request with a summary bound to this SHA. Request a fresh @codex review if the head changed.`);
}

function verifyChecks({ head, number, protection, rules, checks, statuses, runs }) {
  const applicable = runs.filter(r => r.head_sha === head && r.event === 'pull_request' &&
    (r.pull_requests || []).some(pr => pr.number === number));
  const required = [
    ...(protection?.requiresStatusChecks ? protection.requiredStatusChecks.map(c => ({ context: c.context, integration_id: c.app?.databaseId })) : []),
    ...rules.filter(r => r.type === 'required_status_checks').flatMap(r => r.parameters.required_status_checks),
  ];
  const requiredWorkflows = rules.filter(r => r.type === 'workflows').flatMap(r => r.parameters.workflows);
  const problems = [];
  const stagingSuites = new Set(applicable.filter(stagingWorkflow).map(r => r.check_suite_id));
  for (const expected of required) {
    if (expected.context === STAGING_CHECK) continue;
    const matching = checks.filter(c => c.name === expected.context &&
      (expected.integration_id == null || expected.integration_id < 0 || c.app?.id === expected.integration_id));
    // The org staging-presence workflow calls its job "check-commits".
    if (expected.context === 'check-commits' && matching.length && matching.every(c => stagingSuites.has(c.check_suite?.id))) continue;
    if (expected.context === 'check-commits' && !matching.length && requiredWorkflows.some(w => w.path === STAGING_WORKFLOW)) continue;
    const currentChecks = latestChecks(matching);
    const status = newest(statuses.filter(s => s.context === expected.context));
    if (!currentChecks.length && !status) problems.push(`${expected.context}: missing`);
    for (const check of currentChecks) {
      if (check.status !== 'completed' || check.conclusion !== 'success') problems.push(`${expected.context}: ${check.conclusion || check.status}`);
    }
    // When both a status and a check run use the same context, both must pass.
    if (status && status.state !== 'success') problems.push(`${expected.context}: ${status.state}`);
    if (expected.integration_id > 0 && !currentChecks.length) problems.push(`${expected.context}: missing result from required app ${expected.integration_id}`);
  }
  for (const expected of requiredWorkflows) {
    if (expected.path === STAGING_WORKFLOW) continue;
    const run = newest(applicable.filter(r => workflowPath(r) === expected.path));
    if (!run) { problems.push(`${expected.path}: missing required workflow`); continue; }
    if (run.status !== 'completed' || run.conclusion !== 'success') problems.push(`${expected.path}: ${run.conclusion || run.status}`);
    const jobs = latestChecks(checks.filter(c => c.check_suite?.id === run.check_suite_id));
    if (!jobs.length) problems.push(`${expected.path}: missing job checks`);
    for (const check of jobs) {
      if (check.status !== 'completed' || check.conclusion !== 'success') problems.push(`${check.name}: ${check.conclusion || check.status}`);
    }
  }
  if (problems.length) throw new Error(`Required checks are not green:\n${[...new Set(problems)].map(p => `- ${p}`).join('\n')}`);
}

function nextTag(tags) {
  const versions = tags.map(tag => /^dev-v(\d+)\.(\d+)\.(\d+)$/.exec(tag)).filter(Boolean)
    .map(m => m.slice(1).map(BigInt)).sort((a, b) => {
      for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i] ? -1 : 1;
      return 0;
    });
  const [major, minor, patch] = versions[0] || [0n, 0n, 0n];
  return `dev-v${major}.${minor}.${patch + 1n}`;
}

class StagingGit {
  constructor(run, { useStagingApp = false } = {}) { this.run = run; this.useStagingApp = useStagingApp; }
  async git(...args) { return this.run('git', ['-c', 'core.hooksPath=/dev/null', ...args]); }
  async prepare(number, head) {
    await this.git('fetch', '--no-recurse-submodules', 'origin',
      '+refs/heads/staging:refs/remotes/origin/staging',
      `+refs/pull/${number}/head:refs/auto-staging/pr-head`, '+refs/tags/*:refs/tags/*');
    const fetched = (await this.git('rev-parse', 'refs/auto-staging/pr-head')).trim();
    if (fetched !== head) throw new Error('PR head changed while fetching. Retry /deploy-staging after the new head is reviewed.');
    this.staging = (await this.git('rev-parse', 'refs/remotes/origin/staging')).trim();
    return this.staging;
  }
  async existing(number, head) {
    const tags = (await this.git('for-each-ref', '--sort=-creatordate', '--format=%(refname:short)', 'refs/tags/dev-v*')).trim().split('\n').filter(Boolean);
    for (const tag of tags) {
      if (!/^dev-v\d+\.\d+\.\d+$/.test(tag)) continue;
      const type = (await this.git('cat-file', '-t', `refs/tags/${tag}`)).trim();
      if (type !== 'tag') continue;
      const body = await this.git('cat-file', '-p', `refs/tags/${tag}`);
      if (!body.includes(`auto-staging-deploy:v1 ${JSON.stringify({ pr: number, head })}`)) continue;
      const commit = (await this.git('rev-parse', `${tag}^{commit}`)).trim();
      try {
        await this.git('merge-base', '--is-ancestor', head, commit);
        await this.git('merge-base', '--is-ancestor', commit, this.staging);
      } catch { continue; }
      return { tag, commit };
    }
  }
  async assertStaging(commit) {
    const remote = (await this.git('ls-remote', 'origin', 'refs/heads/staging')).trim().split(/\s+/)[0];
    if (remote !== commit) throw new Error('Staging advanced before dispatch. Retry /deploy-staging to tag its current snapshot instead of deploying an older one.');
  }
  async publish({ number, head, issue, assertCurrent }) {
    let tree;
    try {
      tree = (await this.git('merge-tree', '--write-tree', '--name-only', '-z', this.staging, head)).split('\0')[0].trim();
    } catch (error) {
      if (error.code !== 1 && error.status !== 1) throw error;
      const fields = String(error.stdout || '').split('\0');
      const files = fields.slice(1, fields.indexOf('', 1)).filter(Boolean);
      const branch = `conflict/resolved/${issue || '<issue-no>'}`;
      throw new Error(`Merge conflict in staging. Conflicting files:\n${files.map(f => `- ${JSON.stringify(f)}`).join('\n')}\n\nResolve manually on a branch from staging:\n\n\`\`\`sh\ngit fetch origin staging pull/${number}/head\ngit switch -c ${branch} origin/staging\ngit merge --no-ff ${head}\n# Resolve the files above, then git add the resolved paths.\ngit commit\ngit push -u origin ${branch}\ngh pr create --base staging --head ${branch}\n\`\`\`\n\nMerge that conflict-resolution PR into staging, then comment /deploy-staging on this PR again. Do not merge staging into the feature branch or push to main.`);
    }
    if (!/^[a-f0-9]{40,64}$/.test(tree)) throw new Error('Git did not return a valid merge tree.');
    let workflow;
    try { workflow = await this.git('show', `${tree}:.github/workflows/${WORKFLOW}`); }
    catch { throw new Error(`The staging merge does not contain .github/workflows/${WORKFLOW}. No refs were pushed.`); }
    if (!/^\s+workflow_dispatch\s*:/m.test(workflow)) throw new Error(`${WORKFLOW} on the staging merge must enable workflow_dispatch. No refs were pushed.`);
    let commit = this.staging;
    try { await this.git('merge-base', '--is-ancestor', head, this.staging); }
    catch {
      commit = (await this.git('-c', 'user.name=github-actions[bot]', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
        'commit-tree', tree, '-p', this.staging, '-p', head, '-m', `Merge PR #${number} into staging`)).trim();
    }
    const tag = nextTag((await this.git('tag', '--list', 'dev-v*')).trim().split('\n'));
    await this.git('-c', 'user.name=github-actions[bot]', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
      'tag', '-a', tag, commit, '-m', `auto-staging-deploy:v1 ${JSON.stringify({ pr: number, head })}`);
    await assertCurrent();
    // No force push: a concurrent staging update or tag collision fails the
    // entire atomic transaction. Feature refs and main are never push targets.
    if (this.useStagingApp) {
      await this.run('git', ['-c', 'core.hooksPath=/dev/null', 'push', 'origin', `${commit}:refs/heads/staging`], { stagingAuth: true });
      // GITHUB_TOKEN suppresses tag-push workflows, so the explicit dispatch is
      // the only deployment. A failed tag push stops and can be retried safely.
      await this.git('push', 'origin', `refs/tags/${tag}:refs/tags/${tag}`);
    } else {
      await this.git('push', '--atomic', 'origin', `${commit}:refs/heads/staging`, `refs/tags/${tag}:refs/tags/${tag}`);
    }
    this.staging = commit;
    return { tag, commit };
  }
}

async function deploy({ github, context, core, git, command: run, forceDeployUsers = process.env.FORCE_DEPLOY_USERS || '', sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), now = Date.now }) {
  const event = context.payload;
  if (context.eventName !== 'issue_comment' || event.action !== 'created' || !event.issue?.pull_request) return;
  const forceCommand = /^\/force-deploy-staging(?:\s+([\s\S]*))?$/.exec(event.comment?.body || '');
  if (event.comment?.body !== '/deploy-staging' && !forceCommand) return;
  const force = Boolean(forceCommand);
  const reason = forceCommand?.[1]?.trim();
  const actor = event.comment.user.login;
  const repo = context.repo;
  const number = event.issue.number;
  const request = (route, args = {}) => github.request(route, { ...repo, ...args });
  const pages = (route, args = {}) => github.paginate(route, { ...repo, per_page: 100, ...args });
  const statusMarker = '<!-- central-staging-deploy:v1 -->';
  const statusUrl = `${context.serverUrl}/${repo.owner}/${repo.repo}`;
  let statusId;
  let stage = '⏳ Initializing';
  const status = async (message) => {
    stage = message;
    const body = `${statusMarker}
## 🚀 Staging deployment · PR #${number}
**Status:** ${stage}

| Step | Result |
|---|---|
| Authorization & review | ${process.env.STAGE_REVIEW || '⏳ Waiting'} |
| Required checks | ${process.env.STAGE_CHECKS || '⏳ Waiting'} |
| Staging PR | ${process.env.STAGE_PR || '⏳ Waiting'} |
| Tag & CI/CD | ${process.env.STAGE_DEPLOY || '⏳ Waiting'} |

**Main PR:** [#${number}](${statusUrl}/pull/${number})
**Workflow run:** [View execution](${statusUrl}/actions/runs/${context.runId})
${process.env.STAGING_PR_URL ? `**Staging PR:** [Open staging PR](${process.env.STAGING_PR_URL})\n` : ''}${process.env.STAGING_TAG ? `**Tag:** [${process.env.STAGING_TAG}](${statusUrl}/tree/${process.env.STAGING_TAG})\n` : ''}${process.env.STAGING_RUN_URL ? `**Deployment run:** [View CI/CD](${process.env.STAGING_RUN_URL})\n` : ''}
${message.startsWith('❌') ? '**Action required:** Review the failure and retry \`/deploy-staging\` after fixing it.' : ''}`;
    if (!statusId) {
      const existing = await pages('GET /repos/{owner}/{repo}/issues/{issue_number}/comments', { issue_number: number });
      const mine = [...existing].reverse().find(c => c.user?.login === 'github-actions[bot]' && c.body?.includes(statusMarker));
      statusId = mine?.id;
    }
    if (statusId) await request('PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}', { comment_id: statusId, body });
    else statusId = (await request('POST /repos/{owner}/{repo}/issues/{issue_number}/comments', { issue_number: number, body })).data.id;
  };
  const comment = body => status(body.startsWith('Staging deployment stopped:') ? `❌ ${body.replace('Staging deployment stopped:', '').trim()}` : body);

  const getPR = async () => (await request('GET /repos/{owner}/{repo}/pulls/{pull_number}', { pull_number: number })).data;
  let published;
  try {
    await status('⏳ Validating requester, PR and review');
    const access = (await request('GET /repos/{owner}/{repo}/collaborators/{username}/permission', { username: event.comment.user.login })).data;
    if (!['write', 'maintain', 'admin'].includes(access.permission)) throw new Error(`@${actor} needs repository write access to deploy to staging.`);
    if (force) {
      const allowed = new Set(forceDeployUsers.split(/[\s,]+/).filter(Boolean).map(user => user.toLowerCase()));
      if (!allowed.has(actor.toLowerCase())) throw new Error(`@${actor} is not listed in the FORCE_DEPLOY_USERS repository variable. Force deployment is not allowed.`);
      if (!reason) throw new Error('A reason is required. Use /force-deploy-staging <reason>.');
    }
    const pr = await getPR();
    if (pr.state !== 'open' || pr.base.ref !== 'main') throw new Error('Only open PRs targeting main can deploy to staging.');
    const head = pr.head.sha;
    const assertCurrent = async () => {
      const current = await getPR();
      if (current.state !== 'open' || current.base.ref !== 'main' || current.head.sha !== head) throw new Error('PR head changed or PR is no longer open against main. Retry /deploy-staging after reviewing the current head.');
    };
    const reviewGates = async () => {
      const comments = await pages('GET /repos/{owner}/{repo}/issues/{issue_number}/comments', { issue_number: number });
      const reviews = await pages('GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews', { pull_number: number });
      const latestRequest = comments.filter(c => /(^|\n)\s*@codex\s+review\b/i.test(c.body || ''))
        .sort((a, b) => time(b.created_at) - time(a.created_at) || Number(b.id) - Number(a.id))[0];
      const reactions = latestRequest ? await pages('GET /repos/{owner}/{repo}/issues/comments/{comment_id}/reactions', { comment_id: latestRequest.id }) : [];
      try {
        verifyCodex({ head, comments, reviews, reactions, requestId: latestRequest?.id });
      } catch (codexError) {
        const quotaEvidence = comments.filter(c => ['chatgpt-codex-connector[bot]', 'codex[agent]'].includes(c.user?.login))
          .some(c => /(?:rate limit|usage limit|quota exceeded|review limit reached)/i.test(c.body || '') &&
            time(c.created_at) >= time(latestRequest?.created_at) && time(c.created_at) >= time(pr.created_at));
        if (!quotaEvidence) throw codexError;
        const human = [...reviews].filter(r => r.state === 'APPROVED' && r.commit_id === head &&
          r.user?.login !== pr.user.login && time(r.submitted_at) >= time(pr.created_at))
          .sort((a,b) => time(b.submitted_at) - time(a.submitted_at));
        const latestHuman = human[0];
        if (!latestHuman) throw new Error('Codex quota exhausted: an authorized human approval on the current head is required.');
        const permission = (await request('GET /repos/{owner}/{repo}/collaborators/{username}/permission', { username: latestHuman.user.login })).data.permission;
        if (!['write', 'maintain', 'admin'].includes(permission)) throw new Error('Human review fallback requires a reviewer with repository write access.');
        // A newer CHANGES_REQUESTED review invalidates the approval.
        if (reviews.some(r => r.user?.login === latestHuman.user.login && r.state === 'CHANGES_REQUESTED' && time(r.submitted_at) > time(latestHuman.submitted_at)))
          throw new Error('The human reviewer requested changes after their approval.');
      }
      let cursor;
      do {
        const result = await github.graphql(`query($owner:String!,$repo:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) { nodes { id isResolved comments(first:100) { nodes { body url } pageInfo { hasNextPage endCursor } } } pageInfo { hasNextPage endCursor } }
          } }
        }`, { ...repo, number, cursor });
        const connection = result.repository.pullRequest.reviewThreads;
        for (const thread of connection.nodes) {
          if (thread.isResolved) continue;
          let chunk = thread.comments;
          while (true) {
            const blocked = chunk.nodes.find(c => c.body.includes('⚠️ Needs human decision'));
            if (blocked) throw new Error(`Open review thread needs human decision: ${blocked.url}`);
            if (!chunk.pageInfo.hasNextPage) break;
            const more = await github.graphql(`query($id:ID!,$cursor:String!) { node(id:$id) { ... on PullRequestReviewThread { comments(first:100,after:$cursor) { nodes { body url } pageInfo { hasNextPage endCursor } } } } }`, { id: thread.id, cursor: chunk.pageInfo.endCursor });
            chunk = more.node.comments;
          }
        }
        cursor = connection.pageInfo.hasNextPage ? connection.pageInfo.endCursor : undefined;
      } while (cursor);
    };
    const gates = async () => {
      if (!force) await reviewGates();
      const result = await github.graphql(`query($owner:String!,$repo:String!,$number:Int!) {
        repository(owner:$owner,name:$repo) { pullRequest(number:$number) { headRefOid baseRef { branchProtectionRule { requiresStatusChecks requiredStatusChecks { context app { databaseId } } } } } }
      }`, { ...repo, number });
      if (result.repository.pullRequest.headRefOid !== head) throw new Error('PR head changed while checking requirements.');
      const rules = await pages('GET /repos/{owner}/{repo}/rules/branches/{branch}', { branch: 'main' });
      const runs = await pages('GET /repos/{owner}/{repo}/actions/runs', { head_sha: head });
      let checks = await pages('GET /repos/{owner}/{repo}/commits/{ref}/check-runs', { ref: head, filter: 'latest' });
      let statuses = await pages('GET /repos/{owner}/{repo}/commits/{ref}/statuses', { ref: head });
      // GitHub gives test-merge results precedence when they exist.
      if (pr.merge_commit_sha) {
        const mergeChecks = await pages('GET /repos/{owner}/{repo}/commits/{ref}/check-runs', { ref: pr.merge_commit_sha, filter: 'latest' });
        const mergeStatuses = await pages('GET /repos/{owner}/{repo}/commits/{ref}/statuses', { ref: pr.merge_commit_sha });
        if (mergeChecks.length || mergeStatuses.length) { checks = mergeChecks; statuses = mergeStatuses; }
      }
      verifyChecks({ head, number, protection: result.repository.pullRequest.baseRef?.branchProtectionRule, rules, checks, statuses, runs });
      return runs;
    };
    await gates();
    process.env.STAGE_REVIEW = '✅ Cleared';
    process.env.STAGE_CHECKS = '✅ All required checks passed (staging-presence excluded)';
    await status('⏳ Preparing protected staging PR');
    const workflow = (await request('GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}', { workflow_id: WORKFLOW })).data;
    if (workflow.state !== 'active') throw new Error(`${WORKFLOW} is disabled. Enable it before deploying.`);
    await git.prepare(number, head);
    await assertCurrent();
    published = await git.existing(number, head);
    if (published && published.commit !== git.staging) {
      const previousRuns = await pages('GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs', {
        workflow_id: WORKFLOW, branch: published.tag, event: 'workflow_dispatch', head_sha: published.commit,
      });
      const previous = newest(previousRuns.filter(r => r.head_sha === published.commit && r.head_branch === published.tag && r.event === 'workflow_dispatch'));
      if (previous && previous.status !== 'completed') throw new Error(`The earlier staging snapshot is still deploying: ${previous.html_url}. Wait for it to finish, then retry /deploy-staging.`);
      if (previous?.conclusion !== 'success') published = undefined;
    }
    if (!published) {
      const issue = pr.head.ref.match(/^(?:feature|bug|fix)\/(\d+)(?:\D|$)/i)?.[1] || pr.title?.match(/#(\d+)\b/)?.[1];
      published = await git.publish({ number, head, issue, assertCurrent: async () => { await gates(); await assertCurrent(); }, status });
    }
    const { tag, commit } = published;
    process.env.STAGE_PR = '✅ Merged with required GitHub protections';
    process.env.STAGING_TAG = tag;
    process.env.STAGE_DEPLOY = '⏳ Deployment running';
    await status('⏳ Staging merged; dispatching CI/CD');
    const deployments = () => pages('GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs', { workflow_id: WORKFLOW, branch: tag, event: 'workflow_dispatch', head_sha: commit });
    let deployment = newest((await deployments()).filter(r => r.head_sha === commit && r.head_branch === tag && r.event === 'workflow_dispatch'));
    if (deployment?.status === 'completed' && deployment.conclusion === 'success') {
      await assertCurrent();
      process.env.STAGE_DEPLOY = '✅ Already deployed'; await status('✅ Already deployed ' + tag + ' · ' + commit.slice(0, 10));
    } else {
      await assertCurrent();
      await git.assertStaging(commit);
      if (!deployment || deployment.status === 'completed') {
        const previous = deployment?.id || 0;
        await request('POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches', { workflow_id: WORKFLOW, ref: tag });
        deployment = undefined;
        const deadline = now() + 120000;
        do {
          deployment = newest((await deployments()).filter(r => r.id > previous && r.head_sha === commit && r.head_branch === tag && r.event === 'workflow_dispatch'));
          if (!deployment) await sleep(5000);
        } while (!deployment && now() < deadline);
        if (!deployment) throw new Error(`Dispatched ${tag}, but could not locate its deployment run. Retry /deploy-staging to resume.`);
        process.env.STAGING_RUN_URL = deployment.html_url;
        await status('⏳ CI/CD dispatched; waiting for staging deployment');
      }
      const deadline = now() + 70 * 60 * 1000;
      while (deployment.status !== 'completed') {
        if (now() >= deadline) throw new Error(`Deployment still running for ${tag}: ${deployment.html_url}. Retry /deploy-staging to resume watching.`);
        await sleep(15000);
        deployment = (await request('GET /repos/{owner}/{repo}/actions/runs/{run_id}', { run_id: deployment.id })).data;
      }
      if (deployment.conclusion !== 'success') throw new Error(`Deployment of ${tag} ended with ${deployment.conclusion}: ${deployment.html_url}. No success comment or staging check rerun was issued.`);
      await assertCurrent();
      process.env.STAGING_RUN_URL = deployment.html_url;
      process.env.STAGE_DEPLOY = '✅ Deployment succeeded';
      await status('✅ Successfully deployed ' + tag + ' · ' + commit.slice(0, 10) + ' — please verify on staging');
    }
    if (force) {
      // Label creation is only attempted after confirmed deployment success.
      // A 404 means this repo has not used the force-deployed label yet.
      try { await request('GET /repos/{owner}/{repo}/labels/{name}', { name: 'force-deployed' }); }
      catch (error) {
        if (error.status !== 404) throw error;
        try {
          await request('POST /repos/{owner}/{repo}/labels', {
            name: 'force-deployed', color: 'd93f0b', description: 'Staging deployed with Codex and human-decision gates overridden',
          });
        } catch (creationError) {
          if (creationError.status !== 422) throw creationError;
          // Another job may have created the label between GET and POST.
          await request('GET /repos/{owner}/{repo}/labels/{name}', { name: 'force-deployed' });
        }
      }
      await request('POST /repos/{owner}/{repo}/issues/{issue_number}/labels', { issue_number: number, labels: ['force-deployed'] });
      await comment(`⚠️ Force-deployed by @${actor}: ${reason}, skipped: Codex review, human-decision threads`);
    }
    const runs = await pages('GET /repos/{owner}/{repo}/actions/runs', { head_sha: head });
    const staging = newest(runs.filter(r => stagingWorkflow(r) && r.head_sha === head && (r.pull_requests || []).some(p => p.number === number)));
    if (!staging) throw new Error(`Deployment succeeded, but no ${STAGING_CHECK} workflow run exists for this PR head.`);
    const jobs = await pages('GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs', { run_id: staging.id, filter: 'latest' });
    const job = newest(jobs.filter(j => j.name === 'check-commits' || j.name === STAGING_CHECK));
    if (!job) throw new Error(`Deployment succeeded, but the ${STAGING_CHECK} job could not be found.`);
    await request('POST /repos/{owner}/{repo}/actions/jobs/{job_id}/rerun', { job_id: job.id });
  } catch (error) {
    const location = published ? `\n\nStaging tag: ${published.tag}, commit: ${published.commit}.` : '';
    await comment(`Staging deployment stopped: ${error.message}${location}`);
    core.setFailed(error.message);
  } finally {
    core.setOutput('reported', 'true');
  }
}

module.exports = { deploy, command, StagingGit, verifyCodex, verifyChecks, nextTag };
