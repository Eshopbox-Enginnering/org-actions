## Git
- New task: switch to `main`, pull latest, create `<type>/<issue-no>` where `<type>` is `bug` if the issue type is bug, otherwise `feature`. If no `main` and no base given, ask.
- Continue on an existing branch only if the user says so.
- When done (tests green), push and open a PR to `main` with `gh pr create --base main`:
  - Title: `<type> to main: <short summary> (#<issue-no>)`. `<type>` must match the branch prefix exactly.
  - Body: link to the issue (`https://github.com/Eshopbox-Enginnering/Central-Backlog/issues/<issue-no>`) and the test case → test → red/green table.
- Never open PRs to `staging`; the staging deploy happens automatically after checks pass.
- Never merge or rebase `staging` into a feature branch.
- Resolving conflicts on `conflict/resolved/<issue-no>`: take the feature branch for code this PR intentionally changes; keep unrelated staging changes; if both changed the same logic, combine them or ask. Existing tests that contradict the issue's acceptance criteria may be updated to match, with @DisplayName citing the issue.

## Starting a task
- Issues live in `Eshopbox-Enginnering/Central-Backlog`. Given an issue number or link, read it with `gh issue view <no> -R Eshopbox-Enginnering/Central-Backlog --comments`.
- Read issues only via `gh`, never the browser.
- If a GitHub request returns 401 or "not logged in", check the saved login with `env -u GH_TOKEN -u GITHUB_TOKEN gh auth status` and retry the failed command using that login, with `GH_TOKEN` and `GITHUB_TOKEN` unset.
- If the retry works, continue without asking the user to log in. Use the saved login for Git operations too. Never print tokens.
- Ask the user to run `gh auth login` only if the saved login also fails authentication. An occasional 401 must not automatically trigger a login request. A 404 alone does not prove an authentication failure; verify the repository, issue number, and access first.

## Reading the issue
- Test cases come from the issue's "Test cases" section. If there isn't one, derive them from the acceptance criteria.
- If requirements are ambiguous or a needed decision is missing, ask. Don't guess.

## Test-first workflow (features and bug fixes)
**Test cases come first.**
- Use the test cases exactly as written. Don't drop or change any; suggest additions separately.
- If the issue has neither test cases nor acceptance criteria, propose a list (behaviour + expected result) and wait for approval. Skip only if the user says "no plan" or the change is trivial.
- For a bug, the first test case must reproduce the bug.

**Then:**
1. **Red:** Write the tests and confirm they fail for the right reason. If a test already passes before any change, that behaviour exists: report it and don't change code for it.
2. **Green:** Write the minimum code to pass.
3. **Verify:** Run the full test suite and the build.
4. **Refactor** only while green.

**Rules:**
- Never edit, weaken, delete or @Disable existing tests to make them pass. If a test seems wrong, ask.
- If tests can't run, say so. Never claim "done" without running them.
- In the summary, map each test case to its test and show red → green (or "already passing").

## API verification
- API test requests live in `api-tests/<feature>.md` (curl + expected response). Fixture files go in `api-tests/fixtures/`.
- When you change an endpoint's behaviour, add or update its curls there and include them in the PR description so QA can run them on staging.
- Use placeholders only (`$BASE_URL`, `$TOKEN`, `$WORKSPACE`). Never include real tokens, cookies, production URLs or customer data.

## Review comments
- When asked to address PR review comments, read them with `gh pr view <no> --comments` and the review threads.
- To judge a comment, write a test reproducing its scenario and run it. A failing test means valid only if the expected behaviour is supported by the issue's test cases or acceptance criteria. If the spec contradicts it → not valid, cite the spec. If the spec is silent → escalate.
- Valid → fix test-first, push, reply "Fixed in <commit>: <test name> red → green". Resolve the thread yourself, except for P1/security fixes: leave those open for the developer to confirm.
- Not valid → reply with the reason and the evidence (spec line or passing test), leave the thread open, prefixed "⚠️ Needs human decision:".
- Ask before changing code only if the fix depends on a business rule, the spec is silent, or there's no test that can prove the fix. Reply "⚠️ Needs human decision: <question>" and leave the thread open.
- If the human confirms the current behaviour is intentional, add a test whose @DisplayName states the business rule and a code comment linking the issue.
- After replying to every comment:
  - If any reply is "⚠️ Needs human decision", don't trigger a review. Tell the user which threads need them; they'll decide and comment "@codex review" themselves.
  - Otherwise, comment "@codex review" on the PR. After 3 review rounds with new blocking findings, stop and ask the user.

## Learned rules
<!-- "Don't X; do Y instead." One line per real agent mistake. -->
