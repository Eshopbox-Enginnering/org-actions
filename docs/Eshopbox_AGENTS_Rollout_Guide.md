Organization-wide AGENTS.md Rollout
GitHub Actions + GitHub App implementation guide
Eshopbox Engineering

# 1. Final workflow
# 2. org-action repository structure
org-action/
|-- .github/
|   `-- workflows/
|       `-- update-agents.yml
|-- payload/
|   `-- agents-update.md
`-- scripts/
`-- update-agents.sh

# 3. One-time GitHub configuration
## 3.1 GitHub App
- Create a GitHub App owned by the Eshopbox Engineering organization.
- Repository permissions: Contents - Read and write; Pull requests - Read and write; Metadata - Read-only.
- Install the App on all repositories if future repositories should be discovered automatically.
- Generate a private key and keep the .pem file private. Do not commit it to GitHub.
## 3.2 org-action Actions variable and secret
## 3.3 Approval environment
- Create an environment named agents-org-production in org-action.
- Enable Required reviewers and add only the organization owners who may authorize a real rollout.
- Leave Prevent self-review unchecked if the same authorized owner may trigger and approve the workflow.
- Disable administrator bypass if you want the approval gate to apply even to administrators.
## 3.4 Protected branch / ruleset
- Keep the existing main/default branch protection in place.
- Add the GitHub App to the ruleset bypass list.
- Use bypass mode: For pull requests only.
- Do not grant the App general direct-push bypass to the protected default branch.
# 4. payload/agents-update.md
This file contains only the guidance that should be appended. Change this file whenever you want to roll out a new guidance block.
# Agent repository guidance

## Cloud SQL standards

Apply these checks whenever you write **or review** code that touches the database.

### Query optimization

- Verify indexes for important WHERE, JOIN, and ORDER BY conditions.
- Avoid N+1 database queries.
- Avoid SELECT * where only specific columns are required.
- Avoid unbounded queries against growing tables.

### Session / connection hygiene

- Explicitly opened sessions or connections must be closed on success and failure paths.
- Use try/finally, try-with-resources, or the framework equivalent.
- Do not flag framework-managed connections where lifecycle management is automatic.

# 5. .github/workflows/update-agents.yml
name: Update AGENTS.md

on:
workflow_dispatch:
inputs:
operation:
description: "Operation"
required: true
type: choice
options:
- DRY_RUN
- APPLY

repo_type:
description: "Repository type"
required: true
type: choice
options:
- backend
- frontend
- all

dry_run_repo:
description: "Single repo for DRY_RUN"
required: false
type: string

confirmation:
description: "For APPLY only, enter exactly: APPLY"
required: false
type: string

content_file:
description: "Content file to append to AGENTS.md"
required: true
default: "payload/agents-update.md"
type: string

concurrency:
group: agents-org-update
cancel-in-progress: false

jobs:
validate:
name: Validate request
runs-on: ubuntu-latest
steps:
- name: Checkout org-action
uses: actions/checkout@v4

- name: Validate inputs
shell: bash
run: |
set -euo pipefail
OPERATION="${{ inputs.operation }}"
DRY_RUN_REPO="${{ inputs.dry_run_repo }}"
CONFIRMATION="${{ inputs.confirmation }}"
CONTENT_FILE="${{ inputs.content_file }}"

test -f "$CONTENT_FILE" || { echo "ERROR: Content file missing"; exit 1; }
test -s "$CONTENT_FILE" || { echo "ERROR: Content file empty"; exit 1; }

if [ "$OPERATION" = "DRY_RUN" ] && [ -z "$DRY_RUN_REPO" ]; then
echo "ERROR: dry_run_repo is required for DRY_RUN"
exit 1
fi

if [ "$OPERATION" = "APPLY" ] && [ "$CONFIRMATION" != "APPLY" ]; then
echo "ERROR: confirmation must be exactly APPLY"
exit 1
fi

dry-run:
name: Dry run
needs: validate
if: inputs.operation == 'DRY_RUN'
runs-on: ubuntu-latest
permissions:
contents: read
steps:
- uses: actions/checkout@v4

- name: Create GitHub App token
id: app-token
uses: actions/create-github-app-token@v2
with:
app-id: ${{ vars.ORG_AGENTS_APP_ID }}
private-key: ${{ secrets.ORG_AGENTS_APP_PRIVATE_KEY }}
owner: ${{ github.repository_owner }}

- name: Run dry run
env:
GH_TOKEN: ${{ steps.app-token.outputs.token }}
ORG: ${{ github.repository_owner }}
OPERATION: DRY_RUN
REPO_TYPE: ${{ inputs.repo_type }}
DRY_RUN_REPO: ${{ inputs.dry_run_repo }}
CONTENT_FILE: ${{ inputs.content_file }}
BATCH_ID: ${{ github.run_id }}
run: |
chmod +x scripts/update-agents.sh
scripts/update-agents.sh

apply:
name: Apply to repositories
needs: validate
if: inputs.operation == 'APPLY'
runs-on: ubuntu-latest
environment:
name: agents-org-production
permissions:
contents: read
steps:
- uses: actions/checkout@v4

- name: Create GitHub App token
id: app-token
uses: actions/create-github-app-token@v2
with:
app-id: ${{ vars.ORG_AGENTS_APP_ID }}
private-key: ${{ secrets.ORG_AGENTS_APP_PRIVATE_KEY }}
owner: ${{ github.repository_owner }}

- name: Apply AGENTS.md update
env:
GH_TOKEN: ${{ steps.app-token.outputs.token }}
ORG: ${{ github.repository_owner }}
OPERATION: APPLY
REPO_TYPE: ${{ inputs.repo_type }}
CONTENT_FILE: ${{ inputs.content_file }}
BATCH_ID: ${{ github.run_id }}
run: |
chmod +x scripts/update-agents.sh
scripts/update-agents.sh

# 6. scripts/update-agents.sh
#!/usr/bin/env bash
set -euo pipefail

ORG="${ORG:?ORG is required}"
OPERATION="${OPERATION:?OPERATION is required}"
REPO_TYPE="${REPO_TYPE:?REPO_TYPE is required}"
CONTENT_FILE="${CONTENT_FILE:?CONTENT_FILE is required}"
BATCH_ID="${BATCH_ID:?BATCH_ID is required}"
DRY_RUN_REPO="${DRY_RUN_REPO:-}"
CONTENT_PATH="$GITHUB_WORKSPACE/$CONTENT_FILE"

CONTENT_HASH=$(sha256sum "$CONTENT_PATH" | awk '{print substr($1,1,12)}')
BEGIN_MARKER="<!-- BEGIN ESHOPBOX-AGENTS-UPDATE:$CONTENT_HASH -->"
END_MARKER="<!-- END ESHOPBOX-AGENTS-UPDATE:$CONTENT_HASH -->"

detect_repo_type() {
local dir="$1"

if [ -f "$dir/angular.json" ] || [ -f "$dir/vite.config.js" ] ||      [ -f "$dir/vite.config.ts" ] || [ -f "$dir/vite.config.mjs" ] ||      [ -f "$dir/vue.config.js" ] || [ -f "$dir/next.config.js" ] ||      [ -f "$dir/next.config.mjs" ] || [ -f "$dir/next.config.ts" ]; then
echo "frontend"; return
fi

if [ -f "$dir/pom.xml" ] || [ -f "$dir/build.gradle" ] ||      [ -f "$dir/build.gradle.kts" ] || [ -f "$dir/go.mod" ] ||      [ -f "$dir/requirements.txt" ] || [ -f "$dir/pyproject.toml" ] ||      [ -f "$dir/composer.json" ]; then
echo "backend"; return
fi

if [ -f "$dir/package.json" ]; then
if grep -Eq '"(express|fastify|koa|nestjs|@nestjs/core)"' "$dir/package.json"; then
echo "backend"; return
fi
if grep -Eq '"(react|react-dom|@angular/core|vue|next|vite)"' "$dir/package.json"; then
echo "frontend"; return
fi
fi

echo "unknown"
}

matches_selected_type() {
local detected="$1"
if [ "$REPO_TYPE" = "all" ]; then
[ "$detected" = "backend" ] || [ "$detected" = "frontend" ]
return
fi
[ "$detected" = "$REPO_TYPE" ]
}

process_repo() {
local repo="$1"
echo "Repository: $ORG/$repo"

repo_info=$(gh repo view "$ORG/$repo"     --json isArchived,isFork,defaultBranchRef     --jq '[.isArchived,.isFork,.defaultBranchRef.name] | @tsv') || return 0

IFS=$'\t' read -r archived fork default_branch <<< "$repo_info"
[ "$archived" = "true" ] && { echo "SKIP: archived"; return 0; }
[ "$fork" = "true" ] && { echo "SKIP: fork"; return 0; }
[ -z "$default_branch" ] && { echo "SKIP: no default branch"; return 0; }

workdir=$(mktemp -d)
gh repo clone "$ORG/$repo" "$workdir" -- --depth=1 --branch "$default_branch" >/dev/null 2>&1 || {
echo "SKIP: clone failed"; rm -rf "$workdir"; return 0;
}

detected_type=$(detect_repo_type "$workdir")
echo "Detected type: $detected_type"
matches_selected_type "$detected_type" || { rm -rf "$workdir"; return 0; }

cd "$workdir"
if [ -f AGENTS.md ] && grep -Fq "$BEGIN_MARKER" AGENTS.md; then
echo "SKIP: exact content already exists"
cd "$GITHUB_WORKSPACE"; rm -rf "$workdir"; return 0
fi

if [ -f AGENTS.md ]; then
printf '\n\n%s\n\n' "$BEGIN_MARKER" >> AGENTS.md
cat "$CONTENT_PATH" >> AGENTS.md
printf '\n\n%s\n' "$END_MARKER" >> AGENTS.md
else
printf '%s\n\n' "$BEGIN_MARKER" > AGENTS.md
cat "$CONTENT_PATH" >> AGENTS.md
printf '\n\n%s\n' "$END_MARKER" >> AGENTS.md
fi

mapfile -t changed_files < <(git status --porcelain | sed 's/^...//')
if [ "${#changed_files[@]}" -ne 1 ] || [ "${changed_files[0]}" != "AGENTS.md" ]; then
echo "ERROR: unexpected files changed"; git status --short; return 1
fi

if [ "$OPERATION" = "DRY_RUN" ]; then
echo "DRY RUN ONLY - no branch, commit, push or PR"
git diff -- AGENTS.md
cd "$GITHUB_WORKSPACE"; rm -rf "$workdir"; return 0
fi

branch="org-action/agents-${BATCH_ID}"
git config user.name "eshopbox-org-agents[bot]"
git config user.email "eshopbox-org-agents[bot]@users.noreply.github.com"
git checkout -b "$branch"
git add AGENTS.md
git commit -m "chore: update AGENTS.md"
git push origin "$branch"

pr_url=$(gh pr create --repo "$ORG/$repo" --base "$default_branch" --head "$branch"     --title "Update AGENTS.md"     --body "Organization-level automated AGENTS.md update. Batch: $BATCH_ID. Content ID: $CONTENT_HASH. Only AGENTS.md is modified.")

gh pr merge "$pr_url" --squash --delete-branch
echo "SUCCESS: $ORG/$repo updated and merged"
cd "$GITHUB_WORKSPACE"; rm -rf "$workdir"
}

if [ "$OPERATION" = "DRY_RUN" ]; then
[ -n "$DRY_RUN_REPO" ] || { echo "ERROR: dry_run_repo is required"; exit 1; }
process_repo "$DRY_RUN_REPO"
exit 0
fi

[ "$OPERATION" = "APPLY" ] || { echo "ERROR: invalid operation"; exit 1; }

repos=$(gh repo list "$ORG" --limit 1000 --json name,isArchived,isFork   --jq '.[] | select(.isArchived == false) | select(.isFork == false) | .name')

for repo in $repos; do
process_repo "$repo"
done

# 7. How to run it
## 7.1 First run - DRY_RUN
- Open org-action -> Actions -> Update AGENTS.md -> Run workflow.
- Operation: DRY_RUN.
- Repository type: backend (or frontend).
- Single repo: enter one known repository, for example esb-unicommerce-inventory-integration.
- Confirmation: leave blank.
- Content file: payload/agents-update.md.
Expected result: the workflow validates successfully, the Dry run job succeeds, and Apply to repositories is skipped. The logs must show the proposed AGENTS.md diff and must state that no branch, commit, push, or PR is created.
## 7.2 Real rollout - APPLY
- Run the workflow again.
- Operation: APPLY.
- Repository type: backend, frontend, or all.
- Single repo: leave empty.
- Confirmation: type exactly APPLY.
- The apply job pauses at the agents-org-production environment approval gate.
- An authorized owner approves the job.
- The script scans all active non-fork organization repositories, classifies them, and updates only repositories matching the selected type.
# 8. Repository type detection
Important: Selecting all means backend + frontend only. Repositories detected as unknown are skipped to avoid modifying infrastructure, documentation, or unrelated repositories.
# 9. Safety controls
- The exact payload is wrapped in a content-hash marker. Re-running the same payload does not append it twice.
- Existing AGENTS.md content is never replaced.
- If AGENTS.md does not exist, it is created.
- The script verifies that AGENTS.md is the only changed file before creating a commit.
- Archived repositories and forks are skipped.
- DRY_RUN makes no GitHub changes.
- APPLY requires the explicit text APPLY plus environment approval.
- The GitHub App is intended to bypass only through pull requests, not by direct push to protected main.
- Concurrency prevents two organization-wide AGENTS rollouts from running at the same time.
# 10. Post-rollout verification checklist
☐ Open 2-3 merged PRs from different backend technologies.
☐ Confirm the old AGENTS.md content is still present.
☐ Confirm the new block was appended exactly once.
☐ Confirm only AGENTS.md changed in each PR.
☐ Confirm the PR was squash-merged and the rollout branch was deleted.
☐ Check the workflow summary for any failed repositories.
Recommended enhancement: Add a PREVIEW_ALL operation before APPLY to list every repository that will be classified as backend/frontend without making changes. This is useful before the first large rollout or after changing the detection rules.

| Goal Allow a limited set of organization owners to centrally append guidance to AGENTS.md across active backend, frontend, or all application repositories. Existing AGENTS.md content is preserved; missing files are created. The workflow uses a one-repository dry run first, then creates and auto-merges pull requests while keeping the default branch protected. |
| --- |

| 1 | Owner manually runs workflow in org-action |
| --- | --- |
| 2 | Choose DRY_RUN or APPLY and backend / frontend / all |
| 3 | DRY_RUN processes exactly one repository and prints the proposed diff |
| 4 | APPLY pauses at the agents-org-production environment approval gate |
| 5 | Workflow discovers active repositories dynamically |
| 6 | Script detects repository type from files such as pom.xml, angular.json, etc. |
| 7 | AGENTS.md is created or appended, then a branch and pull request are created |
| 8 | GitHub App merges the PR through PR-only ruleset bypass; protected main remains protected |

| Type | Name | Value |
| --- | --- | --- |
| Variable | ORG_AGENTS_APP_ID | GitHub App App ID |
| Secret | ORG_AGENTS_APP_PRIVATE_KEY | Complete contents of the generated .pem private key |

| Detected type | Strong indicators | Notes |
| --- | --- | --- |
| Frontend | angular.json, vite.config.*, vue.config.js, next.config.* | Checked before backend indicators. |
| Backend | pom.xml, build.gradle*, go.mod, requirements.txt, pyproject.toml, composer.json | Covers common Java, Go, Python, PHP backends. |
| Node fallback | package.json dependency inspection | Express/Fastify/Koa/NestJS => backend; React/Angular/Vue/Next/Vite => frontend. |
