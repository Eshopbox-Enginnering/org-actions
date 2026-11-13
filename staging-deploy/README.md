# Organization-wide staging promotion rollout (#7319)

This feature uses the same **DRY_RUN → explicit APPLY** approach as [Sync Engineering Guidelines](../.github/workflows/sync-engineering-guidelines.yml), with a separate workflow: [Roll out staging promotion automation](../.github/workflows/rollout-staging-promotion.yml).

## Backend-only selection

Choose `target=backend` in the rollout workflow. It inventories unarchived repositories, identifies backend projects using the same root-file signals as the engineering-guidelines sync (Maven, Gradle, Go, Python, PHP, or Node backend dependencies), and **skips frontend and unclassified repositories**. The same backend eligibility guard applies to `single`, `selected`, and `all` targets, so an explicitly selected frontend repo is not installed by mistake.

This installer does **not** read, create, or modify `AGENTS.md`. The deployed artifacts are limited to `.github/workflows/auto-staging-deploy.yml` and four `.github/scripts/auto-staging-*.cjs` files. Updating `AGENTS.md` is a separate optional engineering-guidelines process, not a prerequisite for staging promotion.

## Scope and safety

- `DRY_RUN` inventories repositories with a `main` default branch, a `staging` branch and `.github/workflows/cicd.yml` supporting `workflow_dispatch` and `dev-*` tags.
- `APPLY` requires confirmation `APPLY`. It opens **main PRs for review only**; it never merges PRs, tags commits, triggers staging or changes the staging branch. Staging companions must follow review and staging-first process.
- Only names explicitly approved in [approved-repositories.json](./approved-repositories.json) are eligible for APPLY. Other repositories are shown as `AUDIT_ONLY` and skipped.
- Candidate approval is managed through the manifest; it does not indicate that rollout PRs were merged or deployments are enabled. The audit identified 21 backend candidates in run 43409005535; their approval is tracked separately in PRs #312 (staging) and #311 (main).
- GitHub App installation must grant **Workflows: write** along with Contents and Pull requests; the generated workflow explicitly requests this permission to support workflow-file PR merges.
- Required-workflow identities are resolved from each repository's **active GitHub branch rules**, including configured source repository, workflow path, ref/SHA and live source workflow ID. Unknown or mismatched identities fail closed. Workflow IDs from the original pilot are no longer hardcoded.
- Active organization rulesets were inspected across all 21 basic candidates. All have the core staging-presence, protected-branch, test-coverage and title/ticket rules. `esb-client-order-return` and `esbeventpipeline` additionally have large-PR/WIP restrictions. These protections remain in force.
- These checks do not replace a functional smoke test on each repository. Verify the generated staging/main installation PRs, CI/CD dispatch, GitHub App permission, runner and tag handling before broader use.
- No automatic staging merge or deployment is enabled by this installer itself. The `/deploy-staging` command remains the trigger for installed workflow, until separate no-command readiness automation is designed and reviewed.

## Steps

1. Merge the org-actions rollout PR after tests/review; this installs only central rollout tooling, not destination repository changes.
2. Run `operation=DRY_RUN`, `target=backend` to get the inventory and identify repo-specific differences. Check every skip/block reason.
3. Confirm the approved-manifest PRs and dynamic required-workflow validation PRs are merged before running APPLY; check actual repository rules and smoke tests on the generated installation PRs.
4. Run `operation=DRY_RUN`, `target=selected` for a small approved pilot batch.
5. Run `operation=APPLY`, `target=selected`, `confirmation=APPLY` to create destination **main PRs only**.
6. Review destination PRs, create separate staging companion PRs, merge staging first, then main, and validate `/deploy-staging` on a safe smoke PR.
7. Expand to further batches only after the pilot is successful.

This feature intentionally does **not** reuse the guideline uploader's `--admin` merge behavior. Deployment automation must respect the target repository's rules.
