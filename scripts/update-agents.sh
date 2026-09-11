#!/usr/bin/env bash

set -euo pipefail

ORG="${ORG:?ORG is required}"
OPERATION="${OPERATION:?OPERATION is required}"
REPO_TYPE="${REPO_TYPE:?REPO_TYPE is required}"
CONTENT_FILE="${CONTENT_FILE:?CONTENT_FILE is required}"
BATCH_ID="${BATCH_ID:?BATCH_ID is required}"

DRY_RUN_REPO="${DRY_RUN_REPO:-}"

CONTENT_PATH="$GITHUB_WORKSPACE/$CONTENT_FILE"

if [ ! -f "$CONTENT_PATH" ]; then
  echo "ERROR: Content file not found: $CONTENT_PATH"
  exit 1
fi

#
# Generate an ID from the exact content.
# This prevents the exact same update being appended twice.
#
CONTENT_HASH=$(sha256sum "$CONTENT_PATH" | awk '{print substr($1,1,12)}')

BEGIN_MARKER="<!-- BEGIN ESHOPBOX-AGENTS-UPDATE:$CONTENT_HASH -->"
END_MARKER="<!-- END ESHOPBOX-AGENTS-UPDATE:$CONTENT_HASH -->"


detect_repo_type() {

  local dir="$1"

  #
  # FRONTEND
  # Strong frontend indicators.
  #

  if [ -f "$dir/angular.json" ] || \
     [ -f "$dir/vite.config.js" ] || \
     [ -f "$dir/vite.config.ts" ] || \
     [ -f "$dir/vite.config.mjs" ] || \
     [ -f "$dir/vue.config.js" ] || \
     [ -f "$dir/next.config.js" ] || \
     [ -f "$dir/next.config.mjs" ] || \
     [ -f "$dir/next.config.ts" ]; then

    echo "frontend"
    return
  fi


  #
  # BACKEND
  # Strong backend indicators.
  #

  if [ -f "$dir/pom.xml" ] || \
     [ -f "$dir/build.gradle" ] || \
     [ -f "$dir/build.gradle.kts" ] || \
     [ -f "$dir/go.mod" ] || \
     [ -f "$dir/requirements.txt" ] || \
     [ -f "$dir/pyproject.toml" ] || \
     [ -f "$dir/composer.json" ]; then

    echo "backend"
    return
  fi


  #
  # NODE.JS FALLBACK
  #

  if [ -f "$dir/package.json" ]; then

    if grep -Eq \
      '"(express|fastify|koa|nestjs|@nestjs/core)"' \
      "$dir/package.json"; then

      echo "backend"
      return
    fi

    if grep -Eq \
      '"(react|react-dom|@angular/core|vue|next|vite)"' \
      "$dir/package.json"; then

      echo "frontend"
      return
    fi

  fi


  echo "unknown"
}


matches_selected_type() {

  local detected="$1"

  #
  # "all" intentionally means backend + frontend.
  # Unknown/infrastructure repos are NOT included.
  #

  if [ "$REPO_TYPE" = "all" ]; then

    [ "$detected" = "backend" ] || \
    [ "$detected" = "frontend" ]

    return
  fi

  [ "$detected" = "$REPO_TYPE" ]
}


process_repo() {

  local repo="$1"

  echo
  echo "=================================================="
  echo "Repository: $ORG/$repo"
  echo "=================================================="


  #
  # Get repository metadata.
  #

  repo_info=$(gh repo view "$ORG/$repo" \
    --json isArchived,isFork,defaultBranchRef \
    --jq '[
      .isArchived,
      .isFork,
      .defaultBranchRef.name
    ] | @tsv') || {

      echo "SKIP: Unable to read repository."
      return 0
    }


  IFS=$'\t' read -r archived fork default_branch <<< "$repo_info"


  if [ "$archived" = "true" ]; then
    echo "SKIP: Archived repository."
    return 0
  fi


  if [ "$fork" = "true" ]; then
    echo "SKIP: Fork repository."
    return 0
  fi


  if [ -z "$default_branch" ]; then
    echo "SKIP: Unable to determine default branch."
    return 0
  fi


  echo "Default branch: $default_branch"


  #
  # Clone into temporary directory.
  #

  workdir=$(mktemp -d)


  if ! gh repo clone "$ORG/$repo" "$workdir" -- \
    --depth=1 \
    --branch "$default_branch" >/dev/null 2>&1; then

    echo "SKIP: Clone failed."

    rm -rf "$workdir"

    return 0
  fi


  #
  # Detect backend/frontend.
  #

  detected_type=$(detect_repo_type "$workdir")

  echo "Detected type: $detected_type"


  if ! matches_selected_type "$detected_type"; then

    echo "SKIP: Does not match selected type '$REPO_TYPE'."

    rm -rf "$workdir"

    return 0
  fi


  cd "$workdir"


  #
  # Duplicate protection.
  #

  if [ -f AGENTS.md ] && \
     grep -Fq "$BEGIN_MARKER" AGENTS.md; then

    echo "SKIP: Exact content already exists."

    cd "$GITHUB_WORKSPACE"

    rm -rf "$workdir"

    return 0
  fi


  #
  # Append content.
  #
  # Existing AGENTS.md is NEVER replaced.
  #

  if [ -f AGENTS.md ]; then

    echo "Existing AGENTS.md found. Appending content."

    printf '\n\n%s\n\n' "$BEGIN_MARKER" >> AGENTS.md

    cat "$CONTENT_PATH" >> AGENTS.md

    printf '\n\n%s\n' "$END_MARKER" >> AGENTS.md

  else

    echo "AGENTS.md not found. Creating file."

    printf '%s\n\n' "$BEGIN_MARKER" > AGENTS.md

    cat "$CONTENT_PATH" >> AGENTS.md

    printf '\n\n%s\n' "$END_MARKER" >> AGENTS.md

  fi


  #
  # SAFETY CHECK
  #
  # AGENTS.md must be the only changed file.
  #

  mapfile -t changed_files < <(
    git status --porcelain | sed 's/^...//'
  )


  if [ "${#changed_files[@]}" -ne 1 ] || \
     [ "${changed_files[0]}" != "AGENTS.md" ]; then

    echo "ERROR: Unexpected files changed."

    git status --short

    cd "$GITHUB_WORKSPACE"

    rm -rf "$workdir"

    return 1
  fi


  #
  # DRY RUN
  #

  if [ "$OPERATION" = "DRY_RUN" ]; then

    echo
    echo "=============================================="
    echo "DRY RUN ONLY"
    echo "=============================================="
    echo
    echo "Repository type : $detected_type"
    echo "Repository      : $ORG/$repo"
    echo
    echo "NO branch will be created."
    echo "NO commit will be created."
    echo "NOTHING will be pushed."
    echo "NO PR will be created."
    echo
    echo "Proposed diff:"
    echo "----------------------------------------------"

    git diff -- AGENTS.md

    echo "----------------------------------------------"
    echo "DRY RUN COMPLETE"
    echo "----------------------------------------------"

    cd "$GITHUB_WORKSPACE"

    rm -rf "$workdir"

    return 0
  fi


  #
  # APPLY
  #

  branch="org-action/agents-${BATCH_ID}"


  echo "Creating branch: $branch"


  git config \
    user.name \
    "eshopbox-org-agents[bot]"

  git config \
    user.email \
    "eshopbox-org-agents[bot]@users.noreply.github.com"


  git checkout -b "$branch"


  git add AGENTS.md


  git commit \
    -m "chore: update AGENTS.md"


  echo "Pushing branch..."


  git push origin "$branch"


  echo "Creating PR..."


  pr_url=$(gh pr create \
    --repo "$ORG/$repo" \
    --base "$default_branch" \
    --head "$branch" \
    --title "Update AGENTS.md" \
    --body "Organization-level automated AGENTS.md update.

Repository type detected: $detected_type

Batch: $BATCH_ID

Content ID: $CONTENT_HASH

Only AGENTS.md is modified.")


  echo "PR created: $pr_url"


  #
  # Merge PR.
  #
  # GitHub App should be configured as PR-only bypass actor.
  #

  echo "Merging PR..."


  gh pr merge "$pr_url" \
    --squash \
    --delete-branch


  echo "SUCCESS: $ORG/$repo updated and merged."


  cd "$GITHUB_WORKSPACE"

  rm -rf "$workdir"
}


echo "=================================================="
echo "AGENTS.md organization updater"
echo "=================================================="
echo "Organization : $ORG"
echo "Operation    : $OPERATION"
echo "Repo type    : $REPO_TYPE"
echo "Content file : $CONTENT_FILE"
echo "Content ID   : $CONTENT_HASH"
echo "=================================================="


#
# DRY RUN
#
# Only ONE repository is processed.
#

if [ "$OPERATION" = "DRY_RUN" ]; then

  if [ -z "$DRY_RUN_REPO" ]; then

    echo "ERROR: dry_run_repo is required."

    exit 1
  fi


  process_repo "$DRY_RUN_REPO"

  exit 0
fi


#
# APPLY
#

if [ "$OPERATION" != "APPLY" ]; then

  echo "ERROR: Invalid operation: $OPERATION"

  exit 1
fi


#
# Dynamically discover ALL active organization repositories.
#
# This means newly created repositories are automatically
# included in future runs.
#

repos=$(gh repo list "$ORG" \
  --limit 1000 \
  --json name,isArchived,isFork \
  --jq '.[] |
    select(.isArchived == false) |
    select(.isFork == false) |
    .name')


total=0
success=0
failed=0


for repo in $repos; do

  total=$((total + 1))

  if process_repo "$repo"; then

    success=$((success + 1))

  else

    failed=$((failed + 1))

    echo "FAILED: $repo"

  fi

done


echo
echo "=================================================="
echo "ROLLOUT COMPLETE"
echo "=================================================="
echo "Repositories checked : $total"
echo "Processed / skipped  : $success"
echo "Failed               : $failed"
echo "=================================================="


if [ "$failed" -gt 0 ]; then
  exit 1
fi
