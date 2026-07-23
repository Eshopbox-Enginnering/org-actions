GitHub Checks Runner – Root Cause and Fix

Overview

A dedicated self-hosted GitHub Actions runner was created for lightweight CI validation jobs such as PR title validation, commit checks, test-file validation, linting, and AI review workflows.

VM Name: checks-github-runnerProject: eshopbox-common-devopsZone: asia-south1-b

VM Console:https://console.cloud.google.com/compute/instancesDetail/zones/asia-south1-b/instances/checks-github-runner?project=eshopbox-common-devops

Problem Statement

Lightweight validation workflows were initially running on the frontend self-hosted runner because a separate runner group for check workflows was not available.

Examples:

validate-pr-title-and-ticket

check-commits

validate-tests

These jobs do not require the frontend build runner and could consume its execution capacity, delaying frontend build or deployment jobs.

Root Cause

The workflows were configured to use the existing frontend runner or a common self-hosted runner configuration.

At the time those jobs ran, the dedicated checks-runners group had not yet been created and assigned. Therefore:

PR validation jobs were picked up by the frontend runner.

Frontend deployment and validation workloads were not isolated.

Lightweight checks could occupy the frontend runner unnecessarily.

Failed validation results appeared in frontend runner logs.

The Failed status represented workflow validation failures, not a VM or runner-service failure.

Fix Implemented

A dedicated self-hosted runner was provisioned and registered for check-related workflows.

Runner Configuration

Runner name: checks-github-runner

Runner group: checks-runners

Custom label: checks-runner

OS: Linux

Architecture: x64

Service: systemd

Startup: Enabled on VM boot

Workflow Configuration

Applicable check workflows should use:

runs-on:
  group: checks-runners
  labels: checks-runner

Use this for:

PR title and ticket validation

Commit validation

Test-file validation

Lint and static checks

AI code review

Organization-level reusable validation workflows

Frontend build and deployment workflows should continue using the dedicated frontend runner.

Validation Performed

Service status

sudo systemctl status 'actions.runner.*' --no-pager

Expected:

Service is active (running)

Service is enabled

Runner is connected and waiting for jobs

Runner registration

Verified in GitHub organization settings that:

Runner is online

Runner belongs to checks-runners

Runner has label checks-runner

Status returns to Idle after jobs complete

Workflow execution

Verified that new check jobs use:

Runner: checks-github-runner

Runner group: checks-runners

Resource health

free -h
df -h
ps aux --sort=-%mem | head -20

Confirmed:

No memory pressure

No swap usage

Sufficient disk space

No stale Node, npm, Angular, Firebase, or Java processes

Verification Commands

Check runner service

systemctl list-units --type=service | grep actions.runner
sudo systemctl status 'actions.runner.*' --no-pager

Check recent jobs

sudo journalctl \
  -u actions.runner.Eshopbox-Enginnering.checks-github-runner.service \
  -n 100 \
  --no-pager

Check runner errors

sudo journalctl \
  -u actions.runner.Eshopbox-Enginnering.checks-github-runner.service \
  --since "24 hours ago" \
  --no-pager \
  | grep -Ei "error|failed|exception|offline|terminated|out of memory"

A job may report Failed when a PR validation rule fails. This does not mean the runner service is unhealthy.

Check system health

free -h
df -h
sudo systemctl --failed

Check for OOM or kernel errors

sudo journalctl -k --since "7 days ago" --no-pager \
  | grep -Ei "out of memory|oom|killed process|segfault|I/O error"

Expected Outcome

After migration:

Lightweight checks run only on checks-github-runner.

Frontend builds and deployments remain on frontend-github-runner.

PR checks no longer consume frontend deployment capacity.

Validation and deployment workloads are isolated.

Workflow failures are clearly separated from infrastructure failures.

Rollback

If the checks runner becomes unavailable, temporarily revert the workflow runs-on configuration to the previous runner.

This should only be a temporary fallback because it removes workload isolation and may delay frontend deployments.

Current Status

The dedicated checks runner has been created and configured. All applicable validation workflows should target:

runs-on:
  group: checks-runners
  labels: checks-runner

Monitor both runners for the next few workflow runs to confirm that validation jobs no longer appear on the frontend runner.
