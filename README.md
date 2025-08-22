# Eshopbox Org-Level Actions & Reusable Workflows

> Jira: [DEVOPS-2719](https://auperator.atlassian.net/browse/DEVOPS-2719)

Centralized repository hosting reusable **GitHub Actions** and **workflows** for the Eshopbox organization. Use these to ensure consistent automation, faster onboarding, and easier maintenance across all repositories.

// This is testing dummy line for commit validation //




---

## Repository Structure

```
.github/                     # org-level repo
└─ workflows/                # reusable workflows (called via workflow_call)
   ├─ ai-code-review.yml
   ├─ check-commits-in-staging.yml
   └─ sonar-java.yml
└─ CODEOWNERS
└─ README.md
```

---

## What Each Repo Must Add (caller workflows)

Add the following YAML files to **every repository** that should use the org-level automations. These “caller” files trigger and delegate to the reusable workflows in `Eshopbox-Enginnering/org-actions`.

> Replace `@master` with a tag like `@v1` once you create a stable release.

### 1) Codex AI Code Review

Create: `.github/workflows/ai-code-review.yml`

```yaml
name: Codex AI Code Review
on:
  pull_request:
    types: [opened, synchronize, reopened]
    branches:
      - 'feature/*'
      - 'Feature/*'
      - 'FEATURE/*'
      - staging
      - master
      - main

jobs:
  review:
    uses: Eshopbox-Enginnering/org-actions/.github/workflows/ai-code-review.yml@main
    permissions: inherit
    secrets: inherit
    with:
      ai_model: gpt-4o-mini
      temperature: "0.3"
      approve_reviews: "false"
      max_comments: "25"
      project_context: "This is a Java Spring Boot backend project"
      context_files: "pom.xml,README.md"
      exclude_patterns: "**/*.md,**/*.json,**/dist/**,**/target/**,**/build/**,**/*.class"
      check_src_dir: "src/"
```

**Required org secret:** `AI_REVIEW_CODEX`

---

### 2) Check PR Commits Exist in `staging`

Create: `.github/workflows/check-commits-in-staging.yml`

```yaml
name: check-commits-in-staging
on:
  pull_request:
    branches: [ master, main ]

jobs:
  check:
    uses: Eshopbox-Enginnering/org-actions/.github/workflows/check-commits-in-staging.yml@main
    permissions: read-all
    with:
      compare_branch: staging
```

**Ruleset:** Target branches `master` and `main`, and **Require status checks to pass** → add `check-commits-in-staging` (exact job name shown on PRs).

---

### 3) Sonar Java (Manual Scan)

Create: `.github/workflows/sonar-java.yml`

```yaml
name: SonarQube Scan (Java)

on:
  workflow_dispatch:
    inputs:
      project_key:
        description: "Sonar project key (optional)"
        required: false
      maven_args:
        description: "Extra Maven args (optional)"
        required: false

jobs:
  sonar:
    uses: Eshopbox-Enginnering/org-actions/.github/workflows/sonar-java.yml@main
    permissions: read-all
    secrets: inherit
    with:
      project_key: ${{ inputs.project_key }}
      maven_args: ${{ inputs.maven_args }}
```

**Required org secrets:** `SONAR_HOST_URL`, `SONAR_TOKEN`

---

## Notes & Recommendations

* **Secrets at org level:** `AI_REVIEW_CODEX`, `SONAR_HOST_URL`, `SONAR_TOKEN`.
* **Permissions:** prefer `permissions: inherit` and configure least-privilege per repo.
* **Ruleset:** set target branches to `master` and `main`; require the status check from the staging gate job.
* **Versioning:** once stable, tag this repo and switch callers to `@v1`.

---

## Change Log

* Added: `ai-code-review.yml`, `check-commits-in-staging.yml`, `sonar-java.yml` (linked to DEVOPS-2719).

---

## Maintainers

* Engineering Productivity / DevOps @ Eshopbox
