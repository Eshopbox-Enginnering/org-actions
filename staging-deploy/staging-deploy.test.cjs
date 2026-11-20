'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {verifyCodex, verifyChecks, nextTag, StagingGit, hasCodexQuotaEvidence} = require('./auto-staging-deploy.cjs');
const {StagingPR} = require('./staging-pr.cjs');

const SHA = 'a'.repeat(40);
const bot = { user: {login:'chatgpt-codex-connector[bot]'} };
test('same-head clean Codex receipt is accepted', () => {
  assert.doesNotThrow(() => verifyCodex({
    head:SHA, requestId:1, comments:[
      {id:1,body:'@codex review',created_at:'2026-11-01T00:00:00Z'},
      {...bot,body:"Codex Review: Didn't find any major issues.\nReviewed commit: " + SHA.slice(0,10),
        created_at:'2026-11-01T00:01:00Z'}], reviews:[],reactions:[]
  }));
});
test('old-head Codex review is rejected', () => {
  assert.throws(() => verifyCodex({
    head:SHA, comments:[{...bot,body:"Codex Review: Didn't find any major issues.\nReviewed commit: " + 'b'.repeat(10),
      created_at:'2026-11-01T00:01:00Z'}],reviews:[],reactions:[]
  }),/has not cleared/);
});
test('staging-presence check is exempt but other missing checks block', () => {
  assert.doesNotThrow(() => verifyChecks({head:SHA,number:1,
    protection:{requiresStatusChecks:true,requiredStatusChecks:[{context:'check-commits'}]},
    rules:[{type:'workflows',parameters:{workflows:[{path:'.github/workflows/check-commits-in-staging.yml'}]}}],checks:[],statuses:[],runs:[]}));
  assert.throws(() => verifyChecks({head:SHA,number:1,
    protection:{requiresStatusChecks:true,requiredStatusChecks:[{context:'check-commits'},{context:'java-tests'}]},
    rules:[{type:'workflows',parameters:{workflows:[{path:'.github/workflows/check-commits-in-staging.yml'}]}}],checks:[],statuses:[],runs:[]}),/java-tests: missing/);
});
test('versioning retains numeric patch ordering', () => {
  assert.equal(nextTag(['dev-v1.1.9','dev-v1.1.10','dev-v1.2.0']), 'dev-v1.2.1');
});
test('staging PR implementation replaces direct promotion', () => {
  assert.ok(StagingPR.prototype instanceof StagingGit);
  const s = fs.readFileSync(path.join(__dirname,'staging-pr.cjs'),'utf8');
  assert.match(s,/this\.run\('gh'/);
  assert.match(s,/--match-head-commit/);
  assert.match(s,/--merge/);
  assert.doesNotMatch(s,/mergeable_state === 'clean'/);
  assert.match(s,/commit-tree/);
  assert.doesNotMatch(s,/refs\/heads\/staging'\]/);
});
test('structured single-comment reporting and quota fallback are present', () => {
  const source = fs.readFileSync(path.join(__dirname,'auto-staging-deploy.cjs'),'utf8');
  assert.match(source,/central-staging-deploy:v1/);
  assert.match(source,/PATCH \/repos\/\{owner\}\/\{repo\}\/issues\/comments\/\{comment_id\}/);
  assert.match(source,/Codex quota exhausted/);
  assert.match(source,/STAGING_RUN_URL/);
});

test('expected staging-presence failure is exempt during pre-promotion', () => {
  const run = {id:4,event:'pull_request',head_sha:SHA,pull_requests:[{number:1}],
    path:'.github/workflows/check-commits-in-staging.yml@main',check_suite_id:42};
  const stagingCheck = {name:'check-commits-in-staging',app:{id:15368},check_suite:{id:42},
    status:'completed',conclusion:'failure'};
  const otherFail = {name:'java-tests',app:{id:15368},check_suite:{id:43},
    status:'completed',conclusion:'failure'};
  const base = {head:SHA,number:1,protection:{requiresStatusChecks:true,requiredStatusChecks:[
    {context:'check-commits-in-staging'},{context:'java-tests'}]}, rules:[],statuses:[],runs:[run]};
  assert.throws(() => verifyChecks({...base,checks:[stagingCheck,otherFail]}),/java-tests: failure/);
  assert.doesNotThrow(() => verifyChecks({...base,checks:[stagingCheck],
    protection:{requiresStatusChecks:true,requiredStatusChecks:[{context:'check-commits-in-staging'}]}}));
  assert.doesNotThrow(() => verifyChecks({...base,checks:[{...stagingCheck,check_suite:{id:99}}],
    protection:{requiresStatusChecks:true,requiredStatusChecks:[{context:'check-commits-in-staging'}]}}));
});
test('temporary staging promotion uses app merge and verifies resulting merge before tagging', () => {
  const s = fs.readFileSync(path.join(__dirname,'staging-pr.cjs'),'utf8');
  assert.match(s,/--match-head-commit/);
  assert.match(s,/this\.run\('gh'/);
  assert.match(s,/--merge/);
  assert.match(s,/expectedStagingBase/);
  assert.match(s,/finalPR\.merged/);
  assert.match(s,/mergedMeta\.startsWith/);
  assert.match(s,/--atomic/);
});
test('quota fallback has a head-bound structured proof, not prose substring matching', () => {
  const s = fs.readFileSync(path.join(__dirname,'auto-staging-deploy.cjs'),'utf8');
  assert.match(s,/codex-review-unavailable:v1/);
  assert.match(s,/receipt\.status === 'rate_limited' && receipt\.commit_sha === head/);
  assert.doesNotMatch(s,/some\(c => \/\(\?:rate limit\|usage limit/);
});
test('fork pull requests are blocked before staging promotion', () => {
  const s = fs.readFileSync(path.join(__dirname,'auto-staging-deploy.cjs'),'utf8');
  assert.match(s,/pr\.head\.repo\?\.id !== pr\.base\.repo\?\.id/);
});

test('current-head completed clean Codex review summary clears without explicit request', () => {
  const state = {code_review: {commit_sha: SHA, status: 'completed', has_any_findings: false,
    has_unresolved_findings: false}};
  const summary = {id:2, ...bot, created_at:'2026-11-18T09:01:48Z',
    body:'<!-- codex-review-state:v1 ' + JSON.stringify(state) + ' -->'};
  assert.doesNotThrow(() => verifyCodex({head:SHA,comments:[summary],reviews:[],reactions:[]}));
});
test('Codex summary fails closed for stale SHA, findings and pending status', () => {
  const cases = [
    {commit_sha:'b'.repeat(40),status:'completed',has_any_findings:false,has_unresolved_findings:false},
    {commit_sha:SHA,status:'completed',has_any_findings:true,has_unresolved_findings:true},
    {commit_sha:SHA,status:'pending',has_any_findings:false,has_unresolved_findings:false},
  ];
  for (const code_review of cases) {
    const comment = {id:2,...bot,created_at:'2026-11-18T09:01:48Z',
      body:'<!-- codex-review-state:v1 ' + JSON.stringify({code_review}) + ' -->'};
    assert.throws(() => verifyCodex({head:SHA,comments:[comment],reviews:[],reactions:[]}),/has not cleared/);
  }
});
test('newer Codex review request invalidates an earlier clean summary', () => {
  const code_review = {commit_sha:SHA,status:'completed',has_any_findings:false,has_unresolved_findings:false};
  const comments = [{id:2,...bot,created_at:'2026-11-18T09:01:48Z',
    body:'<!-- codex-review-state:v1 ' + JSON.stringify({code_review}) + ' -->'},
    {id:3,body:'@codex review',created_at:'2026-11-18T10:00:00Z'}];
  assert.throws(() => verifyCodex({head:SHA,comments,reviews:[],reactions:[],requestId:3}),/has not cleared/);
});

test('PR #600: failed check-commits is expected and does not block pre-promotion', () => {
  const stagingCheck = {name:'check-commits',status:'completed',conclusion:'failure',
    app:{id:15368},check_suite:{id:99}};
  const base = {head:SHA,number:600,protection:{requiresStatusChecks:true,
    requiredStatusChecks:[{context:'check-commits'}]},
    rules:[],checks:[stagingCheck],statuses:[],runs:[]};
  assert.doesNotThrow(() => verifyChecks(base));
  assert.throws(() => verifyChecks({...base,
    protection:{requiresStatusChecks:true,requiredStatusChecks:[
      {context:'check-commits'},{context:'java-tests'}]}}),/java-tests: missing/);
  assert.throws(() => verifyChecks({...base,
    protection:{requiresStatusChecks:true,requiredStatusChecks:[{context:'check-commits-extra'}]},
    checks:[{...stagingCheck,name:'check-commits-extra'}]}),/check-commits-extra: failure/);
});
test('deployment status does not repeat PR number and marks failed stages accurately', () => {
  const source = fs.readFileSync(path.join(__dirname,'auto-staging-deploy.cjs'),'utf8');
  assert.match(source,/## 🚀 Staging deployment/);
  assert.doesNotMatch(source,/Staging deployment · PR #|Main PR:/);
  assert.ok(source.includes("if (failed && failedStep === key) return '❌ Failed'"));
  assert.ok(source.includes("failed ? '⏭️ Not started'"));
});

test('auto staging merge uses approved App token and refuses missing credentials', () => {
  const source = fs.readFileSync(path.join(__dirname,'auto-staging-deploy.cjs'),'utf8');
  const staging = fs.readFileSync(path.join(__dirname,'staging-pr.cjs'),'utf8');
  assert.match(source,/env\.GH_TOKEN = env\.STAGING_DEPLOY_APP_TOKEN/);
  assert.match(source,/Organization App token is missing/);
  assert.match(staging,/--match-head-commit/);
  assert.doesNotMatch(staging,/promotionReadiness|readiness\.pending/);
  assert.match(staging,/current\.base\.sha !== expectedStagingBase/);
  assert.doesNotMatch(staging,/refs\/heads\/staging'\]/);
});

test('temporary staging promotion never waits on its own CI checks', () => {
  const source = fs.readFileSync(path.join(__dirname,'staging-pr.cjs'),'utf8');
  assert.doesNotMatch(source,/promotionReadiness|getBranchRules|getBranchProtection|listForRef|getCombinedStatusForRef|listWorkflowRunsForRepo/);
  assert.match(source,/No staging promotion PR CI checks or manual approval are required/);
  assert.match(source,/await assertCurrent\(\)/);
  assert.match(source,/current\.head\.sha !== commit/);
  assert.match(source,/current\.base\.sha !== expectedStagingBase/);
  assert.match(source,/--match-head-commit/);
  assert.match(source,/this\.run\('gh'/);
});
test('staging App REST merge verifies exact base tree and handles API errors', () => {
  const source = fs.readFileSync(path.join(__dirname,'staging-pr.cjs'),'utf8');
  assert.match(source,/--match-head-commit/);
  assert.match(source,/expectedStagingBase/);
  assert.match(source,/mergedMeta\.startsWith/);
  assert.match(source,/Automatic staging PR merge failed/);
  assert.doesNotMatch(source,/lastMergeError/);
});

test('each deploy-staging invocation acknowledges and edits only its own status comment', () => {
  const workflow = fs.readFileSync(path.join(__dirname,'../.github/workflows/reusable-staging-deploy.yml'),'utf8');
  const source = fs.readFileSync(path.join(__dirname,'auto-staging-deploy.cjs'),'utf8');
  assert.match(workflow,/  acknowledge:/);
  assert.match(workflow,/    needs: acknowledge/);
  assert.match(workflow,/github\.rest\.issues\.createComment/);
  assert.match(workflow,/STAGING_ACK_COMMENT_ID: \$\{\{ needs\.acknowledge\.outputs\.comment_id \}\}/);
  assert.match(workflow,/github\.rest\.issues\.updateComment/);
  assert.match(source,/Number\(process\.env\.STAGING_ACK_COMMENT_ID\)/);
  assert.match(source,/PATCH \/repos\/\{owner\}\/\{repo\}\/issues\/comments\/\{comment_id\}/);
  assert.doesNotMatch(source,/const mine = \[\.\.\.existing\]\.reverse\(\)/);
});

test('staging status links tag to CI/CD run, not Git tree', () => {
  const source = fs.readFileSync(path.join(__dirname,'auto-staging-deploy.cjs'),'utf8');
  assert.match(source,/STAGING_RUN_URL \? \x60\[\$\{process\.env\.STAGING_TAG\} — View deployment\]/);
  assert.doesNotMatch(source,/\/tree\/\$\{process\.env\.STAGING_TAG\}/);
  assert.match(source,/process\.env\.STAGING_RUN_URL = deployment\.html_url/);
});
test('staging status distinguishes a replay from a new promotion', () => {
  const source = fs.readFileSync(path.join(__dirname,'auto-staging-deploy.cjs'),'utf8');
  assert.match(source,/const previouslyTagged = Boolean\(published\)/);
  assert.match(source,/Existing staging promotion verified \(no new PR or merge\)/);
  assert.match(source,/no new tag or deployment/);
  assert.match(source,/Staging PR auto-merged by authorized GitHub App/);
});

test('exact authenticated Codex quota response plus failed current-head summary enables human fallback', () => {
  const head = SHA;
  const summary = {id:2,...bot,created_at:'2026-11-19T12:04:28Z',
    body:'<!-- codex-review-state:v1 ' + JSON.stringify({code_review:{
      commit_sha:head,status:'failed',has_any_findings:true,has_unresolved_findings:true
    }}) + ' -->'};
  const quota = {id:3,...bot,created_at:'2026-11-19T12:04:30Z',
    body:'You have reached your Codex usage limits for code reviews. You can see your limits in the [Codex usage dashboard](https://chatgpt.com/codex/cloud/settings/usage).'};
  const base = {comments:[summary,quota],head,prCreatedAt:Date.parse('2026-11-19T12:03:00Z')};
  assert.equal(hasCodexQuotaEvidence(base),true);
  assert.equal(hasCodexQuotaEvidence({...base,head:'b'.repeat(40)}),false);
  assert.equal(hasCodexQuotaEvidence({...base,latestRequestAt:Date.parse('2026-11-19T12:05:00Z')}),false);
  assert.equal(hasCodexQuotaEvidence({...base,comments:[quota]}),false);
  assert.equal(hasCodexQuotaEvidence({...base,comments:[{...quota,user:{login:'someone-else'}} ,summary]}),false);
  assert.equal(hasCodexQuotaEvidence({...base,comments:[{...summary,body:summary.body.replace('failed','completed')},quota]}),false);
});
test('structured current-head Codex rate-limit receipt remains supported', () => {
  const evidence = {id:4,...bot,created_at:'2026-11-19T12:04:30Z',
    body:'<!-- codex-review-unavailable:v1 ' + JSON.stringify({status:'rate_limited',commit_sha:SHA}) + ' -->'};
  assert.equal(hasCodexQuotaEvidence({comments:[evidence],head:SHA}),true);
  assert.equal(hasCodexQuotaEvidence({comments:[evidence],head:'b'.repeat(40)}),false);
});


test('temporary staging PR waits for mergeability calculation, not its checks', () => {
  const source = fs.readFileSync(path.join(__dirname,'staging-pr.cjs'),'utf8');
  assert.match(source,/current\.mergeable === true/);
  assert.match(source,/current\.mergeable === false/);
  assert.match(source,/current\.mergeable !== true/);
  assert.match(source,/attempt < 10/);
  assert.match(source,/await assertCurrent\(\)/);
  assert.match(source,/beforeMerge\.head\.sha !== commit/);
  assert.match(source,/beforeMerge\.base\.sha !== expectedStagingBase/);
  assert.doesNotMatch(source,/promotionReadiness|listForRef|getCombinedStatusForRef/);
});

test('staging merge reuses App-authenticated gh CLI on proven checks runner', () => {
  const source = fs.readFileSync(path.join(__dirname,'staging-pr.cjs'),'utf8');
  const workflow = fs.readFileSync(path.join(__dirname,'../.github/workflows/reusable-staging-deploy.yml'),'utf8');
  assert.match(source,/this\\.run\\('gh'/);
  assert.match(source,/--merge/);
  assert.match(source,/--admin/);
  assert.match(source,/--match-head-commit/);
  assert.match(source,/appAuth: true/);
  assert.doesNotMatch(source,/this\\.app\\.rest\\.pulls\\.merge/);
  assert.match(workflow,/group: checks-runners[\\s\\S]*labels: checks-runner/);
  assert.match(workflow,/command -v gh/);
  assert.match(workflow,/gh --version/);
});
