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


# ============================================================
# Helpers
# ============================================================

cleanup_repo() {

  local workdir="$1"

  cd "$GITHUB_WORKSPACE" || true

  if [ -n "$workdir" ] && [ -d "$workdir" ]; then
    rm -rf "$workdir"
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


find_open_pr_by_base() {

  local repo="$1"
  local base_branch="$2"


  gh pr list \
    --repo "$ORG/$repo" \
    --state open \
    --base "$base_branch" \
    --head "$ROLLOUT_BRANCH" \
    --json url \
    --jq '.[0].url // empty' \
    2>/dev/null || true
}


# ============================================================
# Process repository
#
# Return codes:
#
# 0  = dry run success
# 2  = skipped
# 10 = bulk rollout completed
# 11 = single PR created
# 1  = failure
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


  echo "Production branch: $production_branch"


  # ----------------------------------------------------------
  # Clone production/default branch
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
  # Already merged into production?
  # ----------------------------------------------------------

  if [ -f AGENTS.md ] && \
     grep -Fq "$BEGIN_MARKER" AGENTS.md; then

    echo "SKIP: Exact content already exists in production AGENTS.md."
    echo "Content ID: $CONTENT_HASH"

    cleanup_repo "$workdir"

    return 2
  fi


  # ----------------------------------------------------------
  # Bulk requires staging branch
  # ----------------------------------------------------------

  if [ "$TARGET" != "single" ]; then

    if ! staging_branch=$(find_staging_branch "$production_branch"); then

      echo "SKIP: No supported staging branch found."
      echo "Expected staging or aws-velocis-staging."

      cleanup_repo "$workdir"

      return 2
    fi


    echo "Staging branch   : $staging_branch"
  fi


  # ==========================================================
  # EXISTING BRANCH / PR RESUME
  #
  # If previous run created the branch/PR, reuse it.
  # ==========================================================

  if remote_branch_exists "$branch"; then

    echo "Existing rollout branch found:"
    echo "$branch"


    # --------------------------------------------------------
    # SINGLE: reuse existing production PR if available
    # --------------------------------------------------------

    if [ "$TARGET" = "single" ]; then

      production_pr=$(find_open_pr_by_base \
        "$repo" \
        "$production_branch")


      if [ -n "$production_pr" ]; then

        echo "Existing production PR found:"
        echo "$production_pr"
        echo
        echo "SUCCESS: Existing PR reused."
        echo "Manual review/merge required."

        cleanup_repo "$workdir"

        return 11
      fi


      echo "FAILED: Rollout branch exists but no open production PR was found."

      cleanup_repo "$workdir"

      return 1
    fi


    # --------------------------------------------------------
    # BULK: reuse staging PR if available
    # --------------------------------------------------------

    staging_pr=$(find_open_pr_by_base \
      "$repo" \
      "$staging_branch")


    if [ -n "$staging_pr" ]; then

      echo "Existing staging PR found:"
      echo "$staging_pr"


      echo "Merging existing staging PR with bypass..."


      if ! gh pr merge "$staging_pr" \
        --repo "$ORG/$repo" \
        --merge \
        --admin; then

        echo "FAILED: Existing staging PR could not be merged."
        echo "$staging_pr"

        cleanup_repo "$workdir"

        return 1
      fi


      echo "SUCCESS: Existing staging PR merged."


      production_pr=$(find_open_pr_by_base \
        "$repo" \
        "$production_branch")


      if [ -z "$production_pr" ]; then

        echo "Creating production PR from existing rollout branch..."


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

      else

        echo "Existing production PR found:"
        echo "$production_pr"

      fi


      echo "Merging production PR with bypass..."


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
      echo "SUCCESS: Existing rollout resumed and completed."
      echo "Staging PR   : $staging_pr"
      echo "Production PR: $production_pr"

      cleanup_repo "$workdir"

      return 10
    fi


    # --------------------------------------------------------
    # Branch exists but staging PR does not.
    # Create staging PR from existing branch.
    # --------------------------------------------------------

    echo "Rollout branch exists but staging PR does not."
    echo "Creating staging PR..."


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


    echo "Staging PR created:"
    echo "$staging_pr"


    if ! gh pr merge "$staging_pr" \
      --repo "$ORG/$repo" \
      --merge \
      --admin; then

      echo "FAILED: Staging PR could not be merged."

      cleanup_repo "$workdir"

      return 1
    fi


    echo "SUCCESS: Staging PR merged."


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


    if ! gh pr merge "$production_pr" \
      --repo "$ORG/$repo" \
      --merge \
      --delete-branch \
      --admin; then

      echo "FAILED: Production merge failed."

      cleanup_repo "$workdir"

      return 1
    fi


    echo
    echo "SUCCESS: Existing rollout branch completed."
    echo "Staging PR   : $staging_pr"
    echo "Production PR: $production_pr"

    cleanup_repo "$workdir"

    return 10
  fi


  # ==========================================================
  # NEW ROLLOUT
  # ==========================================================


  # ----------------------------------------------------------
  # Modify AGENTS.md
  # ----------------------------------------------------------

  if [ -f AGENTS.md ]; then

    echo "Existing AGENTS.md found."
    echo "Appending content."

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

    echo "Repository        : $ORG/$repo"
    echo "Detected type     : $detected_type"
    echo "Production branch : $production_branch"
    echo "Content ID        : $CONTENT_HASH"
    echo "Rollout branch    : $branch"

    if [ "$TARGET" != "single" ]; then
      echo "Staging branch    : $staging_branch"
    fi

    echo
    echo "Proposed diff:"
    echo "----------------------------------------------"

    git diff -- AGENTS.md

    echo "----------------------------------------------"
    echo "NO branch created."
    echo "NO commit created."
    echo "NO push performed."
    echo "NO PR created."
    echo "NO merge performed."
    echo "----------------------------------------------"

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
  # Create working branch FROM production/default branch
  # ----------------------------------------------------------

  echo "Creating rollout branch:"
  echo "$branch"


  if ! git checkout -b "$branch"; then

    echo "FAILED: Unable to create rollout branch."

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


  working_commit=$(git rev-parse HEAD)

  echo "Working commit: $working_commit"


  # ----------------------------------------------------------
  # Push
  # ----------------------------------------------------------

  echo "Pushing rollout branch..."


  if ! git push \
    --set-upstream \
    origin \
    "$branch"; then

    echo "FAILED: Push failed."

    cleanup_repo "$workdir"

    return 1
  fi


  echo "Branch pushed successfully."


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
  # SINGLE
  #
  # Create production PR only.
  # ==========================================================

  if [ "$TARGET" = "single" ]; then

    echo "Creating production PR..."


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
    echo "PR: $production_pr"
    echo "Manual review/merge required."


    cleanup_repo "$workdir"

    return 11
  fi


  # ==========================================================
  # BULK STEP 1
  #
  # working branch -> staging
  # ==========================================================

  echo
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


  echo "Merging staging PR with bypass..."


  if ! gh pr merge "$staging_pr" \
    --repo "$ORG/$repo" \
    --merge \
    --admin; then

    echo "FAILED: Staging PR could not be merged."
    echo "$staging_pr"

    cleanup_repo "$workdir"

    return 1
  fi


  echo "SUCCESS: Staging PR merged."


  # ==========================================================
  # BULK STEP 2
  #
  # SAME original rollout branch -> production
  # ==========================================================

  echo
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


  echo "Merging production PR with bypass..."


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
  echo "Skipped               : $skipped"
  echo "Failed                : $failed"

fi

echo "=================================================="


if [ "$failed" -gt 0 ]; then
  exit 1
fi


exit 0
