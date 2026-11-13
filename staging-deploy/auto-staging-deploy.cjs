'use strict';

const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const execute = promisify(execFile);
const STAGING_CHECK = 'Check PR commits exist in staging';
const STAGING_WORKFLOW = '.github/workflows/check-commits-in-staging.yml';
// Temporary user-requested staging-only exemption while the checks runner
// lacks GitHub CLI. Remove this exemption when test validation is restored.
const TEMPORARY_TEST_VALIDATION_EXEMPTION = {
  context: 'validate-tests', workflow: '.github/workflows/pr-test-validation.yml',
};
const CODEX_AUTHORS = new Set(['chatgpt-codex-connector[bot]', 'codex[agent]']);
const WORKFLOW = 'cicd.yml';
const time = value => Date.parse(value || '') || 0;
// GitHub supplies the authenticated login. Codex agent identities need not have
// the same REST user.type as the connector bot.
const bot = value => CODEX_AUTHORS.has(value.user?.login);
const workflowPath = run => (run.path || '').split('@')[0];
const workflowLocation = run => {
  const raw = run.path || '';
  const at = raw.lastIndexOf('@');
  const file = at < 0 ? raw : raw.slice(0, at);
  const marker = '/.github/workflows/';
  const index = file.indexOf(marker);
  return {
    source: index < 0 ? null : file.slice(0, index),
    file: index < 0 ? file : file.slice(index + 1),
    ref: at < 0 ? null : raw.slice(at + 1),
  };
};
const normalizedRef = ref => String(ref || '').replace(/^refs\/heads\//, '').replace(/^refs\/tags\//, '');
const canonicalWorkflowRef = ref => {
  const value = String(ref || '');
  if (!value) return '';
  if (/^refs\/(heads|tags)\//.test(value) || /^[a-f0-9]{40}$/i.test(value)) return value;
  return `refs/heads/${value}`;
};
// Reviewed required-workflow IDs from GitHub's org-ruleset runs for PR #440.
// Bind each ID to the *configured* source identity. Rule changes fail closed
// until this protected-branch mapping is reviewed and updated.
const TRUSTED_REQUIRED_WORKFLOWS = Object.freeze({
  '400272849': Object.freeze({ path: '.github/workflows/java-test-coverage.yml', repository_id: 1041381819, ref: 'refs/heads/main' }),
  '251714341': Object.freeze({ path: '.github/workflows/pr-ticket-validation.yml', repository_id: 1041381819, ref: 'refs/heads/main' }),
});

function requiredWorkflowMatches({ run, expected, sources = {} }) {
  const location = workflowLocation(run);
  const rawPath = String(expected.path || '');
  const at = rawPath.lastIndexOf('@');
  const expectedPath = at < 0 ? rawPath : rawPath.slice(0, at);
  const requiredRef = expected.ref || (at < 0 ? null : rawPath.slice(at + 1));
  const expectedLocation = workflowLocation({ path: expectedPath });
  if (location.file !== expectedLocation.file) return false;

  // GitHub's organization required-workflow runs expose a repository-local
  // path, but their workflow_url is an authenticated required_workflows
  // resource. A local user-defined workflow uses actions/workflows instead.
  // Trust the server-issued identity, not merely a matching filename.
  const url = String(run.workflow_url || '');
  const requiredWorkflowRun = /\/actions\/required_workflows\/\d+$/.test(url);
  const source = expected.repository_id == null ? null :
    (sources[`${expected.repository_id}@${normalizedRef(requiredRef)}`] || sources[String(expected.repository_id)]);
  if (expected.repository_id != null && (!source || !source.name)) return false;
  const sourceName = source?.name;
  if (location.source) {
    if (sourceName && location.source.toLowerCase() !== sourceName.toLowerCase()) return false;
    if (!sourceName && expectedLocation.source &&
        location.source.toLowerCase() !== expectedLocation.source.toLowerCase()) return false;
    if (requiredRef && (!location.ref || canonicalWorkflowRef(location.ref) !== canonicalWorkflowRef(requiredRef))) return false;
  } else if (expected.repository_id != null) {
    // The run has no source in its path. Require the required_workflows URL
    // AND workflow_sha matching the exact revision resolved from the ruleset's
    // repository_id + ref. The numeric URL ID is not exposed in branch rules.
    if (!requiredWorkflowRun || !source.sha) return false;
    // A run workflow_id alone cannot identify the configured source repository
    // or ref. A previously successful run may remain after the rule changes.
    // Require explicit source-revision proof rather than guessing by filename.
    if (run.workflow_sha) {
      if (run.workflow_sha !== source.sha) return false;
    } else {
      const match = /^https:\/\/api\.github\.com\/repos\/[^/]+\/[^/]+\/actions\/required_workflows\/(\d+)$/.exec(url);
      const id = match?.[1];
      const trusted = id && TRUSTED_REQUIRED_WORKFLOWS[id];
      // The ID alone is not sufficient. Verify the mapping against the active
      // rule's source repository, path and ref (including pinned SHAs).
      if (!trusted || String(run.workflow_id) !== id ||
          trusted.path !== expectedLocation.file ||
          Number(trusted.repository_id) !== Number(expected.repository_id) ||
          canonicalWorkflowRef(trusted.ref) !== canonicalWorkflowRef(requiredRef) ||
          (expected.sha && trusted.sha !== expected.sha)) return false;
    }
  } else if (expectedLocation.source || requiredRef) return false;
  // A SHA-pinned rule must be backed by a verified revision, even when the
  // run path qualifies its source repository. A ref name is not SHA proof.
  if (expected.sha && run.workflow_sha !== expected.sha &&
      (!location.ref || location.ref !== expected.sha)) return false;
  return true;
}

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
    // Use the same repository-scoped App token for the staged PR merge API.
    env.GH_TOKEN = env.STAGING_PUSH_TOKEN;
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


// Security findings must not be hidden by a prior clean code-review receipt.
// Security-review summaries and receipts are separate from code review.
function verifySecurity({ head, comments = [], reviews = [] }) {
  const isSecurityReceipt = body =>
    /^\s*###\s*(?:🛡️\s*)?Codex Security Review\s*[·:]/im.test(body || '') ||
    (/<!-- codex-security-review:v1\b/.test(body || '') &&
     !/<!-- codex-review-state:v1\b/.test(body || ''));
  const securitySha = body => {
    const marker = (body || '').match(/<!-- codex-security-review:v1 (.*?) -->/s);
    if (marker) {
      try { const metadata = JSON.parse(marker[1]); if (/^[a-f0-9]{40}$/.test(metadata.headSha || '')) return metadata.headSha; }
      catch {}
    }
    return (body || '').match(/Reviewed\s+commit\**:\**\s*[`*_]*([a-f0-9]{7,40})/i)?.[1];
  };
  const securityReviews = [
    ...reviews.filter(bot).filter(r => isSecurityReceipt(r.body))
      .map(r => ({ body: r.body || '', sha: r.commit_id || securitySha(r.body), date: r.submitted_at })),
    ...comments.filter(bot).filter(c => isSecurityReceipt(c.body))
      .map(c => ({ body: c.body || '', sha: securitySha(c.body), date: c.created_at })),
  ].filter(r => r.sha && head.startsWith(r.sha)).sort((a,b) => time(b.date)-time(a.date));
  const summaries = comments.filter(bot).flatMap(c => {
    const m = (c.body || '').match(/<!-- codex-review-state:v1 (.*?) -->/s);
    if (!m) return [];
    try { return [{ state: JSON.parse(m[1]).security_review, date: c.updated_at || c.created_at }]; }
    catch { return []; }
  }).filter(x => x.state?.commit_sha === head).sort((a,b) => time(b.date)-time(a.date));
  const state = summaries[0]?.state;
  const latest = securityReviews[0];
  const hasFindingReceipt = latest && /Here are some automated security review suggestions/i.test(latest.body);
  const cleanReceipt = latest && /No security issues were found/i.test(latest.body);
  // Ignore stale summary state only when a later, SHA-bound clean receipt exists.
  const latestSummaryDate = time(summaries[0]?.date);
  const newerClearance = cleanReceipt && time(latest.date) > latestSummaryDate;
  if ((!newerClearance && (state?.has_any_findings === true || state?.has_unresolved_findings === true)) ||
      hasFindingReceipt || (latest && !cleanReceipt) ||
      (state && state.status !== 'completed' && !newerClearance)) {
    throw new Error('Codex security review has findings or is not cleared for this PR head.');
  }
}

function verifyCodex({ head, comments, reviews, reactions, requestId }) {
  verifySecurity({ head, comments, reviews });
  const requests = comments.filter(c => /(^|\n)\s*@codex\s+review\b/i.test(c.body || ''));
  const latestRequest = [...requests].sort((a, b) => time(b.created_at) - time(a.created_at) || Number(b.id) - Number(a.id))[0];
  const receipts = [
    ...reviews.filter(bot).filter(r =>
      !/^\s*###\s*(?:🛡️\s*)?Codex Security Review\s*[·:]/im.test(r.body || ''))
      .map(r => ({ ...r, date: r.submitted_at, sha: r.commit_id })),
    ...comments.filter(bot).filter(c => !c.body.includes('codex-review-state:v1') &&
      !/^\s*<!-- codex-security-review:v1\b/m.test(c.body || '') &&
      !/^\s*###\s*(?:🛡️\s*)?Codex Security Review\s*[·:]/im.test(c.body || '')).map(c => ({ ...c, date: c.created_at })),
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
  // GitHub can auto-trigger Codex on PR creation, without a separate
  // @codex review comment, thumbs-up reaction, or legacy text receipt.
  // Accept only the authenticated Codex summary for this exact SHA.
  // A later explicit review request must receive its own clearance.
  const autoClearance = !latestRequest && summary?.state?.commit_sha === head &&
    summary.state.status === 'completed' &&
    summary.state.has_any_findings === false &&
    summary.state.has_unresolved_findings === false;
  if (autoClearance) return;
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

async function resolveWorkflowSources({ rules, request }) {
  const sources = {};
  for (const entry of rules.filter(r => r.type === 'workflows').flatMap(r => r.parameters?.workflows || [])) {
    if (entry.repository_id == null) continue;
    const key = String(entry.repository_id);
    const repository = (await request('GET /repositories/{repository_id}', {
      repository_id: entry.repository_id,
    })).data;
    if (!repository?.full_name || Number(repository.id) !== Number(entry.repository_id)) {
      throw new Error(`Cannot verify required workflow repository ${key}`);
    }
    const rawPath = String(entry.path || '');
    const at = rawPath.lastIndexOf('@');
    const ref = entry.ref || (at < 0 ? null : rawPath.slice(at + 1));
    if (!ref && !entry.sha) throw new Error(`Required workflow from ${repository.full_name} has no pinned ref or SHA`);
    const revision = entry.sha || (await request('GET /repos/{owner}/{repo}/commits/{ref}', {
      owner: repository.full_name.split('/')[0],
      repo: repository.full_name.split('/')[1],
      ref: normalizedRef(ref),
    })).data?.sha;
    if (!/^[a-f0-9]{40}$/.test(revision || '')) {
      throw new Error(`Cannot verify required workflow revision for ${repository.full_name}@${ref}`);
    }
    if (sources[key] && sources[key].sha !== revision) {
      // Different refs in the same source repository are valid independent
      // requirements; index by rule's repo and ref instead of repository alone.
      sources[`${key}@${normalizedRef(ref)}`] = { name: repository.full_name, sha: revision };
    } else {
      sources[key] = { name: repository.full_name, sha: revision };
      sources[`${key}@${normalizedRef(ref)}`] = { name: repository.full_name, sha: revision };
    }
  }
  return sources;
}

function verifyChecks({ head, number, protection, rules, checks, statuses, runs, strictTestValidation = false, workflowSources = {}, workflowRunSha = head }) {
  const applicable = runs.filter(r => r.head_sha === workflowRunSha && r.event === 'pull_request' &&
    (r.pull_requests || []).some(pr => pr.number === number));
  const required = [
    ...(protection?.requiresStatusChecks ? protection.requiredStatusChecks.map(c => ({ context: c.context, integration_id: c.app?.databaseId })) : []),
    ...rules.filter(r => r.type === 'required_status_checks').flatMap(r => r.parameters.required_status_checks),
  ];
  const requiredWorkflows = rules.filter(r => r.type === 'workflows').flatMap(r => r.parameters.workflows);
  const problems = [];
  const stagingSuites = new Set(applicable.filter(stagingWorkflow).map(r => r.check_suite_id));
  for (const expected of required) {
    if (expected.context === STAGING_CHECK || (!strictTestValidation && expected.context === TEMPORARY_TEST_VALIDATION_EXEMPTION.context)) continue;
    const matching = checks.filter(c => c.name === expected.context &&
      (expected.integration_id == null || expected.integration_id < 0 || c.app?.id === expected.integration_id));
    // The org staging-presence workflow calls its job "check-commits".
    if (expected.context === 'check-commits' && matching.length && matching.every(c => stagingSuites.has(c.check_suite?.id))) continue;
    if (expected.context === 'check-commits' && !matching.length && requiredWorkflows.some(w => w.path === STAGING_WORKFLOW)) continue;
    const currentChecks = latestChecks(matching);
    const status = newest(statuses.filter(s => s.context === expected.context));
    if (!currentChecks.length && (!status || (strictTestValidation && expected.integration_id > 0))) problems.push(`${expected.context}: missing`);
    for (const check of currentChecks) {
      if (check.status !== 'completed' || check.conclusion !== 'success') problems.push(`${expected.context}: ${check.conclusion || check.status}`);
    }
    // When both a status and a check run use the same context, both must pass.
    if (status && status.state !== 'success') problems.push(`${expected.context}: ${status.state}`);
    if (expected.integration_id > 0 && !currentChecks.length) problems.push(`${expected.context}: missing result from required app ${expected.integration_id}`);
  }
  for (const expected of requiredWorkflows) {
    if (expected.path === STAGING_WORKFLOW || (!strictTestValidation && expected.path === TEMPORARY_TEST_VALIDATION_EXEMPTION.workflow)) continue;
    const run = newest(applicable.filter(r => requiredWorkflowMatches({ run: r, expected, sources: workflowSources })));
    if (strictTestValidation && expected.repository_id != null && !workflowSources[String(expected.repository_id)]) {
      problems.push(`${expected.path}: cannot verify required workflow source repository ${expected.repository_id}`);
      continue;
    }
    if (!run) { problems.push(`${expected.path}: missing required workflow`); continue; }
    if (run.status !== 'completed' || run.conclusion !== 'success') problems.push(`${expected.path}: ${run.conclusion || run.status}`);
    const jobs = latestChecks(checks.filter(c => c.check_suite?.id === run.check_suite_id));
    if (!jobs.length) {
      problems.push(`${expected.path}: missing job checks`);
      continue;
    }
    // A required workflow can succeed while conditionally skipping a job.
    // GitHub decides whether the workflow itself succeeded; individual
    // skipped jobs are not failures unless they are explicitly required status
    // checks (validated separately above). Failed or incomplete jobs still
    // block promotion, even if the workflow conclusion claims success.
    const successful = jobs.filter(c => c.status === 'completed' && c.conclusion === 'success');
    for (const check of jobs) {
      if (check.status !== 'completed' || !['success', 'skipped'].includes(check.conclusion)) {
        problems.push(`${check.name}: ${check.conclusion || check.status}`);
      }
    }
    if (!successful.length) problems.push(`${expected.path}: no successful job checks`);

    // The centrally managed Java coverage workflow has two mutually
    // exclusive paths: "coverage" and "Coverage not applicable". The detect
    // job succeeding alone is insufficient to prove coverage was evaluated.
    if (workflowLocation({ path: String(expected.path || '').split('@')[0] }).file === '.github/workflows/java-test-coverage.yml') {
      if (!successful.some(c => c.name === 'Detect supported Java repository')) {
        problems.push(`${expected.path}: Java applicability detection did not succeed`);
      }
      if (!successful.some(c => ['coverage', 'Coverage not applicable'].includes(c.name))) {
        problems.push(`${expected.path}: neither coverage nor not-applicable succeeded`);
      }
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
  const comment = body => request('POST /repos/{owner}/{repo}/issues/{issue_number}/comments', { issue_number: number, body });
  const getPR = async () => (await request('GET /repos/{owner}/{repo}/pulls/{pull_number}', { pull_number: number })).data;
  let published;
  try {
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
      verifyCodex({ head, comments, reviews, reactions, requestId: latestRequest?.id });
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
      const workflowSources = await resolveWorkflowSources({ rules, request });
      verifyChecks({ head, number, protection: result.repository.pullRequest.baseRef?.branchProtectionRule, rules, checks, statuses, runs, workflowSources });
      return runs;
    };
    await gates();
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
      published = await git.publish({ number, head, issue, assertCurrent: async () => { await gates(); await assertCurrent(); } });
    }
    const { tag, commit } = published;
    const deployments = () => pages('GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs', { workflow_id: WORKFLOW, branch: tag, event: 'workflow_dispatch', head_sha: commit });
    let deployment = newest((await deployments()).filter(r => r.head_sha === commit && r.head_branch === tag && r.event === 'workflow_dispatch'));
    if (deployment?.status === 'completed' && deployment.conclusion === 'success') {
      await assertCurrent();
      await comment(`Already deployed to staging: ${tag}, ${commit}. Rechecking staging presence.`);
    } else {
      await assertCurrent();
      await git.assertStaging(commit);
      if (!deployment || deployment.status === 'completed') {
        const previous = deployment?.id || 0;
        await run('gh', ['workflow', 'run', WORKFLOW, '--ref', tag, '--repo', `${repo.owner}/${repo.repo}`]);
        deployment = undefined;
        const deadline = now() + 120000;
        do {
          deployment = newest((await deployments()).filter(r => r.id > previous && r.head_sha === commit && r.head_branch === tag && r.event === 'workflow_dispatch'));
          if (!deployment) await sleep(5000);
        } while (!deployment && now() < deadline);
        if (!deployment) throw new Error(`Dispatched ${tag}, but could not locate its deployment run. Retry /deploy-staging to resume.`);
      }
      const deadline = now() + 70 * 60 * 1000;
      while (deployment.status !== 'completed') {
        if (now() >= deadline) throw new Error(`Deployment still running for ${tag}: ${deployment.html_url}. Retry /deploy-staging to resume watching.`);
        await sleep(15000);
        deployment = (await request('GET /repos/{owner}/{repo}/actions/runs/{run_id}', { run_id: deployment.id })).data;
      }
      if (deployment.conclusion !== 'success') throw new Error(`Deployment of ${tag} ended with ${deployment.conclusion}: ${deployment.html_url}. No success comment or staging check rerun was issued.`);
      await assertCurrent();
      await comment(`Deployed to staging: ${tag}, ${commit}, please verify`);
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

module.exports = { deploy, command, StagingGit, verifyCodex, verifySecurity, verifyChecks, nextTag, requiredWorkflowMatches, resolveWorkflowSources };
