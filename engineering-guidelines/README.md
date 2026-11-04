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
- `phase`: choose `staging` for a normal APPLY run; it creates and merges the staging PR first, then creates and merges the production PR. Manual `production` phase remains available for older rollouts.
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

## Automated staging and production rollout

**One manual APPLY run creates and merges both PRs**, in order: staging first, then production. The rollout uses the same administrative merge capability as the existing `update-agents.sh` bulk updater. This **can bypass normal PR approvals and required checks**; only run it with the authorized GitHub App and for trusted canonical guideline files. The script independently checks branch freshness, exact changed-file scope, and canonical document bytes before each merge. Any mismatch or GitHub merge error stops processing that repository and fails the workflow.

- `DRY_RUN` previews changes without creating PRs or merging.
- `APPLY` with `phase=staging` performs the two merges sequentially. A single-repository target follows this sequence too.
- Missing or outdated production documents are reconciled only after staging has merged successfully.
- If both branches already contain the selected guidelines, the run reports a successful no-op.
- A failed merge, stale branch, unexpected file change, or unsupported repository type is reported rather than silently merged.
- Backend build metadata (including PHP `composer.json`) takes precedence over frontend tooling such as Vite for mixed repositories.
- The separate [Promote Merged Engineering Guidelines](../.github/workflows/promote-engineering-guidelines.yml) workflow exists for legacy/recovery flows; a successful automated rollout needs no scheduled promotion.
- Avoid disabling organization-wide checks: normal developer PRs continue to be validated.

## Safety and workflow limits

- Backend/frontend detection follows the existing AGENTS updater's build-file heuristics. Unknown repositories are skipped; use `single` for an explicitly named repo with a recognizable build file.
- Under APPLY staging, the workflow creates and administratively merges its validated guideline PRs. These administrative merges can bypass normal review, CI, branch protection, and CODEOWNER checks. Never use them for unrelated code changes.
- Pre-existing rollout branches without an identifiable PR are blocked rather than overwritten. Production requires the canonical content to be present in staging; the automation merges staging before creating the independently verified production PR. **Preserve the rollout branch** through production.
- Only production/default branches `main`, `master`, or `aws-velocis-main` and a corresponding staging branch are supported.
- Checked-in source files are read from this workflow repository. Optional one-off GitHub attachments are fetched at run time with HTTPS and 1 MiB UTF-8 limits. Inaccessible sources fail before repository writes.
- Try `target=single,operation=DRY_RUN` first, then `APPLY,phase=staging` on one repository before organization-wide deployment.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_sync_engineering_guidelines.py' -v
python3 -m py_compile scripts/sync-engineering-guidelines.py
```
