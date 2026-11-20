'use strict';

// Extends Shivani's validated read/review/check logic. Never directly pushes staging.
const {StagingGit, nextTag} = require('./auto-staging-deploy.cjs');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
// Fail closed on an absent required check; --admin intentionally bypasses
// GitHub's review rules so CI readiness must be checked explicitly.
function promotionReadiness({checks, statuses, rules, protection, runs, sha, head, number}) {
  const byName = new Map();
  for (const check of checks) {
    const key = check.name;
    const existing = byName.get(key);
    if (!existing || (Date.parse(check.started_at || '') || 0) > (Date.parse(existing.started_at || '') || 0))
      byName.set(key, check);
  }
  const required = [
    ...(protection?.required_status_checks?.contexts || []).map(context => ({context})),
    ...(protection?.required_status_checks?.checks || []).map(check => ({context:check.context,app_id:check.app_id})),
    ...rules.filter(rule => rule.type === 'required_status_checks')
      .flatMap(rule => rule.parameters?.required_status_checks || [])
      .map(check => ({context:check.context,app_id:check.integration_id ?? check.app_id})),
  ];
  const errors = [];
  const pending = [];
  for (const req of required) {
    const check = byName.get(req.context);
    const status = (statuses.statuses || []).find(item => item.context === req.context);
    const appMatches = !req.app_id || req.app_id === -1 || check?.app?.id === req.app_id;
    if (check && appMatches) {
      if (check.status !== 'completed') pending.push(req.context);
      else if (!['success','neutral','skipped'].includes(check.conclusion))
        errors.push(req.context + ': ' + check.conclusion);
    } else if (req.app_id && req.app_id !== -1) {
      pending.push(req.context + ': required app check missing');
    } else if (!status) pending.push(req.context + ': missing');
    if (status?.state === 'pending') pending.push(req.context + ': status pending');
    else if (status && status.state !== 'success') errors.push(req.context + ': ' + status.state);
  }
  const requiredWorkflows = rules.filter(rule => rule.type === 'workflows')
    .flatMap(rule => rule.parameters?.workflows || []);
  for (const wf of requiredWorkflows) {
    const run = runs.find(run => run.head_sha === head && run.event === 'pull_request' &&
      (run.pull_requests || []).some(pr => pr.number === number) &&
      (run.path || '').split('@')[0] === wf.path &&
      (!wf.repository_id || run.repository?.id === wf.repository_id) &&
      checks.some(c => c.check_suite?.id === run.check_suite_id));
    if (!run) { pending.push(wf.path + ': missing required workflow'); continue; }
    if (run.status !== 'completed') pending.push(wf.path + ': running');
    else if (!['success','neutral','skipped'].includes(run.conclusion))
      errors.push(wf.path + ': ' + run.conclusion);
    const workflowJobs = checks.filter(c => c.check_suite?.id === run.check_suite_id);
    for (const job of workflowJobs) {
      if (job.status !== 'completed') pending.push(wf.path + ': job running');
      else if (!['success','neutral','skipped'].includes(job.conclusion))
        errors.push(wf.path + ': ' + job.name + ': ' + job.conclusion);
    }
  }
  const eligibleChecks = checks.filter(check => check.name !== 'deploy / deploy');
  for (const check of eligibleChecks) {
    if (check.status !== 'completed') pending.push(check.name + ': running');
    else if (!['success','neutral','skipped'].includes(check.conclusion))
      errors.push(check.name + ': ' + check.conclusion);
  }
  // Aggregate state includes contexts beyond the first REST response page.
  if (statuses.total_count > 0 && statuses.state === 'pending') pending.push('combined status pending');
  if (statuses.total_count > 0 && ['failure','error'].includes(statuses.state))
    errors.push('combined status: ' + statuses.state);
  if (!eligibleChecks.length && !(statuses.total_count > 0)) pending.push('no checks started');
  return {errors:[...new Set(errors)],pending:[...new Set(pending)]};
}

class StagingPR extends StagingGit {
  constructor(run, appGithub, callerGithub, repository) {
    super(run, {useStagingApp: true});
    this.app = appGithub;
    this.api = callerGithub;
    this.repo = repository;
  }

  async publish({number, head, issue, assertCurrent, status}) {
    // If this exact reviewed head is already in staging (e.g. prior promotion
    // merged, but tag or dispatch failed), resume at tagging instead of
    // creating another PR.
    let headAlreadyMerged = false;
    try { await this.git('merge-base', '--is-ancestor', head, this.staging); headAlreadyMerged = true; }
    catch {}
    if (headAlreadyMerged) {
      await assertCurrent();
      const tag = nextTag((await this.git('tag', '--list', 'dev-v*')).trim().split('\n'));
      await this.git('-c','user.name=github-actions[bot]',
        '-c','user.email=41898282+github-actions[bot]@users.noreply.github.com',
        'tag','-a',tag,this.staging,'-m','auto-staging-deploy:v1 ' + JSON.stringify({pr:number,head}));
      await this.git('push','--atomic','origin',
        this.staging + ':refs/heads/staging',
        'refs/tags/' + tag + ':refs/tags/' + tag);
      return {tag,commit:this.staging};
    }
    let tree;
    try {
      tree = (await this.git('merge-tree', '--write-tree', this.staging, head)).trim().split(/\s+/)[0];
    } catch (error) {
      const detail = String(error.stdout || error.stderr || error.message || '').slice(0, 3000);
      throw new Error('Staging merge conflict. Create a conflict-resolution PR from staging; merge it after review and retry /deploy-staging.\n' + detail);
    }
    if (!/^[0-9a-f]{40,64}$/.test(tree)) throw new Error('Git merge-tree did not produce a valid tree.');
    const stagedWorkflow = await this.git('show', tree + ':.github/workflows/cicd.yml').catch(() => '');
    if (!/^\s+workflow_dispatch\s*:/m.test(stagedWorkflow))
      throw new Error('The staging snapshot lacks workflow_dispatch in .github/workflows/cicd.yml. No staging PR created.');

    // The merge commit has staging first and the reviewed main PR head second.
    // The main PR head therefore remains an ancestor of the eventual staging result.
    let commit = (await this.git('-c', 'user.name=github-actions[bot]',
      '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
      'commit-tree', tree, '-p', this.staging, '-p', head,
      '-m', 'Promote reviewed PR #' + number + ' into staging')).trim();
    const branch = 'auto-staging/promote-' + number + '-' + head.slice(0, 12);
    await assertCurrent();

    // A dedicated branch from staging avoids touching feature/main.
    // A previously-created matching branch is reusable only if it has the same commit.
    const existing = await this.app.rest.git.getRef({...this.repo, ref: 'heads/' + branch}).catch(e => {
      if (e.status === 404) return null;
      throw e;
    });
    if (existing && existing.data.object.sha !== commit) {
      await this.git('fetch', '--no-recurse-submodules', 'origin',
        '+refs/heads/' + branch + ':refs/remotes/origin/' + branch);
      // commit-tree timestamps differ between attempts. Reuse an existing
      // trusted promotion commit only when the exact tree and both parents match.
      const oldSha = existing.data.object.sha;
      const meta = await this.git('cat-file', '-p', oldSha).catch(() => '');
      if (!meta.startsWith('tree ' + tree + '\nparent ' + this.staging + '\nparent ' + head + '\n'))
        throw new Error('Existing staging promotion branch differs from reviewed tree or parents: ' + branch);
      commit = oldSha;
    }
    if (!existing) await this.git('push', 'origin', commit + ':refs/heads/' + branch);

    let prs = await this.app.rest.pulls.list({...this.repo, state:'open', head:this.repo.owner + ':' + branch, base:'staging'});
    let pr = prs.data[0];
    if (!pr) {
      const response = await this.app.rest.pulls.create({
        ...this.repo, base:'staging', head:branch,
        title: 'Patch to Staging: Promote reviewed main PR #' + number,
        body: (issue ? 'Central-Backlog: https://github.com/Eshopbox-Enginnering/Central-Backlog/issues/' + issue + '\n\n' : '') +
          'Automated deployment staging PR. Source: #' + number +
          '\nReviewed head: ' + head + '\n\nAutomated promotion uses the organization-approved App after required source checks and staging check verification.'
      });
      pr = response.data;
    }
    process.env.STAGING_PR_URL = pr.html_url;
    process.env.STAGE_PR = '⏳ [#' + pr.number + '](' + pr.html_url + ') · automated verification and merge';
    await status('⏳ Staging PR created; validating checks for automatic merge');

    // The source PR passed the reviewed-head and required-check gates before
    // this trusted staging promotion PR was created. As with the existing
    // AGENTS/guidelines rollout, the organization-approved App merges the
    // bot-created staging PR without requesting a manual reviewer.
    // Never bypass a failing or pending observed CI check.
    const settings = (await this.app.rest.repos.get(this.repo)
      .catch(error => { throw new Error('Could not read repository merge settings with App token: ' + error.message); })).data;
    if (!settings.allow_merge_commit)
      throw new Error('Staging repository disallows ancestry-preserving merge commits: ' + pr.html_url);
    const activeRules = await this.api.paginate(this.api.rest.repos.getBranchRules, {...this.repo, branch:'staging', per_page:100})
      .catch(error => { throw new Error('Could not read staging branch rules: ' + error.message); });
    // REST getBranchProtection requires Administration:read, which the caller's
    // least-privilege GITHUB_TOKEN does not have. Use the same GraphQL
    // branchProtectionRule query that the source-PR gate already relies on.
    const protectionResult = await this.api.graphql(`query($owner:String!,$repo:String!) {
      repository(owner:$owner,name:$repo) {
        ref(qualifiedName:"refs/heads/staging") {
          ... on Ref { branchProtectionRule {
            requiresStatusChecks
            requiredStatusChecks { context app { databaseId } }
          } }
        }
      }
    }`,this.repo).catch(error => {
      throw new Error('Could not read staging branch protection through GraphQL: ' + error.message);
    });
    const protectionRule = protectionResult.repository?.ref?.branchProtectionRule;
    const branchProtection = protectionRule?.requiresStatusChecks ? {
      required_status_checks: { checks: (protectionRule.requiredStatusChecks || []).map(check => ({
        context: check.context, app_id: check.app?.databaseId ?? -1
      })) }
    } : null;
    if (activeRules.some(rule => rule.type === 'merge_queue'))
      throw new Error('Staging requires a merge queue. Automated admin merge is not supported: ' + pr.html_url);

    const expectedStagingBase = this.staging;
    const deadline = Date.now() + 20 * 60 * 1000;
    let lastMergeError;
    while (Date.now() < deadline) {
      await assertCurrent();
      const current = (await this.app.rest.pulls.get({...this.repo, pull_number:pr.number})).data;
      if (current.state === 'closed') {
        if (!current.merged) throw new Error('Staging PR was closed without merging: ' + pr.html_url);
        break;
      }
      if (current.head.sha !== commit)
        throw new Error('Staging PR head moved unexpectedly; stopping: ' + pr.html_url);
      if (current.base.sha !== this.staging)
        throw new Error('Staging advanced after promotion PR creation; stop instead of merging stale base: ' + pr.html_url);

      // pull_request jobs normally run on the synthetic test-merge SHA.
      // Never silently fall back to head checks if a merge SHA exists.
      const checkedSha = current.merge_commit_sha || commit;
      const observed = await this.api.paginate(this.api.rest.checks.listForRef,
        {...this.repo, ref:checkedSha, per_page:100}).catch(error => {
          throw new Error('Could not read staging PR check runs: ' + error.message);
        });
      const statuses = (await this.api.rest.repos.getCombinedStatusForRef(
        {...this.repo,ref:checkedSha}).catch(error => {
          throw new Error('Could not read staging PR combined statuses: ' + error.message);
        })).data;
      const runs = await this.api.paginate(this.api.rest.actions.listWorkflowRunsForRepo,
        {...this.repo,head_sha:commit,event:'pull_request',per_page:100}).catch(error => {
          throw new Error('Could not read staging PR workflow runs: ' + error.message);
        });
      const readiness = promotionReadiness({
        checks:observed,statuses,rules:activeRules,protection:branchProtection,
        runs,sha:checkedSha,head:commit,number:pr.number
      });
      if (readiness.errors.length)
        throw new Error('Staging PR has failed checks: ' + readiness.errors.join(', ') + '. ' + pr.html_url);
      if (!readiness.pending.length) {
        await assertCurrent();
        const beforeMerge = (await this.app.rest.pulls.get({...this.repo,pull_number:pr.number})).data;
        if (beforeMerge.head.sha !== commit || beforeMerge.base.sha !== this.staging ||
            beforeMerge.merge_commit_sha !== checkedSha)
          throw new Error('Staging PR SHA, base, or test-merge commit changed before administrative merge.');
        try {
          await this.run('gh', ['pr','merge',String(pr.number),'--repo',
            this.repo.owner + '/' + this.repo.repo,'--merge','--admin',
            '--match-head-commit',commit], {appAuth:true});
          break;
        } catch (error) {
          // execFile error.message includes the gh command itself; inspect
          // stderr only to distinguish a transient PR readiness failure.
          const detail = String(error.stderr || error.stdout || '').trim();
          if (!/required checks|review required|is not mergeable|merge conflict|branch protection|ruleset/i.test(detail))
            throw error;
          lastMergeError = detail;
        }
      }
      await pause(15000);
    }
    const finalPR = (await this.app.rest.pulls.get({...this.repo, pull_number:pr.number})).data;
    if (!finalPR.merged) throw new Error('Automated staging merge did not complete: ' + pr.html_url +
      '. ' + (lastMergeError || 'Checks are still pending or the App cannot administratively merge.') +
      ' No tag or deployment was created.');
    await this.git('fetch', '--no-recurse-submodules', 'origin',
      '+refs/heads/staging:refs/remotes/origin/staging', '+refs/tags/*:refs/tags/*');
    this.staging = (await this.git('rev-parse','refs/remotes/origin/staging')).trim();
    if (finalPR.merge_commit_sha && this.staging !== finalPR.merge_commit_sha)
      throw new Error('Staging advanced after merging the promotion PR; refuse to tag an unreviewed snapshot.');
    const mergedMeta = await this.git('cat-file','-p',this.staging);
    if (!mergedMeta.startsWith('tree ' + tree + '\nparent ' + expectedStagingBase + '\nparent ' + commit + '\n'))
      throw new Error('Staging merge parents do not match verified promotion head and base. Refusing deployment.');
    await this.git('merge-base', '--is-ancestor', head, this.staging).catch(() => {
      throw new Error('Merged staging does not contain reviewed main PR head. Refusing to deploy.');
    });
    await assertCurrent();
    const tag = nextTag((await this.git('tag', '--list', 'dev-v*')).trim().split('\n'));
    await this.git('-c','user.name=github-actions[bot]',
      '-c','user.email=41898282+github-actions[bot]@users.noreply.github.com',
      'tag','-a',tag,this.staging,'-m','auto-staging-deploy:v1 ' + JSON.stringify({pr:number,head}));
    // GITHUB_TOKEN is deliberately used for the tag to avoid duplicate tag-push deploys.
    await this.git('push','--atomic','origin',
        this.staging + ':refs/heads/staging',
        'refs/tags/' + tag + ':refs/tags/' + tag);
    return {tag,commit:this.staging};
  }
}
module.exports = {StagingPR,promotionReadiness};
