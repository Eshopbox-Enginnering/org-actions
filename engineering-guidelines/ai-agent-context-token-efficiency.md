# AI Agent Context and Token Efficiency Guidelines

**Applies to:** AI coding, review, investigation, and documentation tasks in Eshopbox backend and frontend repositories.

**Purpose:** Reduce unnecessary context, tool calls, retries, and output without sacrificing correctness, security, test coverage, or required engineering workflows.

## 1. Start with the smallest reliable source

- Read the task or issue description and acceptance criteria first. Retrieve comments, linked tickets, logs, or attachments **when they contain relevant decisions or are needed**; do not scan entire threads by default.
- Inspect the repository's relevant `AGENTS.md` and existing documentation/index before searching code. Follow applicable repository-local engineering guidelines.
- Identify the affected module or feature first. Read only likely entry points and dependencies; widen the search when a specific dependency or unresolved question requires it.
- When changing a shared function, API, type, class, or state selector, find its relevant callers before editing. Prefer a targeted symbol search; widen coverage when usage or potential impact is unclear.
- Avoid repeating files already examined unless the content has changed or a new question requires rereading them.

## 2. Keep searches and output focused

- Prefer filename/path discovery, scoped `rg -n` or `rg -l`, and exact line ranges over dumping whole files or entire trees.
- Inspect changed-file lists or `git diff --stat` before full diffs; expand only the hunks relevant to the task.
- Cap logs and test output where practical: keep complete raw output in an artifact/file when necessary, but surface the actionable errors and a short tail.
- Use targeted calls and structured results. Batch independent lookups when supported, without hiding failures.
- Exclude generated, vendored, or build artifacts from broad searches unless they are the subject of the task.

## 3. Plan and implement with bounded context

- Keep the working plan short, listing the task, known constraints, affected locations, and verification steps; do not continuously restate full background.
- Favor the smallest change that satisfies the acceptance criteria. Do not perform unrelated refactoring, style sweeps, audits, or broad rewrites unless requested or required for a safe fix.
- Preserve working code, API contracts, existing tests, existing documentation, and repository-specific instructions.
- If requirements are ambiguous and the choice materially changes behavior, raise the precise decision rather than repeatedly exploring speculative approaches.

## 4. Test efficiently without weakening verification

- Follow the repository's existing test-first and CI requirements. For bugs, verify the regression is reproducible where practical before fixing it.
- During development, run the relevant test file/class/suite after incremental changes; run broader tests/build once when the final change is ready, **or whenever project policy and risk require it**.
- Do not skip, disable, weaken, or delete tests just to reduce token usage or to obtain a green result.
- For a missing external dependency, report the limitation and avoid repeating an identical failing run unless the environment or test conditions changed. Run independent verifications when safe, but do not mark blocked tests as passing.
- Cite the actual tests, commands, and results; never invent execution results.

## 5. Review and communicate concisely

- Review code against the issue, touched behavior, callers, tests, applicable guidelines, and security implications. Focus findings on verifiable regressions and actionable changes.
- Include file/line, impact, and a concrete recommendation when possible. Avoid duplicate review findings or speculative unrelated issues.
- In PR descriptions and final summaries, report **what changed, why, verification results, and remaining blockers**. Link to the source material instead of copying entire logs or documents.
- Keep a concise handoff of important decisions and unresolved concerns; don't paste extensive earlier context into each new step.

## 6. Safety and instruction precedence

- Token efficiency is subordinate to correctness, security, privacy, repository-level instructions, acceptance criteria, required reviews, and CI checks.
- Never avoid reviewing additional relevant files, comments, tests, logs, or dependencies merely to save tokens when those are necessary to understand or safely complete the task.
- Use the approved authentication, branch, PR, and deployment workflows. This document **does not replace** them or authorize skipping approval requirements.
- Never print credentials or secrets in prompts, tool output, example curls, or reports.

**Reference:** [Central Backlog #7368](https://github.com/Eshopbox-Enginnering/Central-Backlog/issues/7368).
