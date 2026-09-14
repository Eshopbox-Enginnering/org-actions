#!/usr/bin/env bash

set -uo pipefail


# ============================================================
# Required configuration
# ============================================================

ORG="${ORG:?ORG is required}"
OPERATION="${OPERATION:?OPERATION is required}"
TARGET="${TARGET:?TARGET is required}"
CONTENT_FILE="${CONTENT_FILE:?CONTENT_FILE is required}"
BATCH_ID="${BATCH_ID:?BATCH_ID is required}"

TARGET_REPO="${TARGET_REPO:-}"

CONTENT_PATH="$GITHUB_WORKSPACE/$CONTENT_FILE"


# ============================================================
# Counters
# ============================================================

total=0
updated=0
skipped=0
failed=0


# ============================================================
# Validate input
# ============================================================

if [ ! -f "$CONTENT_PATH" ]; then
  echo "ERROR: Content file not found: $CONTENT_PATH"
  exit 1
fi

if [ ! -s "$CONTENT_PATH" ]; then
  echo "ERROR: Content file is empty: $CONTENT_PATH"
  exit 1
fi

case "$OPERATION" in
  DRY_RUN|APPLY)
    ;;
  *)
    echo "ERROR: Invalid operation: $OPERATION"
    exit 1
    ;;
esac


case "$TARGET" in
  single|backend|frontend|all)
    ;;
  *)
    echo "ERROR: Invalid target: $TARGET"
    exit 1
    ;;
esac


if [ "$TARGET" = "single" ] && [ -z "$TARGET_REPO" ]; then
  echo "ERROR: TARGET_REPO is required when TARGET=single"
  exit 1
fi


# ============================================================
# Generate content ID
#
# This prevents the exact same update from being appended twice.
# ============================================================

CONTENT_HASH=$(sha256sum "$CONTENT_PATH" | awk '{print substr($1,1,12)}')

BEGIN_MARKER="<!-- BEGIN ESHOPBOX-AGENTS-UPDATE:$CONTENT_HASH -->"
END_MARKER="<!-- END ESHOPBOX-AGENTS-UPDATE:$CONTENT_HASH -->"


# ============================================================
# Cleanup helper
# ============================================================

cleanup_repo() {

  local workdir="$1"

  cd "$GITHUB_WORKSPACE" || true

  if [ -n "$workdir" ] && [ -d "$workdir" ]; then
    rm -rf "$workdir"
  fi
}


# ============================================================
# Detect repository type
# ============================================================

detect_repo_type() {

  local dir="$1"


  # ----------------------------------------------------------
  # Frontend
  # ----------------------------------------------------------

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


  # ----------------------------------------------------------
  # Backend
  # ----------------------------------------------------------

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


  # ----------------------------------------------------------
  # Node fallback
  # ----------------------------------------------------------

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


# ============================================================
# Check whether repository should be processed
# ============================================================

matches_target() {

  local repo="$1"
  local detected="$2"


  # Single repository takes priority.
  #
  # Do NOT check frontend/backend classification here.
  # If user explicitly selected a repo, process that repo.
  if [ "$TARGET" = "single" ]; then
    [ "$repo" = "$TARGET_REPO" ]
    return
  fi


  if [ "$TARGET" = "backend" ]; then
    [ "$detected" = "backend" ]
    return
  fi


  if [ "$TARGET" = "frontend" ]; then
    [ "$detected" = "frontend" ]
    return
  fi


  # "all" = all detected backend + frontend repositories.
  #
  # Infrastructure / unknown repositories are intentionally excluded.
  if [ "$TARGET" = "all" ]; then

    [ "$detected" = "backend" ] || \
    [ "$detected" = "frontend" ]

    return
  fi


  return 1
}


# ============================================================
# Process repository
# ============================================================

process_repo() {

  local repo="$1"
  local repo_info=""
  local archived=""
  local fork=""
  local default_branch=""
  local workdir=""
  local detected_type=""
  local branch=""
  local pr_url=""


  echo
  echo "=================================================="
  echo "Repository: $ORG/$repo"
  echo "=================================================="


  # ----------------------------------------------------------
  # Read repository metadata
  # ----------------------------------------------------------

  if ! repo_info=$(gh repo view "$ORG/$repo" \
    --json isArchived,isFork,defaultBranchRef \
    --jq '[
      .isArchived,
      .isFork,
      (.defaultBranchRef.name // "")
    ] | @tsv'); then

    echo "FAILED: Unable to read repository metadata."
    return 1
  fi


  IFS=$'\t' read -r archived fork default_branch <<< "$repo_info"


  if [ "$archived" = "true" ]; then
    echo "SKIP: Archived repository."
    return 2
  fi


  if [ "$fork" = "true" ]; then
    echo "SKIP: Fork repository."
    return 2
  fi


  if [ -z "$default_branch" ]; then
    echo "SKIP: Unable to determine default branch."
    return 2
  fi


  echo "Default branch: $default_branch"


  # ----------------------------------------------------------
  # Clone repository
  # ----------------------------------------------------------

  workdir=$(mktemp -d)


  if ! gh repo clone "$ORG/$repo" "$workdir" -- \
    --depth=1 \
    --branch "$default_branch"; then

    echo "FAILED: Unable to clone repository."

    cleanup_repo "$workdir"

    return 1
  fi


  # ----------------------------------------------------------
  # Detect repository type
  # ----------------------------------------------------------

  detected_type=$(detect_repo_type "$workdir")

  echo "Detected type: $detected_type"


  # ----------------------------------------------------------
  # Target filtering
  # ----------------------------------------------------------

  if ! matches_target "$repo" "$detected_type"; then

    echo "SKIP: Repository does not match target '$TARGET'."

    cleanup_repo "$workdir"

    return 2
  fi


  cd "$workdir" || {

    echo "FAILED: Unable to enter work directory."

    cleanup_repo "$workdir"

    return 1
  }


  # ----------------------------------------------------------
  # Duplicate protection
  # ----------------------------------------------------------

  if [ -f AGENTS.md ] && \
     grep -Fq "$BEGIN_MARKER" AGENTS.md; then

    echo "SKIP: Exact content already exists in AGENTS.md."

    cleanup_repo "$workdir"

    return 2
  fi


  # ----------------------------------------------------------
  # Append content
  #
  # IMPORTANT:
  # Existing AGENTS.md is NEVER replaced.
  # ----------------------------------------------------------

  if [ -f AGENTS.md ]; then

    echo "Existing AGENTS.md found."
    echo "Appending organization content to bottom."

    printf '\n\n%s\n\n' "$BEGIN_MARKER" >> AGENTS.md

    cat "$CONTENT_PATH" >> AGENTS.md

    printf '\n\n%s\n' "$END_MARKER" >> AGENTS.md

  else

    echo "AGENTS.md not found."
    echo "Creating AGENTS.md."

    printf '%s\n\n' "$BEGIN_MARKER" > AGENTS.md

    cat "$CONTENT_PATH" >> AGENTS.md

    printf '\n\n%s\n' "$END_MARKER" >> AGENTS.md

  fi


  # ----------------------------------------------------------
  # Safety check
  #
  # AGENTS.md MUST be the only changed file.
  # ----------------------------------------------------------

  mapfile -t changed_files < <(
    git status --porcelain | sed 's/^...//'
  )


  if [ "${#changed_files[@]}" -ne 1 ] || \
     [ "${changed_files[0]}" != "AGENTS.md" ]; then

    echo "FAILED: Unexpected files changed."

    git status --short

    cleanup_repo "$workdir"

    return 1
  fi


  # ----------------------------------------------------------
  # DRY RUN
  # ----------------------------------------------------------

  if [ "$OPERATION" = "DRY_RUN" ]; then

    echo
    echo "----------------------------------------------"
    echo "DRY RUN"
    echo "----------------------------------------------"
    echo "Repository : $ORG/$repo"
    echo "Type       : $detected_type"
    echo
    echo "NO branch will be created."
    echo "NO commit will be created."
    echo "NO push will happen."
    echo "NO PR will be created."
    echo
    echo "Proposed diff:"
    echo "----------------------------------------------"

    git diff -- AGENTS.md

    echo "----------------------------------------------"
    echo "DRY RUN COMPLETE"
    echo "----------------------------------------------"

    cleanup_repo "$workdir"

    return 0
  fi


  # ----------------------------------------------------------
  # APPLY
  # ----------------------------------------------------------

  branch="org-action/agents-${BATCH_ID}"


  echo "Creating branch: $branch"


  git config \
    user.name \
    "eshopbox-org-agents[bot]"


  git config \
    user.email \
    "eshopbox-org-agents[bot]@users.noreply.github.com"


  # ----------------------------------------------------------
  # Ensure branch does not already exist remotely
  # ----------------------------------------------------------

  if git ls-remote \
    --exit-code \
    --heads \
    origin \
    "$branch" >/dev/null 2>&1; then

    echo "FAILED: Remote branch already exists: $branch"

    cleanup_repo "$workdir"

    return 1
  fi


  # ----------------------------------------------------------
  # Create local branch
  # ----------------------------------------------------------

  if ! git checkout -b "$branch"; then

    echo "FAILED: Unable to create branch."

    cleanup_repo "$workdir"

    return 1
  fi


  # ----------------------------------------------------------
  # Commit
  # ----------------------------------------------------------

  git add AGENTS.md


  if ! git commit \
    -m "chore: update AGENTS.md"; then

    echo "FAILED: Unable to create commit."

    cleanup_repo "$workdir"

    return 1
  fi


  # ----------------------------------------------------------
  # Push
  #
  # CRITICAL:
  # Never continue to PR creation if push fails.
  # ----------------------------------------------------------

  echo "Pushing branch..."


  if ! git push \
    --set-upstream \
    origin \
    "$branch"; then

    echo "FAILED: git push failed."

    cleanup_repo "$workdir"

    return 1
  fi


  echo "Branch pushed successfully."


  # ----------------------------------------------------------
  # Verify branch actually exists remotely
  # ----------------------------------------------------------

  if ! git ls-remote \
    --exit-code \
    --heads \
    origin \
    "$branch" >/dev/null 2>&1; then

    echo "FAILED: Branch was not found remotely after push."

    cleanup_repo "$workdir"

    return 1
  fi


  # ----------------------------------------------------------
  # Create PR
  # ----------------------------------------------------------

  echo "Creating PR..."


  if ! pr_url=$(gh pr create \
    --repo "$ORG/$repo" \
    --base "$default_branch" \
    --head "$branch" \
    --title "Update AGENTS.md" \
    --body "Organization-level automated AGENTS.md update.

Repository: $ORG/$repo

Repository type detected: $detected_type

Batch: $BATCH_ID

Content ID: $CONTENT_HASH

Only AGENTS.md is modified."); then

    echo "FAILED: PR creation failed."

    cleanup_repo "$workdir"

    return 1
  fi


  if [ -z "$pr_url" ]; then

    echo "FAILED: GitHub did not return a PR URL."

    cleanup_repo "$workdir"

    return 1
  fi


  echo "PR created: $pr_url"


  # ----------------------------------------------------------
  # Merge PR
  #
  # Remove this block if you want manual approval + merge.
  # ----------------------------------------------------------

  echo "Merging PR..."


  if ! gh pr merge "$pr_url" \
    --repo "$ORG/$repo" \
    --squash \
    --delete-branch; then

    echo "FAILED: PR merge failed."
    echo "PR remains available at: $pr_url"

    cleanup_repo "$workdir"

    return 1
  fi


  echo "SUCCESS: $ORG/$repo updated and merged."


  cleanup_repo "$workdir"

  return 0
}


# ============================================================
# Header
# ============================================================

echo "=================================================="
echo "AGENTS.md organization updater"
echo "=================================================="
echo "Organization : $ORG"
echo "Operation    : $OPERATION"
echo "Target       : $TARGET"

if [ "$TARGET" = "single" ]; then
  echo "Repository   : $TARGET_REPO"
fi

echo "Content file : $CONTENT_FILE"
echo "Content ID   : $CONTENT_HASH"
echo "Batch ID     : $BATCH_ID"
echo "=================================================="


# ============================================================
# SINGLE REPOSITORY
#
# Most important safety behavior:
# if single is selected, NEVER enumerate organization repos.
# ============================================================

if [ "$TARGET" = "single" ]; then

  total=1

  process_repo "$TARGET_REPO"
  result=$?


  case "$result" in

    0)
      updated=$((updated + 1))
      ;;

    2)
      skipped=$((skipped + 1))
      ;;

    *)
      failed=$((failed + 1))
      ;;

  esac


# ============================================================
# ORGANIZATION TARGET
# ============================================================

else

  echo
  echo "Discovering organization repositories..."


  if ! repos=$(gh repo list "$ORG" \
    --limit 1000 \
    --json name,isArchived,isFork \
    --jq '.[] |
      select(.isArchived == false) |
      select(.isFork == false) |
      .name'); then

    echo "ERROR: Unable to retrieve organization repositories."
    exit 1
  fi


  for repo in $repos; do

    total=$((total + 1))


    process_repo "$repo"
    result=$?


    case "$result" in

      0)
        updated=$((updated + 1))
        ;;

      2)
        skipped=$((skipped + 1))
        ;;

      *)
        failed=$((failed + 1))
        ;;

    esac

  done

fi


# ============================================================
# Summary
# ============================================================

echo
echo "=================================================="
echo "ROLLOUT COMPLETE"
echo "=================================================="
echo "Repositories checked : $total"
echo "Updated               : $updated"
echo "Skipped               : $skipped"
echo "Failed                : $failed"
echo "=================================================="


if [ "$failed" -gt 0 ]; then
  exit 1
fi


exit 0
