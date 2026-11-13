'use strict';

const { verifyChecks, verifyCodex, verifySecurity, resolveWorkflowSources } = require('./auto-staging-deploy.cjs');

const WORKFLOW = 'cicd.yml';
const newest = xs => [...xs].sort((a, b) => Number(b.id) - Number(a.id))[0];
const workflowPath = run => (run.path || '').split('@')[0];

const STAGING_VERSION = /^refs\/tags\/dev-v(\d+)\.(\d+)\.(\d+)$/;

function nextStagingTag(tags, mergedSHA) {
  const versions = tags.map(ref => {
    const name = ref.ref || '';
    const match = name.match(STAGING_VERSION);
    return match ? { name: name.slice('refs/tags/'.length), major: BigInt(match[1]),
      minor: BigInt(match[2]), patch: BigInt(match[3]), sha: ref.commitSha || ref.object?.sha } : null;
  }).filter(Boolean).sort((a, b) =>
    a.major !== b.major ? (a.major > b.major ? -1 : 1) :
    a.minor !== b.minor ? (a.minor > b.minor ? -1 : 1) :
    a.patch !== b.patch ? (a.patch > b.patch ? -1 : 1) : 0);
  const existing = versions.find(v => v.sha === mergedSHA);
  if (existing) return { tag: existing.name, existing: true };
  if (!versions.length) throw new Error('No dev-vX.Y.Z staging tags found; version cannot be safely determined.');
  const latest = versions[0];
  return { tag: `dev-v${latest.major}.${latest.minor}.${latest.patch + 1n}`, existing: false };
}

// GitHub's matching-refs response returns the annotated tag object's SHA.
// Resolve all annotated version tags in one git call (tags are already fetched)
// before making any retry or version allocation decisions.
async function normalizeTagTargets(refs, git) {
  if (!refs.some(ref => ref.object?.type === 'tag' && STAGING_VERSION.test(ref.ref || ''))) return refs;
  const listing = await git.git('for-each-ref', '--format=%(refname)%09%(*objectname)', 'refs/tags/dev-v*');
  const peeled = new Map(String(listing).trim().split(/\r?\n/).map(line => line.split('\t')));
  return refs.map(ref => {
    if (ref.object?.type !== 'tag' || !STAGING_VERSION.test(ref.ref || '')) return ref;
    const commitSha = peeled.get(ref.ref);
    if (!/^[a-f0-9]{40,64}$/.test(commitSha || '')) {
      throw new Error(`Cannot verify annotated tag target: ${ref.ref}`);
    }
    return { ...ref, commitSha };
  });
}

function selectPromotionTag(refs, mergedSHA, number) {
  const legacyName = `dev-auto-pr${number}-${mergedSHA.slice(0, 12)}`;
  const legacy = refs.find(ref => ref.ref === `refs/tags/${legacyName}`);
  if (legacy) {
    if ((legacy.commitSha || legacy.object?.sha) !== mergedSHA) {
      throw new Error(`Legacy promotion tag ${legacyName} targets an unexpected commit.`);
    }
    return { tag: legacyName, existing: true, legacy: true };
  }
  return { ...nextStagingTag(refs, mergedSHA), legacy: false };
}

function promotionSnapshot({ stagingSHA, mergedSHA, previouslyMerged }) {
  if (stagingSHA !== mergedSHA && !previouslyMerged) {
    throw new Error('Staging changed after the merge. No stale tag created.');
  }
  return mergedSHA;
}

function hasWorkflowDispatch(yaml) {
  // Restrictive parser for GitHub Actions' top-level "on" trigger only.
  // Unknown YAML shapes fail closed rather than matching a job name or comment.
  const lines = String(yaml).split(/\r?\n/);
  const roots = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*(?:#.*)?$/.test(line)) continue;
    const root = /^(?:on|"on"|'on')\s*:\s*(.*?)\s*$/.exec(line);
    if (root) roots.push({ index: i, value: root[1].replace(/\s+#.*$/, '') });
  }
  if (roots.length !== 1) return false;
  const { index, value } = roots[0];
  if (value) {
    if (value === 'workflow_dispatch' || value === '"workflow_dispatch"' || value === "'workflow_dispatch'") return true;
    const flow = /^\[([^\]]*)\]$/.exec(value);
    return Boolean(flow && flow[1].split(',').some(x =>
      /^(?:workflow_dispatch|"workflow_dispatch"|'workflow_dispatch')$/.test(x.trim())));
  }
  let childIndent = null;
  let found = false;
  for (let i = index + 1; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*(?:#.*)?$/.test(line)) continue;
    const indent = /^ */.exec(line)[0].length;
    if (indent === 0) break;
    if (!indent || /^\t/.test(line)) return false;
    if (childIndent == null) childIndent = indent;
    if (indent !== childIndent) continue;
    const entry = /^\s*(?:workflow_dispatch|"workflow_dispatch"|'workflow_dispatch')\s*:\s*(.*?)\s*$/.exec(line);
    if (entry && !/^(?:false|no|off|null|~)$/i.test(entry[1].replace(/\s+#.*$/, ''))) found = true;
  }
  return found;
}

function activeChangeRequests(reviews) {
  const latest = new Map();
  for (const review of reviews) {
    if (!review.user?.login || !['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'].includes(review.state)) continue;
    const key = review.user.login.toLowerCase();
    const previous = latest.get(key);
    const timestamp = Date.parse(review.submitted_at || '') || 0;
    const previousTime = Date.parse(previous?.submitted_at || '') || 0;
    if (!previous || timestamp > previousTime || (timestamp === previousTime && Number(review.id) > Number(previous.id))) latest.set(key, review);
  }
  return [...latest.values()].filter(review => review.state === 'CHANGES_REQUESTED').map(review => review.user.login);
}

async function unresolvedThreads(github, repo, number) {
  const unresolved = [];
  let cursor;
  do {
    const result = await github.graphql(`query($owner:String!,$repo:String!,$number:Int!,$cursor:String) {
      repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
        reviewThreads(first:100,after:$cursor) {
          nodes { isResolved comments(first:1) { nodes { url } } }
          pageInfo { hasNextPage endCursor }
        }
      } }
    }`, { ...repo, number, cursor });
    const connection = result.repository.pullRequest.reviewThreads;
    for (const thread of connection.nodes) if (!thread.isResolved) unresolved.push(thread.comments.nodes[0]?.url || 'Unresolved review thread');
    cursor = connection.pageInfo.hasNextPage ? connection.pageInfo.endCursor : undefined;
  } while (cursor);
  return unresolved;
}

async function checkReady({ github, repo, number, head, getPR, pages }) {
  const pr = await getPR();
  if (pr.state !== 'open' || pr.draft || pr.base.ref !== 'main' || pr.head.sha !== head) {
    throw new Error('PR must remain open, non-draft, targeting main, and at the reviewed commit.');
  }
  const unresolved = await unresolvedThreads(github, repo, number);
  if (unresolved.length) throw new Error(`Resolve all review conversations before running /deploy-staging:\n${unresolved.map(x => '- ' + x).join('\n')}`);
  const reviews = await pages('GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews', { pull_number: number });
  const requests = activeChangeRequests(reviews);
  // Security findings are an independent gate, even with an approved human review.
  const securityComments = await pages('GET /repos/{owner}/{repo}/issues/{issue_number}/comments', { issue_number: number });
  verifySecurity({ head, comments: securityComments, reviews });
  if (requests.length) throw new Error(`Outstanding changes requested by: ${requests.join(', ')}. Address them before /deploy-staging.`);
  // An authorized actor requesting deployment is not itself a code review.
  // Require a SHA-bound Codex clearance or another maintainer's current-head
  // approval. A new push makes prior approvals stale for this promotion.
  const approved = reviews.filter(r =>
    r.state === 'APPROVED' && r.commit_id === head &&
    r.user?.login?.toLowerCase() !== pr.user?.login?.toLowerCase() &&
    !requests.some(login => login.toLowerCase() === r.user.login.toLowerCase()));
  let acceptedHumanReview = false;
  for (const review of approved) {
    const response = await github.request('GET /repos/{owner}/{repo}/collaborators/{username}/permission',
      { ...repo, username: review.user.login });
    if (['write', 'maintain', 'admin'].includes(response.data.permission)) {
      acceptedHumanReview = true;
      break;
    }
  }
  if (!acceptedHumanReview) {
    const comments = await pages('GET /repos/{owner}/{repo}/issues/{issue_number}/comments', { issue_number: number });
    const latestRequest = comments.filter(c => /(^|\n)\s*@codex\s+review\b/i.test(c.body || ''))
      .sort((a, b) => Date.parse(b.created_at || '') - Date.parse(a.created_at || ''))[0];
    const reactions = latestRequest ?
      await pages('GET /repos/{owner}/{repo}/issues/comments/{comment_id}/reactions', { comment_id: latestRequest.id }) : [];
    try { verifyCodex({ head, comments, reviews, reactions, requestId: latestRequest?.id }); }
    catch { throw new Error('Codex has not cleared this PR head. Obtain a clean Codex review or an authorized human approval on the current commit before /deploy-staging.'); }
  }


  const result = await github.graphql(`query($owner:String!,$repo:String!,$number:Int!) {
    repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
      headRefOid baseRef { branchProtectionRule { requiresStatusChecks requiredStatusChecks { context app { databaseId } } } }
    } }
  }`, { ...repo, number });
  if (result.repository.pullRequest.headRefOid !== head) throw new Error('PR head changed during validation.');
  const rules = await pages('GET /repos/{owner}/{repo}/rules/branches/{branch}', { branch: 'main' });
  let runs = await pages('GET /repos/{owner}/{repo}/actions/runs', { head_sha: head });
  let workflowRunSha = head;
  let checks = await pages('GET /repos/{owner}/{repo}/commits/{ref}/check-runs', { ref: head, filter: 'latest' });
  let statuses = await pages('GET /repos/{owner}/{repo}/commits/{ref}/statuses', { ref: head });
  if (pr.merge_commit_sha) {
    const mergeChecks = await pages('GET /repos/{owner}/{repo}/commits/{ref}/check-runs', { ref: pr.merge_commit_sha, filter: 'latest' });
    const mergeStatuses = await pages('GET /repos/{owner}/{repo}/commits/{ref}/statuses', { ref: pr.merge_commit_sha });
    if (mergeChecks.length || mergeStatuses.length) {
      checks = mergeChecks;
      statuses = mergeStatuses;
      // Required workflow runs must come from the same SHA as check suites.
      // Otherwise successful merge-queue job suites appear to be missing.
      runs = await pages('GET /repos/{owner}/{repo}/actions/runs', { head_sha: pr.merge_commit_sha });
      workflowRunSha = pr.merge_commit_sha;
    }
  }
  const workflowSources = await resolveWorkflowSources({ rules, request: (route, args) => github.request(route, args) });
  verifyChecks({ head, number, protection: result.repository.pullRequest.baseRef?.branchProtectionRule,
    rules, checks, statuses, runs, strictTestValidation: true, workflowSources, workflowRunSha });


}

function conflictMessage(number, issue, files) {
  const branch = `conflict/resolved/${issue || number}`;
  return `Merge conflicts with staging:\n${files.map(f => '- ' + f).join('\n') || '- See GitHub merge conflict details'}\n\nResolve on a **separate branch from staging** (never merge staging into the original main feature branch):\n\nsh\ngit fetch origin staging\ngit switch -c ${branch} origin/staging\ngit fetch origin pull/${number}/head\ngit merge --no-ff FETCH_HEAD\n# Resolve conflicts, run tests, commit, push, and open PR to staging.\n\nAfter merging that conflict-resolution PR, run /deploy-staging again on the original main PR.`;
}

async function deploy({ github, appGithub, context, core, command, git, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), now = Date.now }) {
  const event = context.payload;
  if (context.eventName !== 'issue_comment' || event.action !== 'created' || !event.issue?.pull_request ||
      event.comment?.body?.trim() !== '/deploy-staging') return;
  const repo = context.repo, number = event.issue.number, actor = event.comment.user.login;
  const request = (route, args = {}) => github.request(route, { ...repo, ...args });
  // Separate GitHub App client. Never fall back to GITHUB_TOKEN for staging PR writes.
  const appRequest = (route, args = {}) => {
    if (!appGithub?.request) throw new Error('Staging GitHub App client is unavailable; promotion refused.');
    return appGithub.request(route, { ...repo, ...args });
  };
  const pages = (route, args = {}) => github.paginate(route, { ...repo, per_page: 100, ...args });
  const actionUrl = `https://github.com/${repo.owner}/${repo.repo}/actions/runs/${context.runId || 'unknown'}`;
  const statusMarker = `<!-- staging-promotion-status:v1 request=${event.comment.id} -->`;
  let statusCommentId;
  let phase = 'Validating checks and reviews';
  let deploymentUrl;
  const message = (state, detail = '') => [
    statusMarker,
    `### ${state === 'success' ? '✅' : state === 'failure' ? '❌' : '⏳'} Staging deployment — ${state === 'success' ? 'Successful' : state === 'failure' ? 'Failed' : 'In progress'}`,
    '',
    `**Step:** ${phase}`,
    detail,
    stagingUrl ? `**Staging PR:** [View PR](${stagingUrl})` : '',
    tag ? `**Tag:** [${tag}](https://github.com/${repo.owner}/${repo.repo}/tree/refs/tags/${tag})` : '',
    deploymentUrl ? `**Deployment:** [View CI/CD run](${deploymentUrl})` : '',
    `**Automation:** [View run](${actionUrl})`,
  ].filter(Boolean).join('\n');
  const report = async (state, detail = '') => {
    const body = message(state, detail);
    if (statusCommentId) {
      await request('PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}', {
        comment_id: statusCommentId, body,
      });
    } else {
      const response = await request('POST /repos/{owner}/{repo}/issues/{issue_number}/comments', {
        issue_number: number, body,
      });
      statusCommentId = response.data?.id;
    }
  };
  const progress = async step => { phase = step; await report('running'); };
  const getPR = async () => (await request('GET /repos/{owner}/{repo}/pulls/{pull_number}', { pull_number: number })).data;
  let stagingUrl, tag;
  try {
    await progress('Validating checks and reviews');
    const access = (await request('GET /repos/{owner}/{repo}/collaborators/{username}/permission', { username: actor })).data;
    if (!['write', 'maintain', 'admin'].includes(access.permission)) throw new Error(`@${actor} needs write access to request staging deployment.`);
    const pr = await getPR();
    if (pr.state !== 'open' || pr.base.ref !== 'main' || pr.head.repo.full_name !== `${repo.owner}/${repo.repo}`) {
      throw new Error('Only open main PRs from branches in this repository are supported.');
    }
    const head = pr.head.sha, issue = pr.head.ref.match(/^(?:feature|bug|fix)\/(\d+)/i)?.[1];
    const ready = () => checkReady({ github, repo, number, head, getPR, pages });
    await ready();
    const workflow = (await request('GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}', { workflow_id: WORKFLOW })).data;
    if (workflow.state !== 'active') throw new Error('Staging CI/CD workflow is not enabled.');

    await progress('Preparing staging promotion');
    await git.prepare(number, head);
    // Reuse any existing staging PR for this exact source head. Never create
    // another staging PR/tag for a repeated command.
    const headName = `${repo.owner}:${pr.head.ref}`;
    const stagingPRs = await pages('GET /repos/{owner}/{repo}/pulls', { state: 'all', base: 'staging', head: headName });
    let stage = stagingPRs.find(p => p.head.sha === head && p.base.ref === 'staging' && p.merged_at) ||
      stagingPRs.find(p => p.head.sha === head && p.base.ref === 'staging' && p.state === 'open');

    const alreadyContained = await git.git('merge-base', '--is-ancestor', head, git.staging).then(() => true, () => false);
    if (!stage && alreadyContained) {
      // A manually merged conflict-resolution PR has a different source head.
      // The original head is now included in staging. Tag that exact snapshot,
      // but only when GitHub identifies a merged staging PR at this commit.
      const candidates = await pages('GET /repos/{owner}/{repo}/pulls', { state: 'closed', base: 'staging' });
      stage = candidates.find(p => p.merged_at && p.merge_commit_sha === git.staging &&
        (p.head.sha === head ||
          (p.head.ref === `conflict/resolved/${issue || number}` &&
           // The conflict-resolution PR must explicitly reference this
           // original main PR and its reviewed SHA, not another feature.
           (p.body || '').includes(pr.html_url) &&
           (p.body || '').includes(head))));
      if (!stage) throw new Error('PR head is in staging, but no merged staging PR matches the current snapshot. Cannot safely identify a promotion to tag.');
    }
    if (!stage) {
      await ready();
      const remote = (await git.git('ls-remote', 'origin', 'refs/heads/staging')).trim().split(/\s+/)[0];
      if (remote !== git.staging) throw new Error('Staging changed during preparation. Retry /deploy-staging.');
      // Use the same organization App identity as org-actions. This uses the
      // Octokit REST client, so the self-hosted runner does not require gh.
      stage = (await appRequest('POST /repos/{owner}/{repo}/pulls', {
        head: pr.head.ref, base: 'staging',
        title: `feature to staging: Promote main PR #${number} (#7319)`,
        body: `Staging promotion for #${number} (${pr.html_url}).\n\nSource commit: ${head}\n\nAutomation-created after main PR checks and review-thread validation. Main PR remains open.`,
      })).data;
      if (!stage.number || !stage.html_url || stage.head?.sha !== head) {
        throw new Error('Staging PR creation returned an unexpected head; refusing to promote.');
      }
    }
    stagingUrl = stage.html_url;
    await progress('Validating and merging staging PR');

    // Historical merged staging PRs are already reviewed snapshots. Do not
    // recompute a merge against today's staging tip, which may have advanced.
    if (!stage.merged_at) {
      let mergedTree;
      try {
        mergedTree = (await git.git('merge-tree', '--write-tree', '--name-only', '-z', git.staging, head)).split('\0')[0].trim();
      } catch (error) {
        const fields = String(error.stdout || '').split('\0');
        const files = fields.slice(1, fields.indexOf('', 1)).filter(Boolean);
        throw new Error(conflictMessage(number, issue, files));
      }
      if (!/^[a-f0-9]{40,64}$/.test(mergedTree)) throw new Error('Staging merge did not produce a valid tree.');
      let deploymentWorkflow;
      try { deploymentWorkflow = await git.git('show', `${mergedTree}:.github/workflows/${WORKFLOW}`); }
      catch { throw new Error('Merged staging snapshot is missing cicd.yml. Promotion cancelled.'); }
      if (!hasWorkflowDispatch(deploymentWorkflow)) {
        throw new Error('Merged staging snapshot removes cicd.yml workflow_dispatch. Promotion cancelled.');
      }
    }

    if (!stage.merged_at) {
      await ready();
      const remote = (await git.git('ls-remote', 'origin', 'refs/heads/staging')).trim().split(/\s+/)[0];
      if (remote !== git.staging) throw new Error(`Staging changed while PR was being created. Review ${stagingUrl} and retry.`);
      if (stage.head.sha !== head) throw new Error('Source branch changed. Refusing to merge an unreviewed SHA.');
      // GitHub App permissions and all repository branch rules are still enforced.
      // Merge only the previously reviewed head; never force or bypass protection.
      const merged = (await appRequest('PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge', {
        pull_number: stage.number, merge_method: 'merge', sha: head,
      })).data;
      if (!merged.merged || !merged.sha) throw new Error('Staging PR merge was not confirmed.');
      core.info(`Merged staging PR: ${merged.sha}`);
    }

    const stageFinal = (await request('GET /repos/{owner}/{repo}/pulls/{pull_number}', { pull_number: stage.number })).data;
    if (!stageFinal.merged || (stageFinal.head.sha !== head && !alreadyContained)) throw new Error(`Staging PR was not merged for expected head. Check ${stagingUrl}.`);
    await ready(); // The original main PR may have changed during the staging PR merge.
    const mergedSHA = stageFinal.merge_commit_sha;
    if (!mergedSHA) throw new Error('Staging merge commit SHA was not returned.');

    await git.git('fetch', 'origin', '+refs/heads/staging:refs/remotes/origin/staging', '+refs/tags/*:refs/tags/*');
    const stagingSHA = (await git.git('rev-parse', 'refs/remotes/origin/staging')).trim();
    // An earlier staging PR can have been merged before later promotions.
    // Report its exact tag/run, never tag the newer staging head for this PR.
    const promotionSHA = promotionSnapshot({ stagingSHA, mergedSHA, previouslyMerged: Boolean(stage.merged_at) });
    await git.git('merge-base', '--is-ancestor', head, promotionSHA);
    await git.git('merge-base', '--is-ancestor', mergedSHA, stagingSHA);

    await ready(); // Do not tag a source head whose review state changed after merge.
    await progress('Creating deployment tag');
    // Read the entire tag namespace and choose the next version after the
    // highest dev-vX.Y.Z tag. Ignore unrelated dev-* and prod-* tags.
    const rawTags = await pages('GET /repos/{owner}/{repo}/git/matching-refs/{ref}', { ref: 'tags/' });
    const allTags = await normalizeTagTargets(rawTags, git);
    // Resolve legacy annotated tags too, but only for the exact PR/merge SHA
    // being retried. Other legacy tags must not affect the next version.
    const legacyName = `dev-auto-pr${number}-${mergedSHA.slice(0, 12)}`;
    const legacyRef = allTags.find(t => t.ref === `refs/tags/${legacyName}`);
    if (legacyRef?.object?.type === 'tag') {
      const commitSha = (await git.git('rev-parse', `refs/tags/${legacyName}^{}`)).trim();
      if (!/^[a-f0-9]{40,64}$/.test(commitSha)) throw new Error('Cannot verify legacy tag target.');
      legacyRef.commitSha = commitSha;
    }
    const selected = selectPromotionTag(allTags, mergedSHA, number);
    const proposedTag = selected.tag;
    const existingTag = allTags.find(t => t.ref === `refs/tags/${proposedTag}`);
    if (existingTag && (existingTag.commitSha || existingTag.object?.sha) !== mergedSHA &&
        !selected.legacy) {
      throw new Error(`Tag ${proposedTag} was allocated concurrently to a different commit; retry the promotion.`);
    }
    if (!existingTag) {
      if (stagingSHA !== mergedSHA) {
        throw new Error(`Staging advanced after ${stagingUrl}; the old promotion has no version tag. Refusing to tag an older snapshot.`);
      }
      try {
        await request('POST /repos/{owner}/{repo}/git/refs', { ref: `refs/tags/${proposedTag}`, sha: mergedSHA });
      } catch (error) {
        // GitHub returns 422 when another workflow allocated this version.
        // Fail closed instead of moving or overwriting anyone else's tag.
        if (error.status === 422) throw new Error(`Tag ${proposedTag} was created concurrently. Retry /deploy-staging to select the next version.`);
        throw error;
      }
    }
    tag = proposedTag; // Show a tag link only after it exists or was verified.
    const tagUrl = `https://github.com/${repo.owner}/${repo.repo}/tree/refs/tags/${tag}`;
    const listRuns = async () => pages('GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs', {
      workflow_id: WORKFLOW, event: 'workflow_dispatch', branch: tag, head_sha: mergedSHA,
    });
    let deployment = newest((await listRuns()).filter(r => r.head_sha === mergedSHA && r.head_branch === tag));
    if ((!deployment || (deployment.status === 'completed' && deployment.conclusion !== 'success')) && stagingSHA !== mergedSHA) {
      throw new Error(`Previous staging deployment is not successful and staging has since advanced. Refusing to redeploy old tag ${tag}.`);
    }
    if (!deployment || (deployment.status === 'completed' && deployment.conclusion !== 'success')) {
      const previousRunId = deployment?.id || 0;
      await ready();
      await request('POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches', {
        workflow_id: WORKFLOW, ref: tag,
      });
      const deadline = now() + 120000;
      do {
        await sleep(5000);
        deployment = newest((await listRuns()).filter(r => r.id > previousRunId && r.head_sha === mergedSHA && r.head_branch === tag));
      } while (!deployment && now() < deadline);
      if (!deployment) throw new Error('Tag was created, but the CI/CD run did not appear. Retry /deploy-staging.');
    }
    deploymentUrl = deployment.html_url;
    await progress('Deploying to staging');
    // Only a successful deployment may unblock the original main PR.
    const deploymentDeadline = now() + 70 * 60 * 1000;
    while (deployment.status !== 'completed') {
      if (now() >= deploymentDeadline) throw new Error(`Staging deployment still running: ${deployment.html_url}. Retry /deploy-staging to resume tracking.`);
      await sleep(15000);
      deployment = (await request('GET /repos/{owner}/{repo}/actions/runs/{run_id}', { run_id: deployment.id })).data;
    }
    if (deployment.conclusion !== 'success') {
      throw new Error(`Staging deployment ended with ${deployment.conclusion}: ${deployment.html_url}. Main gate was not reopened.`);
    }
    await ready();
    await progress('Refreshing required staging-presence check');
    // Promotion makes the original main PR's circular staging-presence gate
    // eligible for success. GitHub does not rerun it automatically.
    const validationRuns = await pages('GET /repos/{owner}/{repo}/actions/runs', { head_sha: head });
    const checkRun = newest(validationRuns.filter(r =>
      r.head_sha === head && r.event === 'pull_request' &&
      r.name === 'Check PR commits exist in staging' &&
      workflowPath(r) === '.github/workflows/check-commits-in-staging.yml' &&
      (r.pull_requests || []).some(p => p.number === number)));
    if (checkRun) {
      const jobs = await pages('GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs', { run_id: checkRun.id, filter: 'latest' });
      const job = newest(jobs.filter(j => j.name === 'check-commits' || j.name === 'Check PR commits exist in staging'));
      if (job) await request('POST /repos/{owner}/{repo}/actions/jobs/{job_id}/rerun', { job_id: job.id });
      else throw new Error('Staging promoted, but the staging-presence job could not be found for rerun.');
    } else {
      throw new Error('Staging promoted, but the original main PR staging-presence check run was not found for rerun.');
    }
    phase = 'Deployment completed; staging-presence recheck requested';
    await report('success');
  } catch (error) {
    try {
      // Preserve Markdown lists, code snippets and review-thread links.
      // Removing them hides the actual steps a developer needs to resolve.
      await report('failure', `**Reason:** ${error.message}`);
    } catch (reportError) {
      core.warning?.(`Unable to update staging status comment: ${reportError.message}`);
    }
    core.setFailed(error.message);
  } finally {
    core.setOutput('reported', 'true');
  }
}

module.exports = { deploy, nextStagingTag, normalizeTagTargets, selectPromotionTag, checkReady, unresolvedThreads, activeChangeRequests, conflictMessage, promotionSnapshot, hasWorkflowDispatch };
