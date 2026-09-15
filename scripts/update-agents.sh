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
CENTRAL_BACKLOG_ISSUE="${CENTRAL_BACKLOG_ISSUE:?CENTRAL_BACKLOG_ISSUE is required}"

TARGET_REPO="${TARGET_REPO:-}"

CONTENT_PATH="$GITHUB_WORKSPACE/$CONTENT_FILE"


# ============================================================
# Counters
# ============================================================

total=0
single_pr_created=0
staging_merged=0
production_merged=0
conflict_resolved=0
skipped=0
failed=0


# ============================================================
# Validation
# ============================================================

if [ ! -f "$CONTENT_PATH" ]; then
  echo "ERROR: Content file not found: $CONTENT_PATH"
  exit 1
fi

if [ ! -s "$CONTENT_PATH" ]; then
  echo "ERROR: Content file is empty: $CONTENT_PATH"
  exit 1
fi

if ! [[ "$CENTRAL_BACKLOG_ISSUE" =~ ^[0-9]+$ ]]; then
  echo "ERROR: CENTRAL_BACKLOG_ISSUE must be numeric."
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
  echo "ERROR: TARGET_REPO is required when TARGET=single."
  exit 1
fi


# ============================================================
# Content identity
# ============================================================

CONTENT_HASH=$(sha256sum "$CONTENT_PATH" | awk '{print substr($1,1,12)}')

BEGIN_MARKER="<!-- BEGIN ESHOPBOX-AGENTS-UPDATE:$CONTENT_HASH -->"
END_MARKER="<!-- END ESHOPBOX-AGENTS-UPDATE:$CONTENT_HASH -->"

ROLLOUT_BRANCH="org-action/agents-${CONTENT_HASH}"
CONFLICT_BRANCH="conflict/resolved/agents-${CONTENT_HASH}"


# ============================================================
# Helpers
# ============================================================

cleanup_repo() {

  local workdir="$1"

  cd "$GITHUB_WORKSPACE" || true

  if [ -n "$workdir" ] && [ -d "$workdir" ]; then
    rm -rf -- "$workdir" 2>/dev/null || {
      sleep 1
      rm -rf -- "$workdir" 2>/dev/null || true
    }
  fi
}


remote_branch_exists() {

  local branch="$1"

  git ls-remote \
    --exit-code \
    --heads \
    origin \
    "refs/heads/$branch" >/dev/null 2>&1
}


delete_remote_branch_if_exists() {

  local branch="$1"

  if remote_branch_exists "$branch"; then

    echo "Deleting remote branch: $branch"

    git push \
      origin \
      --delete "$branch" >/dev/null 2>&1 || true
  fi
}


# ============================================================
# Detect repository type
# ============================================================

detect_repo_type() {

  local dir="$1"


  # Frontend
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


  # Backend
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


  # Node fallback
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
# Target matching
# ============================================================

matches_target() {

  local repo="$1"
  local detected="$2"


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


  if [ "$TARGET" = "all" ]; then
    [ "$detected" = "backend" ] || \
    [ "$detected" = "frontend" ]
    return
  fi


  return 1
}


# ============================================================
# Production branch validation
#
# Bulk must NEVER use arbitrary feature/WIP/default branches.
# ============================================================

is_supported_production_branch() {

  local branch="$1"

  case "$branch" in
    main|master|aws-velocis-main)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}


# ============================================================
# Find staging branch
# ============================================================

find_staging_branch() {

  local production_branch="$1"


  if [ "$production_branch" = "aws-velocis-main" ]; then

    if remote_branch_exists "aws-velocis-staging"; then
      echo "aws-velocis-staging"
      return 0
    fi
  fi


  if remote_branch_exists "staging"; then
    echo "staging"
    return 0
  fi


  if remote_branch_exists "aws-velocis-staging"; then
    echo "aws-velocis-staging"
    return 0
  fi


  return 1
}


# ============================================================
# Find open PR
# ============================================================

find_open_pr_by_base() {

  local repo="$1"
  local base_branch="$2"
  local head_branch="$3"


  gh pr list \
    --repo "$ORG/$repo" \
    --state open \
    --base "$base_branch" \
    --head "$head_branch" \
    --json url \
    --jq '.[0].url // empty' \
    2>/dev/null || true
}


# ============================================================
# Append managed content to AGENTS.md
# ============================================================

apply_agents_content() {

  if [ -f AGENTS.md ] && \
     grep -Fq "$BEGIN_MARKER" AGENTS.md; then

    echo "Content already present in AGENTS.md."

    return 2
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


  return 0
}


# ============================================================
# Resolve staging conflict using temporary branch
#
# IMPORTANT:
# - branch is created FROM staging
# - only AGENTS.md content is applied
# - conflict branch is merged into staging
# - conflict branch is deleted
# - original rollout branch is preserved for production PR
# ============================================================

resolve_staging_conflict() {

  local repo="$1"
  local staging_branch="$2"

  local conflict_pr=""
  local conflict_commit=""


  echo
  echo "Staging conflict detected."
  echo "Creating temporary conflict-resolution branch."


  delete_remote_branch_if_exists "$CONFLICT_BRANCH"


  git fetch origin \
    "$staging_branch" \
    --depth=100 >/dev/null 2>&1 || true


  if ! git checkout \
    -B "$CONFLICT_BRANCH" \
    "origin/$staging_branch"; then

    echo "FAILED: Unable to create conflict branch from staging."
    return 1
  fi


  # Apply only the organization-managed AGENTS content.
  apply_agents_content
  apply_result=$?


  if [ "$apply_result" -eq 2 ]; then

    echo "Content already exists in staging AGENTS.md."
    echo "No temporary conflict commit required."

    git checkout "$ROLLOUT_BRANCH" >/dev/null 2>&1 || true

    return 0
  fi


  # Only AGENTS.md may change.
  mapfile -t conflict_changed_files < <(
    git status --porcelain | sed 's/^...//'
  )


  if [ "${#conflict_changed_files[@]}" -ne 1 ] || \
     [ "${conflict_changed_files[0]}" != "AGENTS.md" ]; then

    echo "FAILED: Conflict-resolution branch changed unexpected files."

    git status --short

    return 1
  fi


  git add AGENTS.md


  if ! git commit \
    -m "chore: resolve AGENTS.md rollout for staging"; then

    echo "FAILED: Unable to commit conflict-resolution change."
    return 1
  fi


  conflict_commit=$(git rev-parse HEAD)

  echo "Conflict-resolution commit: $conflict_commit"


  if ! git push \
    --set-upstream \
    origin \
    "$CONFLICT_BRANCH"; then

    echo "FAILED: Unable to push conflict-resolution branch."
    return 1
  fi


  conflict_pr=$(find_open_pr_by_base \
    "$repo" \
    "$staging_branch" \
    "$CONFLICT_BRANCH")


  if [ -z "$conflict_pr" ]; then

    echo "Creating conflict-resolution PR..."


    if ! conflict_pr=$(gh pr create \
      --repo "$ORG/$repo" \
      --base "$staging_branch" \
      --head "$CONFLICT_BRANCH" \
      --title "Codex to Staging: Resolve AGENTS.md rollout conflict" \
      --body "Automated conflict-resolution branch for organization AGENTS.md rollout.

Central Backlog:
Eshopbox-Enginnering/Central-Backlog#$CENTRAL_BACKLOG_ISSUE

Repository:
$ORG/$repo

Content ID:
$CONTENT_HASH

This branch was created from $staging_branch and only updates AGENTS.md."); then

      echo "FAILED: Unable to create conflict-resolution PR."
      return 1
    fi
  fi


  echo "Conflict-resolution PR:"
  echo "$conflict_pr"


  if ! gh pr merge "$conflict_pr" \
    --repo "$ORG/$repo" \
    --merge \
    --delete-branch \
    --admin; then

    echo "FAILED: Conflict-resolution PR could not be merged."
    echo "$conflict_pr"

    return 1
  fi


  echo "SUCCESS: Conflict-resolution PR merged to staging."


  git checkout "$ROLLOUT_BRANCH" >/dev/null 2>&1 || true


  return 0
}


# ============================================================
# Process repository
#
# Return codes:
#
# 0  dry run success
# 2  skipped
# 10 bulk completed
# 11 single PR created
# 12 bulk completed with conflict-resolution path
# 1  failure
# ============================================================

process_repo() {

  local repo="$1"

  local repo_info=""
  local archived=""
  local fork=""
  local production_branch=""
  local staging_branch=""

  local workdir=""
  local detected_type=""

  local branch="$ROLLOUT_BRANCH"

  local staging_pr=""
  local production_pr=""

  local pr_body=""
  local working_commit=""

  local conflict_used=0


  echo
  echo "=================================================="
  echo "Repository: $ORG/$repo"
  echo "=================================================="


  # ----------------------------------------------------------
  # Metadata
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


  IFS=$'\t' read -r archived fork production_branch <<< "$repo_info"


  if [ "$archived" = "true" ]; then
    echo "SKIP: Archived repository."
    return 2
  fi


  if [ "$fork" = "true" ]; then
    echo "SKIP: Fork repository."
    return 2
  fi


  if [ -z "$production_branch" ]; then
    echo "SKIP: Unable to determine production/default branch."
    return 2
  fi


  echo "Default branch: $production_branch"


  # ----------------------------------------------------------
  # Bulk production safety
  # ----------------------------------------------------------

  if [ "$TARGET" != "single" ]; then

    if ! is_supported_production_branch "$production_branch"; then

      echo "SKIP: Unsupported production branch for bulk rollout."
      echo "Default branch: $production_branch"
      echo "Allowed: main, master, aws-velocis-main"

      return 2
    fi
  fi


  # ----------------------------------------------------------
  # Clone production
  # ----------------------------------------------------------

  workdir=$(mktemp -d)


  if ! gh repo clone "$ORG/$repo" "$workdir" -- \
    --depth=1 \
    --branch "$production_branch"; then

    echo "FAILED: Clone failed."

    cleanup_repo "$workdir"

    return 1
  fi


  cd "$workdir" || {

    echo "FAILED: Unable to enter repository."

    cleanup_repo "$workdir"

    return 1
  }


  git fetch origin \
    '+refs/heads/*:refs/remotes/origin/*' \
    --depth=100 >/dev/null 2>&1 || true


  # ----------------------------------------------------------
  # Detect type
  # ----------------------------------------------------------

  detected_type=$(detect_repo_type "$workdir")

  echo "Detected type: $detected_type"


  if ! matches_target "$repo" "$detected_type"; then

    echo "SKIP: Repository does not match target '$TARGET'."

    cleanup_repo "$workdir"

    return 2
  fi


  # ----------------------------------------------------------
  # Already in production?
  # ----------------------------------------------------------

  if [ -f AGENTS.md ] && \
     grep -Fq "$BEGIN_MARKER" AGENTS.md; then

    echo "SKIP: Exact content already exists in production AGENTS.md."
    echo "Content ID: $CONTENT_HASH"

    cleanup_repo "$workdir"

    return 2
  fi


  # ----------------------------------------------------------
  # Bulk requires staging
  # ----------------------------------------------------------

  if [ "$TARGET" != "single" ]; then

    if ! staging_branch=$(find_staging_branch "$production_branch"); then

      echo "SKIP: No supported staging branch found."

      cleanup_repo "$workdir"

      return 2
    fi


    echo "Production branch: $production_branch"
    echo "Staging branch   : $staging_branch"


    if [ "$production_branch" = "$staging_branch" ]; then

      echo "SKIP: Production and staging branch are the same."
      echo "Branch: $production_branch"

      cleanup_repo "$workdir"

      return 2
    fi
  fi


  # ==========================================================
  # EXISTING ROLLOUT BRANCH
  # ==========================================================

  if remote_branch_exists "$branch"; then

    echo "Existing rollout branch found:"
    echo "$branch"


    # --------------------------------------------------------
    # Single
    # --------------------------------------------------------

    if [ "$TARGET" = "single" ]; then

      production_pr=$(find_open_pr_by_base \
        "$repo" \
        "$production_branch" \
        "$branch")


      if [ -n "$production_pr" ]; then

        echo "SUCCESS: Existing single-repo PR found."
        echo "$production_pr"

        cleanup_repo "$workdir"

        return 11
      fi


      echo "FAILED: Rollout branch exists but no production PR found."

      cleanup_repo "$workdir"

      return 1
    fi


    # --------------------------------------------------------
    # Existing staging PR
    # --------------------------------------------------------

    staging_pr=$(find_open_pr_by_base \
      "$repo" \
      "$staging_branch" \
      "$branch")


    if [ -n "$staging_pr" ]; then

      echo "Existing staging PR:"
      echo "$staging_pr"


      if ! gh pr merge "$staging_pr" \
        --repo "$ORG/$repo" \
        --merge \
        --admin; then

        echo "Direct staging merge failed."
        echo "Attempting conflict-resolution flow..."


        if ! resolve_staging_conflict \
          "$repo" \
          "$staging_branch"; then

          echo "FAILED: Unable to resolve staging conflict."

          cleanup_repo "$workdir"

          return 1
        fi


        conflict_used=1
      fi


    else

      echo "Existing rollout branch has no staging PR."
      echo "Creating staging PR..."


      pr_body="Organization-level automated AGENTS.md update.

Central Backlog:
Eshopbox-Enginnering/Central-Backlog#$CENTRAL_BACKLOG_ISSUE

Repository:
$ORG/$repo

Content ID:
$CONTENT_HASH

Only AGENTS.md is modified."


      if ! staging_pr=$(gh pr create \
        --repo "$ORG/$repo" \
        --base "$staging_branch" \
        --head "$branch" \
        --title "Codex to Staging: Update AGENTS.md organization guidance" \
        --body "$pr_body"); then

        echo "FAILED: Staging PR creation failed."

        cleanup_repo "$workdir"

        return 1
      fi


      if ! gh pr merge "$staging_pr" \
        --repo "$ORG/$repo" \
        --merge \
        --admin; then

        echo "Direct staging merge failed."
        echo "Attempting conflict-resolution flow..."


        if ! resolve_staging_conflict \
          "$repo" \
          "$staging_branch"; then

          echo "FAILED: Unable to resolve staging conflict."

          cleanup_repo "$workdir"

          return 1
        fi


        conflict_used=1
      fi
    fi


    # --------------------------------------------------------
    # Production PR
    # --------------------------------------------------------

    production_pr=$(find_open_pr_by_base \
      "$repo" \
      "$production_branch" \
      "$branch")


    if [ -z "$production_pr" ]; then

      echo "Creating production PR..."


      pr_body="Organization-level automated AGENTS.md update.

Central Backlog:
Eshopbox-Enginnering/Central-Backlog#$CENTRAL_BACKLOG_ISSUE

Repository:
$ORG/$repo

Content ID:
$CONTENT_HASH

Only AGENTS.md is modified."


      if ! production_pr=$(gh pr create \
        --repo "$ORG/$repo" \
        --base "$production_branch" \
        --head "$branch" \
        --title "Codex to Main: Update AGENTS.md organization guidance" \
        --body "$pr_body"); then

        echo "FAILED: Production PR creation failed."

        cleanup_repo "$workdir"

        return 1
      fi
    fi


    if ! gh pr merge "$production_pr" \
      --repo "$ORG/$repo" \
      --merge \
      --delete-branch \
      --admin; then

      echo "FAILED: Production PR could not be merged."
      echo "$production_pr"

      cleanup_repo "$workdir"

      return 1
    fi


    echo
    echo "SUCCESS: Existing rollout completed."
    echo "Production PR: $production_pr"


    cleanup_repo "$workdir"


    if [ "$conflict_used" -eq 1 ]; then
      return 12
    fi


    return 10
  fi


  # ==========================================================
  # NEW ROLLOUT
  # ==========================================================

  apply_agents_content


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
  # Dry run
  # ----------------------------------------------------------

  if [ "$OPERATION" = "DRY_RUN" ]; then

    echo
    echo "----------------------------------------------"
    echo "DRY RUN"
    echo "----------------------------------------------"

    echo "Repository        : $ORG/$repo"
    echo "Detected type     : $detected_type"
    echo "Production branch : $production_branch"

    if [ "$TARGET" != "single" ]; then
      echo "Staging branch    : $staging_branch"
    fi

    echo
    git diff -- AGENTS.md

    echo
    echo "NO branch created."
    echo "NO PR created."
    echo "NO merge performed."

    cleanup_repo "$workdir"

    return 0
  fi


  # ----------------------------------------------------------
  # Git identity
  # ----------------------------------------------------------

  git config \
    user.name \
    "eshopbox-org-agents[bot]"


  git config \
    user.email \
    "eshopbox-org-agents[bot]@users.noreply.github.com"


  # ----------------------------------------------------------
  # Create rollout branch FROM production
  # ----------------------------------------------------------

  echo "Creating rollout branch:"
  echo "$branch"


  if ! git checkout -b "$branch"; then

    echo "FAILED: Unable to create rollout branch."

    cleanup_repo "$workdir"

    return 1
  fi


  git add AGENTS.md


  if ! git commit \
    -m "chore: update AGENTS.md"; then

    echo "FAILED: Unable to commit AGENTS.md."

    cleanup_repo "$workdir"

    return 1
  fi


  working_commit=$(git rev-parse HEAD)

  echo "Working commit: $working_commit"


  if ! git push \
    --set-upstream \
    origin \
    "$branch"; then

    echo "FAILED: Push failed."

    cleanup_repo "$workdir"

    return 1
  fi


  # ----------------------------------------------------------
  # PR body
  # ----------------------------------------------------------

  pr_body="Organization-level automated AGENTS.md update.

Central Backlog:
Eshopbox-Enginnering/Central-Backlog#$CENTRAL_BACKLOG_ISSUE

Repository:
$ORG/$repo

Repository type:
$detected_type

Batch:
$BATCH_ID

Content ID:
$CONTENT_HASH

Working commit:
$working_commit

Only AGENTS.md is modified."


  # ==========================================================
  # Single
  # ==========================================================

  if [ "$TARGET" = "single" ]; then

    if ! production_pr=$(gh pr create \
      --repo "$ORG/$repo" \
      --base "$production_branch" \
      --head "$branch" \
      --title "Codex to Main: Update AGENTS.md organization guidance" \
      --body "$pr_body"); then

      echo "FAILED: PR creation failed."

      cleanup_repo "$workdir"

      return 1
    fi


    echo
    echo "SUCCESS: Single repository PR created."
    echo "$production_pr"
    echo "Manual review/merge required."


    cleanup_repo "$workdir"

    return 11
  fi


  # ==========================================================
  # Bulk staging
  # ==========================================================

  echo "Creating staging PR..."


  if ! staging_pr=$(gh pr create \
    --repo "$ORG/$repo" \
    --base "$staging_branch" \
    --head "$branch" \
    --title "Codex to Staging: Update AGENTS.md organization guidance" \
    --body "$pr_body"); then

    echo "FAILED: Staging PR creation failed."

    cleanup_repo "$workdir"

    return 1
  fi


  echo "Staging PR:"
  echo "$staging_pr"


  if ! gh pr merge "$staging_pr" \
    --repo "$ORG/$repo" \
    --merge \
    --admin; then

    echo "Direct staging merge failed."
    echo "Attempting conflict-resolution flow..."


    if ! resolve_staging_conflict \
      "$repo" \
      "$staging_branch"; then

      echo "FAILED: Unable to resolve staging conflict."

      cleanup_repo "$workdir"

      return 1
    fi


    conflict_used=1
  fi


  # ==========================================================
  # Bulk production
  # ==========================================================

  echo "Creating production PR..."


  if ! production_pr=$(gh pr create \
    --repo "$ORG/$repo" \
    --base "$production_branch" \
    --head "$branch" \
    --title "Codex to Main: Update AGENTS.md organization guidance" \
    --body "$pr_body"); then

    echo "FAILED: Production PR creation failed."

    cleanup_repo "$workdir"

    return 1
  fi


  echo "Production PR:"
  echo "$production_pr"


  if ! gh pr merge "$production_pr" \
    --repo "$ORG/$repo" \
    --merge \
    --delete-branch \
    --admin; then

    echo "FAILED: Production PR could not be merged."
    echo "$production_pr"

    cleanup_repo "$workdir"

    return 1
  fi


  echo
  echo "SUCCESS: Repository rollout completed."
  echo "Staging PR   : $staging_pr"
  echo "Production PR: $production_pr"


  cleanup_repo "$workdir"


  if [ "$conflict_used" -eq 1 ]; then
    return 12
  fi


  return 10
}


# ============================================================
# Header
# ============================================================

echo "=================================================="
echo "AGENTS.md organization updater"
echo "=================================================="
echo "Organization          : $ORG"
echo "Operation             : $OPERATION"
echo "Target                : $TARGET"

if [ "$TARGET" = "single" ]; then
  echo "Repository            : $TARGET_REPO"
fi

echo "Content file          : $CONTENT_FILE"
echo "Content ID            : $CONTENT_HASH"
echo "Rollout branch        : $ROLLOUT_BRANCH"
echo "Conflict branch       : $CONFLICT_BRANCH"
echo "Central Backlog issue : $CENTRAL_BACKLOG_ISSUE"
echo "Batch ID              : $BATCH_ID"
echo "=================================================="


# ============================================================
# Single
# ============================================================

if [ "$TARGET" = "single" ]; then

  total=1


  process_repo "$TARGET_REPO"
  result=$?


  case "$result" in

    0)
      ;;

    2)
      skipped=$((skipped + 1))
      ;;

    11)
      single_pr_created=$((single_pr_created + 1))
      ;;

    *)
      failed=$((failed + 1))
      ;;

  esac


# ============================================================
# Bulk
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
        ;;

      2)
        skipped=$((skipped + 1))
        ;;

      10)
        staging_merged=$((staging_merged + 1))
        production_merged=$((production_merged + 1))
        ;;

      12)
        staging_merged=$((staging_merged + 1))
        production_merged=$((production_merged + 1))
        conflict_resolved=$((conflict_resolved + 1))
        ;;

      *)
        failed=$((failed + 1))
        echo "FAILED: $repo"
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

if [ "$OPERATION" = "DRY_RUN" ]; then

  echo "Operation             : DRY_RUN"
  echo "Skipped               : $skipped"
  echo "Failed                : $failed"

else

  echo "Single PRs created    : $single_pr_created"
  echo "Staging merged        : $staging_merged"
  echo "Production merged     : $production_merged"
  echo "Conflicts resolved    : $conflict_resolved"
  echo "Skipped               : $skipped"
  echo "Failed                : $failed"

fi

echo "=================================================="


if [ "$failed" -gt 0 ]; then
  exit 1
fi


exit 0
