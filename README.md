# Eshopbox Org-Level Actions & Reusable Workflows

> Jira: [DEVOPS-2719](https://auperator.atlassian.net/browse/DEVOPS-2719)

Centralized repository hosting reusable **GitHub Actions** and **workflows** for the Eshopbox organization. Use these to ensure consistent automation, faster onboarding, and easier maintenance across all repositories.

---

## Repository Structure

```
.github/                     # org-level repo named exactly ".github"
└─ workflows/                # reusable workflows (called via workflow_call)
   ├─ ai-code-review.yml
   ├─ check-commits-in-staging.yml
   └─ sonar-java.yml
└─ CODEOWNERS
└─ README.md
```

---

## Available Workflows

### 1) Codex AI Code Review

* Runs AI-powered review on pull requests.
* Skips when no relevant changes are detected under configured source directories.
* Requires org secret: `AI_REVIEW_CODEX` (OpenAI API key).

### 2) Check Commits in Staging

* Ensures all commits going into `master` or `main` are already present in `staging`.
* Prevents bypassing the staging branch.

### 3) Sonar Java

* Runs a SonarQube scan for Java projects.
* Uses Java 17 and Maven with dependency caching.
* Requires org secrets: `SONAR_HOST_URL`, `SONAR_TOKEN`.

---

## Permissions & Security

* Use `permissions: inherit` in caller workflows and configure least-privilege at the repo level.
* Store required secrets once at the **org level** (`AI_REVIEW_CODEX`, `SONAR_TOKEN`, `SONAR_HOST_URL`).
* Restrict who can edit this org-level repo since changes impact all consuming repos.

---

## Versioning Guidance

* Pin to a stable tag (e.g., `@v1`) once workflows stabilize.
* Use branch protection and required checks to enforce adoption across repos.

---

## Change Log

* Added: `ai-code-review.yml`, `check-commits-in-staging.yml`, `sonar-java.yml` (linked to DEVOPS-2719).

---

## Maintainers

* Engineering Productivity / DevOps @ Eshopbox
