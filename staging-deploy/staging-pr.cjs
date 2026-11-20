'use strict';

// Extends Shivani's validated read/review/check logic. Never directly pushes staging.
const {StagingGit, nextTag} = require('./auto-staging-deploy.cjs');
// The original main-targeted PR is the sole place for review and CI gates.
// This temporary promotion PR is merged through the approved organization App,
// without waiting for separate staging PR CI or a human approval.
class StagingPR extends StagingGit {
  constructor(run, appGithub, callerGithub, repository) {
    super(run, {useStagingApp: true});
    this.app = appGithub;
    this.repo = repository;
  }

  async publish({number, head, issue, workflowFile, assertCurrent, status}) {
    if (!['cicd.yml','ci-cd.yml'].includes(workflowFile))
      throw new Error('Unsupported CI/CD workflow for staging promotion');
    // If this exact reviewed head is already in staging (e.g. prior promotion
    // merged, but tag or dispatch failed), resume at tagging instead of
    // creating another PR.
    let headAlreadyMerged = false;
    try { await this.git('merge-base', '--is-ancestor', head, this.staging); headAlreadyMerged = true; }
    catch {}
    if (headAlreadyMerged) {
      await assertCurrent();
      // An earlier or manual promotion can bypass the merge-tree validation
      // below. Never create a tag unless this exact staging commit contains
      // the selected, dispatchable service CI/CD workflow.
      const existingWorkflow = await this.git('show',
        this.staging + ':.github/workflows/' + workflowFile).catch(() => '');
      if (!/^\s+workflow_dispatch\s*:/m.test(existingWorkflow))
        throw new Error('The existing staging snapshot lacks workflow_dispatch in .github/workflows/' + workflowFile + '. No staging tag created.');
      await this.assertStaging(this.staging);
      const tag = nextTag((await this.git('tag', '--list', 'dev-v*')).trim().split('\n'));
      await this.git('-c','user.name=github-actions[bot]',
        '-c','user.email=41898282+github-actions[bot]@users.noreply.github.com',
        'tag','-a',tag,this.staging,'-m','auto-staging-deploy:v1 ' + JSON.stringify({pr:number,head}));
      // The staging PR has already merged (or the reviewed head is present).
    // Never attempt to re-push the protected staging branch via GITHUB_TOKEN.
    await this.assertStaging(this.staging);
    await this.git('push','origin','refs/tags/' + tag + ':refs/tags/' + tag);
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
    const stagedWorkflow = await this.git('show', tree + ':.github/workflows/' + workflowFile).catch(() => '');
    if (!/^\s+workflow_dispatch\s*:/m.test(stagedWorkflow))
      throw new Error('The staging snapshot lacks workflow_dispatch in .github/workflows/' + workflowFile + '. No staging PR created.');

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
          '\nReviewed head: ' + head + '\n\nAutomated promotion follows validated source PR checks. The organization-approved App merges this temporary staging PR without staging-PR CI or human review.'
      });
      pr = response.data;
    }
    process.env.STAGING_PR_URL = pr.html_url;
    process.env.STAGE_PR = '⏳ [#' + pr.number + '](' + pr.html_url + ') · automatic merge pending';
    await status('⏳ Staging PR ready; merging automatically (no staging PR checks)');

    // Source-PR approval/Codex fallback, required source checks and the
    // reviewed head have already passed before this PR is created.
    // No staging promotion PR CI checks or manual approval are required.
    const expectedStagingBase = this.staging;
    // GitHub may return mergeable:null while calculating the synthetic merge.
    // Wait only for mergeability, never for staging PR CI or review approval.
    let current;
    for (let attempt = 0; attempt < 10; attempt++) {
      await assertCurrent();
      current = (await this.app.rest.pulls.get({...this.repo,pull_number:pr.number})).data;
      if (current.head.sha !== commit)
        throw new Error('Staging promotion PR head changed unexpectedly: ' + pr.html_url);
      if (current.base.sha !== expectedStagingBase)
        throw new Error('Staging advanced after promotion PR creation; refusing stale merge: ' + pr.html_url);
      if (current.state === 'closed') {
        if (!current.merged) throw new Error('Staging PR was closed without merging: ' + pr.html_url);
        break;
      }
      if (current.mergeable === true) break;
      if (current.mergeable === false)
        throw new Error('Staging promotion PR has merge conflicts: ' + pr.html_url);
      if (attempt < 9) await new Promise(resolve => setTimeout(resolve, 2000));
    }
    if (current.state !== 'closed') {
      if (current.mergeable !== true)
        throw new Error('GitHub has not completed staging PR mergeability calculation: ' + pr.html_url);
      await assertCurrent();
      const beforeMerge = (await this.app.rest.pulls.get({...this.repo,pull_number:pr.number})).data;
      if (beforeMerge.state !== 'open' || beforeMerge.head.sha !== commit ||
          beforeMerge.base.sha !== expectedStagingBase || beforeMerge.mergeable === false)
        throw new Error('Staging promotion PR changed before automatic merge: ' + pr.html_url);
      try {
        // Use the same administrative App-authenticated gh merge path as the
        // proven engineering-guidelines / AGENTS rollout on checks-runners.
        // The checked head is pinned, and the merged parents/tree are checked
        // again below before any tag or deployment can occur.
        await this.run('gh', ['pr', 'merge', String(pr.number), '--repo',
          this.repo.owner + '/' + this.repo.repo, '--merge', '--admin',
          '--match-head-commit', commit], {appAuth: true});
      } catch (error) {
        const detail = String(error.stderr || error.stdout || error.message || '').trim();
        throw new Error('Automatic staging PR merge failed: ' + detail + '. ' + pr.html_url);
      }
    }
    const finalPR = (await this.app.rest.pulls.get({...this.repo, pull_number:pr.number})).data;
    if (!finalPR.merged) throw new Error('Automatic staging PR merge did not complete: ' + pr.html_url + '. No tag or deployment was created.');
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
    // The staging PR has already merged (or the reviewed head is present).
    // Never attempt to re-push the protected staging branch via GITHUB_TOKEN.
    await this.assertStaging(this.staging);
    await this.git('push','origin','refs/tags/' + tag + ':refs/tags/' + tag);
    return {tag,commit:this.staging};
  }
}
module.exports = {StagingPR};
