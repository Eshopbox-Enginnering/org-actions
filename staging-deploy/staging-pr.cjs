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
    let commit = (await this.git('-c', 'user.name=github-actions[bot]'
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
      // commit-tree timestamps differ between attempts. Reuse an existing
      // trusted promotion commit only when the exact tree and both parents match.
      const oldSha = existing.data.object.sha;
      const meta = await this.git('cat-file', '-p', oldSha).catch(() => '');
      if (!meta.startsWith('tree ' + tree + '\\nparent ' + this.staging + '\\nparent ' + head + '\\n'))
        throw new Error('Existing staging promotion branch differs from reviewed tree or parents: ' + branch);
      commit = oldSha;
    }
    if (!existing) await this.run('git', ['-c', 'core.hooksPath=/dev/null', 'push', 'origin',
      commit + ':refs/heads/' + branch], {stagingAuth:true});

    let prs = await this.app.rest.pulls.list({...this.repo, state:'open', head:this.repo.owner + ':' + branch, base:'staging'});
    let pr = prs.data[0];
    if (!pr) {
      const response = await this.app.rest.pulls.create({
        ...this.repo, base:'staging', head:branch,
        title: 'Patch to Staging: Promote reviewed main PR #' + number,
        body: 'Central-Backlog: https://github.com/Eshopbox-Enginnering/Central-Backlog/issues/' + (issue || '7319') +
          '\n\nAutomated deployment staging PR. Source: #' + number +
          '\nReviewed head: ' + head + '\n\nAll standard staging checks and approvals must pass. No administrative bypass.'
      });
      pr = response.data;
    }
    process.env.STAGING_PR_URL = pr.html_url;
    process.env.STAGE_PR = '⏳ [#' + pr.number + '](' + pr.html_url + ') · checks and approvals';
    await status('⏳ Staging PR created; waiting for required approvals and checks');

    // Fail closed: never use a protected-branch bypass. "clean" is GitHub's
    // mergeability verdict after branch rules, review requirements and checks.
    // A pending PR remains visible for authorized human review.
    const deadline = Date.now() + 20 * 60 * 1000;
    while (Date.now() < deadline) {
      await assertCurrent();
      const current = (await this.app.rest.pulls.get({...this.repo, pull_number:pr.number})).data;
      if (current.state === 'closed') {
        if (!current.merged) throw new Error('Staging PR was closed without merging: ' + pr.html_url);
        break;
      }
      if (current.head.sha !== commit) throw new Error('Staging PR head moved unexpectedly; stopping: ' + pr.html_url);
      if (current.mergeable_state === 'clean') {
        // Standard merge endpoint: do not set any bypass/admin flag.
        // Pin the staging PR SHA against races.
        await assertCurrent();
        try {
          await this.app.rest.pulls.merge({...this.repo, pull_number:pr.number, sha:commit, merge_method:'merge',
            commit_title:'Merge staging promotion PR #' + pr.number});
        } catch(e) {
          if (![405,409,422].includes(e.status)) throw e;
          // GitHub may have updated checks/protection while merge was attempted.
          // Do not force a merge, continue waiting for genuine green readiness.
        }
      }
      await pause(15000);
    }
    const finalPR = (await this.app.rest.pulls.get({...this.repo, pull_number:pr.number})).data;
    if (!finalPR.merged) throw new Error('Staging PR is awaiting required checks or approval: ' + pr.html_url +
      '. After approvals, retry /deploy-staging. No tag or deployment was created.');
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
    await this.git('push','origin','refs/tags/' + tag + ':refs/tags/' + tag);
    return {tag,commit:this.staging};
  }
}
module.exports = {StagingPR};
