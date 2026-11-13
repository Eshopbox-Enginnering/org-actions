# Organization-wide staging promotion rollout (#7319)

This feature uses the same **DRY_RUN → explicit APPLY** approach as [Sync Engineering Guidelines](../.github/workflows/sync-engineering-guidelines.yml), with a separate workflow: [Roll out staging promotion automation](../.github/workflows/rollout-staging-promotion.yml).

## Scope and safety

- `DRY_RUN` inventories repositories with a `main` default branch, a `staging` branch and `.github/workflows/cicd.yml` supporting `workflow_dispatch` and `dev-*` tags.
- `APPLY` requires confirmation `APPLY`. It opens **main PRs for review only**; it never merges PRs, tags commits, triggers staging or changes the staging branch. Staging companions must follow review and staging-first process.
- Only names explicitly approved in [approved-repositories.json](./approved-repositories.json) are eligible for APPLY. Other repositories are shown as `AUDIT_ONLY` and skipped.
- The initial approved entry is the existing source pilot `esb-product-engine-jobs`; it should be a no-op if its canonical automation files are already in place.
- Copied source includes repository-specific required-workflow identity mappings. **Do not approve another repository until its required workflows, rule source identities, branch protections, GitHub App permissions, deployment workflow, tag format and integration tests are validated, and the script is parameterized for that repository.** In particular, never copy the pilot's hardcoded rule IDs to an unreviewed repository.
- No automatic staging merge or deployment is enabled by this installer itself. The `/deploy-staging` command remains the trigger for installed workflow, until separate no-command readiness automation is designed and reviewed.

## Steps

1. Merge the org-actions rollout PR after tests/review; this installs only central rollout tooling, not destination repository changes.
2. Run `operation=DRY_RUN`, `target=all` to get the inventory and identify repo-specific differences. Check every skip/block reason.
3. Review eligibility and refactor required-check trust configuration for each repository before adding it to the allowlist.
4. Run `operation=DRY_RUN`, `target=selected` for a small approved pilot batch.
5. Run `operation=APPLY`, `target=selected`, `confirmation=APPLY` to create destination **main PRs only**.
6. Review destination PRs, create separate staging companion PRs, merge staging first, then main, and validate `/deploy-staging` on a safe smoke PR.
7. Expand to further batches only after the pilot is successful.

This feature intentionally does **not** reuse the guideline uploader's `--admin` merge behavior. Deployment automation must respect the target repository's rules.
