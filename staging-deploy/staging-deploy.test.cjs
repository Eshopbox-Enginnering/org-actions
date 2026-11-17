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
    rules:[],checks:[],statuses:[],runs:[]}));
  assert.throws(() => verifyChecks({head:SHA,number:1,
    protection:{requiresStatusChecks:true,requiredStatusChecks:[{context:'check-commits'},{context:'java-tests'}]},
    rules:[],checks:[],statuses:[],runs:[]}),/java-tests: missing/);
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
