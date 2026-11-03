# Engineering guideline synchronization

This workflow distributes Markdown engineering standards to supported Eshopbox repositories without editing AGENTS.md or unrelated files.

## Central catalog

`engineering-guidelines/catalog.json` has separate `backend` and `frontend` arrays. Each entry is:

```json
{
  "id": "cache",
  "source_url": "https://github.com/user-attachments/files/34269757/eshopbox-cache-usage-guidelines.md",
  "destination": "docs/engineering-guidelines/eshopbox-cache-usage-guidelines.md"
}
```

The initial backend catalog contains the five files from [Central Backlog #7218](https://github.com/Eshopbox-Enginnering/Central-Backlog/issues/7218#issuecomment-6575357434). The frontend catalog is initially empty: add future frontend-only entries there, or use the one-off input described below. You may use either a GitHub issue attachment URL or a checked-in file under `engineering-guidelines/` as the `source_url`. Prefer checked-in sources for long-term stability and reviewable source updates.

## Run workflow

Run [Sync Engineering Guidelines](../.github/workflows/sync-engineering-guidelines.yml) via Actions > Run workflow.

- `operation`: `DRY_RUN` (shows hashes and diff; no commits/PRs), or `APPLY` with confirmation `APPLY`
- `target`: `single` (with `repository` name), `backend`, `frontend`, or `all`
- `phase`: `staging` first. Merge staging PR manually; then choose `production` to create the production PR using the **same branch**. Do not delete the rollout branch when merging staging.
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

## Safety and workflow limits

- Backend/frontend detection follows the existing AGENTS updater's build-file heuristics. Unknown repositories are skipped; use `single` for an explicitly named repo with a recognizable build file.
- Workflow creates PRs but never merges them. It keeps all normal review, CI, branch protection, and CODEOWNER checks.
- Pre-existing rollout branches without an identifiable PR are blocked rather than overwritten. Production requires the matching merged staging PR and the original rollout branch. **Preserve the rollout branch** through production.
- Only production/default branches `main`, `master`, or `aws-velocis-main` and a corresponding staging branch are supported.
- Source attachments are fetched at run time, limited to approved GitHub attachment origins and 1 MiB UTF-8 text. An inaccessible source fails safely before repository writes.
- Try `target=single,operation=DRY_RUN` first, then `APPLY,phase=staging` before organization-wide deployment.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_sync_engineering_guidelines.py' -v
python3 -m py_compile scripts/sync-engineering-guidelines.py
```
