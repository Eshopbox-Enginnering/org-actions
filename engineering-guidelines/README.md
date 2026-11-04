# Engineering guideline synchronization

This workflow distributes Markdown engineering standards to supported Eshopbox repositories without editing AGENTS.md or unrelated files.

## Central catalog

`engineering-guidelines/catalog.json` has separate `backend` and `frontend` arrays. Each entry is:

```json
{
  "id": "cache",
  "source_url": "engineering-guidelines/backend/eshopbox-cache-usage-guidelines.md",
  "destination": "docs/engineering-guidelines/eshopbox-cache-usage-guidelines.md"
}
```

The backend source documents are now committed under `engineering-guidelines/backend/`, and the catalog refers to those checked-in files. The initial backend catalog contains the five files from [Central Backlog #7218](https://github.com/Eshopbox-Enginnering/Central-Backlog/issues/7218#issuecomment-6575357434). The frontend catalog is initially empty: add future frontend-only entries there, or use the one-off input described below. You may use either a GitHub issue attachment URL or a checked-in file under `engineering-guidelines/` as the `source_url`. Prefer checked-in sources for long-term stability and reviewable source updates.

## Run workflow

Run [Sync Engineering Guidelines](../.github/workflows/sync-engineering-guidelines.yml) via Actions > Run workflow.

- `operation`: `DRY_RUN` (shows hashes and diff; no commits/PRs), or `APPLY` with confirmation `APPLY`
- `target`: `single` (with `repository` name), `backend`, `frontend`, or `all`
- `phase`: `staging` first. Merge staging PR manually; the scheduled promotion workflow then creates a separate clean production PR. The manual production phase remains available for older rollouts.
- `file_id`: `all` or an `id` from the catalog, such as `cache` or `429`
- `source_url` + `destination` + `source_scope`: optional **one-off** file replacing the catalog selection, e.g., `source_scope=frontend`, source `engineering-guidelines/frontend-guide.md`, destination `docs/engineering-guidelines/frontend-guide.md`
- `central_backlog_issue`: referenced in every PR

**File sync behavior:** missing destination = create; differing bytes = replace the entire file; identical bytes = skip. Only exact selected paths in `docs/engineering-guidelines/` are managed; nothing is deleted automatically.

## Make AI coding tools follow these guidelines

The `docs/engineering-guidelines/` directory is the destination for reference documents, **not an automatically discovered AI rules directory**. Copying Markdown alone does not guarantee that agents consult it.

1. Add the content of [ai-agent-guidance.md](ai-agent-guidance.md) to each repository's **existing root `AGENTS.md`**, using a clearly marked organization-managed section. Preserve repository-specific instructions; do not replace the entire file. Codex and compatible agents then discover the guidance when working in that repository.
2. For GitHub Copilot, optionally create `.github/instructions/engineering-guidelines.instructions.md` with `applyTo: "**/*"` and an instruction to consult the applicable documents under `docs/engineering-guidelines/`. This file is distinct from the guidelines themselves.
3. If a repository uses Claude Code or Cursor, keep its recognized `CLAUDE.md` or `.cursor/rules/` entry point in sync with a short reference. Do not assume `AGENTS.md` is picked up identically by every tool.

**Important:** The new Sync Engineering Guidelines workflow deliberately does not edit `AGENTS.md` or tool-specific instruction files. To activate AI discovery across all repos, perform a separate, reviewed rollout of the reference text using the existing AGENTS updater; do not silently modify these files during document sync. Agents may still fail to follow instructions, so test with a representative code review/change that requires the 401 or caching guideline.

## Automatic production PR after staging merge

**One manual APPLY run is sufficient.** Run the Sync Engineering Guidelines workflow with `operation=APPLY` and `phase=staging`. Review and merge the staging PR normally. The central [Promote Merged Engineering Guidelines](../.github/workflows/promote-engineering-guidelines.yml) workflow then scans recently merged guideline staging PRs every 15 minutes, validates their machine-readable source metadata, and creates the matching production PR automatically.

- No separate production workflow trigger is required for newly generated staging PRs.
- Production PRs are built independently from the production branch and contain **only selected guideline files**, not unrelated staging commits.
- The GitHub App must retain cross-repository pull request and contents permissions.
- Both staging and production PRs still require manual review and merge; no auto-merge is performed.
- Scheduled GitHub Actions can run later than the 15-minute interval. Use the promotion workflow's manual **Run workflow** button to reconcile sooner if needed.
- Historical staging PRs created before this version have no metadata marker and **will not** be automatically promoted. Use the original manual production phase for those.
- A failed validation blocks the production PR and reports the affected repository; it never bypasses safety checks.
- Check existing org-level main branch rules (including commit-in-staging checks) before merging production PRs; a clean production-based docs commit may need an approved policy exception if your rules require the exact SHA to appear in staging.

## Bulk rollout and PR checks

The `Sync Engineering Guidelines` workflow now invokes the dual-PR orchestrator. With `APPLY` and `phase=staging`, a single run opens the staging and main PRs for review, but **does not merge either PR**. Merge staging first, followed by main.

Application-level PR checks remain enabled. The centralized lightweight checks (title/ticket, test-file validation, staging commit validation, and source-branch verification) use GitHub-hosted runners; Java coverage reserves `checks-runners` for a PR with relevant Java or build changes. A guideline production PR gets a narrowly scoped alternative to SHA-in-staging verification **only when every changed file is under `docs/engineering-guidelines/` and its bytes exactly match staging**. It is otherwise checked normally.

When the main PR is created before the staging PR has merged, its staging-content verification may initially fail. After merging staging, rerun that check on the main PR before merging main. Do not temporarily disable organization rulesets for bulk rollout. Existing branch approvals and review requirements continue to apply.

## Safety and workflow limits

- Backend/frontend detection follows the existing AGENTS updater's build-file heuristics. Unknown repositories are skipped; use `single` for an explicitly named repo with a recognizable build file.
- Workflow creates PRs but never merges them. It keeps all normal review, CI, branch protection, and CODEOWNER checks.
- Pre-existing rollout branches without an identifiable PR are blocked rather than overwritten. Production requires a matching merged staging PR, verifies canonical content on staging, and creates a clean production-based branch with only selected document edits. **Preserve the rollout branch** through production.
- Only production/default branches `main`, `master`, or `aws-velocis-main` and a corresponding staging branch are supported.
- Checked-in source files are read from this workflow repository. Optional one-off GitHub attachments are fetched at run time with HTTPS and 1 MiB UTF-8 limits. Inaccessible sources fail before repository writes.
- Try `target=single,operation=DRY_RUN` first, then `APPLY,phase=staging` before organization-wide deployment.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_sync_engineering_guidelines.py' -v
python3 -m py_compile scripts/sync-engineering-guidelines.py
```
