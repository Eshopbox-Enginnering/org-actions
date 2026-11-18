'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {verifyCodex, verifyChecks, nextTag, StagingGit} = require('./auto-staging-deploy.cjs');
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
  assert.match(s,/mergeable_state === 'clean'/);
  assert.match(s,/merge_method:'merge'/);
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

test('real staging caller check is exempt only when linked to staging workflow run', () => {
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
  assert.throws(() => verifyChecks({...base,checks:[{...stagingCheck,check_suite:{id:99}}],
    protection:{requiresStatusChecks:true,requiredStatusChecks:[{context:'check-commits-in-staging'}]}}),/check-commits-in-staging: failure/);
});
test('staging PR promotion fails closed without merge support or queue support', () => {
  const s = fs.readFileSync(path.join(__dirname,'staging-pr.cjs'),'utf8');
  assert.match(s,/allow_merge_commit/);
  assert.match(s,/merge_queue/);
  assert.match(s,/--atomic/);
  assert.match(s,/refs\/heads\/staging/);
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
