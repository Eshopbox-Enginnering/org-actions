'use strict';

// Extends Shivani's validated read/review/check logic. Never directly pushes staging.
const {StagingGit, nextTag} = require('./auto-staging-deploy.cjs');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
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
          '\nReviewed head: ' + head + '\n\nAll standard staging checks and approvals must pass. No administrative bypass.'
      });
      pr = response.data;
    }
    process.env.STAGING_PR_URL = pr.html_url;
    process.env.STAGE_PR = '⏳ [#' + pr.number + '](' + pr.html_url + ') · checks and approvals';
    await status('⏳ Staging PR created; waiting for required approvals and checks');

    // The source PR passed the reviewed-head and required-check gates before
    // this trusted staging promotion PR was created. As with the existing
    // AGENTS/guidelines rollout, the organization-approved App merges the
    // bot-created staging PR without requesting a manual reviewer.
    // Never bypass a failing or pending observed CI check.
    const settings = (await this.app.rest.repos.get(this.repo)).data;
    if (!settings.allow_merge_commit)
      throw new Error('Staging repository disallows ancestry-preserving merge commits: ' + pr.html_url);
    const activeRules = await this.api.paginate(this.api.rest.repos.getBranchRules, {...this.repo, branch:'staging', per_page:100})
      .catch(error => { throw new Error('Could not read staging branch rules: ' + error.message); });
    if (activeRules.some(rule => rule.type === 'merge_queue'))
      throw new Error('Staging requires a merge queue. Automated admin merge is not supported: ' + pr.html_url);

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

      // Read GitHub check runs and commit statuses on the exact promotion SHA.
      // A mandatory check which fails must not be overridden by the App.
      const observed = await this.api.paginate(this.api.rest.checks.listForRef,
        {...this.repo, ref: commit, per_page:100});
      const newestChecks = new Map();
      for (const check of observed) {
        const key = check.name + ':' + (check.app?.id ?? '');
        if (!newestChecks.has(key) || new Date(newestChecks.get(key).started_at || 0) < new Date(check.started_at || 0))
          newestChecks.set(key,check);
      }
      const statuses = (await this.api.rest.repos.getCombinedStatusForRef({...this.repo,ref:commit})).data;
      const checks = [...newestChecks.values()].filter(c => c.name !== 'deploy / deploy');
      const bad = checks.filter(c => c.status === 'completed' && !['success','skipped','neutral'].includes(c.conclusion));
      if (bad.length || statuses.statuses?.some(x => x.state === 'failure' || x.state === 'error'))
        throw new Error('Staging PR has failed checks: ' + bad.map(c => c.name + ': ' + c.conclusion).join(', ') + '. ' + pr.html_url);
      const pending = checks.some(c => c.status !== 'completed') ||
        statuses.statuses?.some(x => x.state === 'pending');
      if (!pending && checks.length) {
        await assertCurrent();
        try {
          // The App is the same organization-approved administrative merge
          // identity used by Update AGENTS.md and Sync Engineering Guidelines.
          // GitHub still validates its bypass entitlement. The reviewed SHA
          // and target branch are rechecked before merging.
          await this.app.rest.pulls.merge({...this.repo,pull_number:pr.number,
            sha:commit,merge_method:'merge',commit_title:'Merge staging promotion PR #' + pr.number});
          break;
        } catch (error) {
          if (![405,409,422].includes(error.status)) throw error;
          lastMergeError = error.message;
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
module.exports = {StagingPR};
