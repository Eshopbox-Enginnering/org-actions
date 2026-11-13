# Backend staging promotion rollout (#7319)

Run [Roll out staging promotion automation](../.github/workflows/rollout-staging-promotion.yml) from org-actions on main.

## Scope

- Only detected backend repositories with main, staging, and compatible cicd.yml are eligible. Frontend and unknown repositories are always skipped.
- No approved-repositories.json and no AGENTS.md changes.
- The workflow installs exactly one atomic change per repository: `.github/workflows/auto-staging-deploy.yml` and the four `.github/scripts/auto-staging-*.cjs` files.
- `DRY_RUN` performs an audit without creating PRs or merging.
- `APPLY` requires confirmation `APPLY`. For each eligible repository it creates **one feature branch from main**, opens **one staging PR** and merges it through standard GitHub protections, then opens **one main PR using the exact same feature commit** and merges normally. **At most one open rollout PR in each repository at a time.** No application is deployed by the installer.
- Never use `--admin`, disable required checks, alter organization rulesets, or force-update branches. Any check failure blocks that repository and is reported.
- `org-action/staging-promotion-...` branches use the established org-action naming family, but are **not** in the existing `org-action/agents-` / `org-action/guidelines-` skip exceptions. Required checks still run; the exact feature commit is already in staging when main validation executes, so the staging-presence rule can pass without bypass.
- The rollout checks each PR's head SHA at merge; a changed head fails closed.
- Application rollout is still triggered only by a later explicit `/deploy-staging` command on an eligible application PR.

## Runbook

1. Run `DRY_RUN` with `target=backend`; review eligible and blocked repositories.
2. Run `APPLY` with `target=backend` and `confirmation=APPLY` **only after approval**.
3. Each backend uses one feature commit and is processed staging-first, then main; if staging merge cannot pass standard checks, no main PR is created for that repository.
4. Review the run logs and any blocked repository. Fix blockers in a reviewed org-actions change; do not bypass protections.
5. Validate the existing `/deploy-staging` command in a backend repository before operational use.

The source of truth for required-workflow validation is the active GitHub ruleset/source workflow identity; no pilot-specific IDs are copied into the current shared scripts.
