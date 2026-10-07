# AWS GitHub Self-Hosted Runners — RCA & Recovery Runbook

This document contains common RCA and manual recovery steps for Eshopbox GitHub self-hosted runners running on AWS.

---

## 1. AWS Runner Infrastructure

### EC2 Console

[Open AWS Runner Instances](https://ap-south-1.console.aws.amazon.com/ec2/home?region=ap-south-1#Instances:instanceState=running;search=:Runner;v=3;$case=tags:true%5C,client:false;$regex=tags:false%5C,client:false)

### Backend Runner EC2

Instance:

```text
ESHOPBOX-GITHUB-RUNNER-BACKEND
i-0d1f9a46b790ad9d5
```

[Connect using Session Manager](https://ap-south-1.console.aws.amazon.com/systems-manager/session-manager/i-0d1f9a46b790ad9d5?region=ap-south-1#:)

Runners:

```text
aws-default-runner
aws-backend-github-runner-2
aws-backend-github-runner-3
```

Runner directories:

```text
/home/ec2-user/actions-runner
/home/ec2-user/aws-backend-github-runner-2/actions-runner
/home/ec2-user/aws-backend-github-runner-3/actions-runner
```

Services:

```text
actions.runner.Eshopbox-Enginnering.aws-default-runner.service
actions.runner.Eshopbox-Enginnering.aws-backend-github-runner-2.service
actions.runner.Eshopbox-Enginnering.aws-backend-github-runner-3.service
```

Backend disk layout:

```text
/                 -> 8 GB root disk
/var/lib/docker   -> 100 GB disk
```

Runner `_work` directories are stored on the 100 GB disk:

```text
/var/lib/docker/github-runner-work/aws-default-runner
/var/lib/docker/github-runner-work/aws-backend-github-runner-2
/var/lib/docker/github-runner-work/aws-backend-github-runner-3
```

The original runner locations contain symlinks:

```text
/home/ec2-user/actions-runner/_work
  -> /var/lib/docker/github-runner-work/aws-default-runner

/home/ec2-user/aws-backend-github-runner-2/actions-runner/_work
  -> /var/lib/docker/github-runner-work/aws-backend-github-runner-2

/home/ec2-user/aws-backend-github-runner-3/actions-runner/_work
  -> /var/lib/docker/github-runner-work/aws-backend-github-runner-3
```

---

### Frontend + Checks Runner EC2

Instance:

```text
ESHOPBOX-GITHUB-RUNNER-FRONTEND-CHECKS
i-0546cdd286d1f832d
```

[Connect using Session Manager](https://ap-south-1.console.aws.amazon.com/systems-manager/session-manager/i-0546cdd286d1f832d?region=ap-south-1#:)

Runners:

```text
aws-frontend-github-runner
aws-checks-github-runner
```

Runner directories:

```text
/home/ec2-user/aws-frontend-github-runner
/home/ec2-user/aws-checks-github-runner/actions-runner
```

Services:

```text
actions.runner.Eshopbox-Enginnering.aws-frontend-github-runner.service
actions.runner.Eshopbox-Enginnering.aws-checks-github-runner.service
```

Runner groups:

```text
frontend-runners
checks-runners
```

---

# 2. Quick Health Check

Run this first during any runner issue:

```bash
echo "===== DISK ====="
df -h /

echo
echo "===== RUNNERS ====="
systemctl list-units --type=service | grep actions.runner

echo
echo "===== LISTENERS ====="
ps -ef | grep Runner.Listener | grep -v grep || echo "No listeners"

echo
echo "===== ACTIVE JOBS ====="
pgrep -af Runner.Worker || echo "No active jobs"

echo
echo "===== FAILED SERVICES ====="
systemctl --failed --no-pager
```

For backend also check the 100 GB disk:

```bash
df -h / /var/lib/docker
```

---

# 3. Disk Usage 100% / Runner Offline

A full root disk can cause:

- GitHub runner to go offline
- runner logs to fail
- jobs to fail unexpectedly
- SSM/session problems
- runner service startup failures

Check:

```bash
df -h /
```

For backend:

```bash
df -h / /var/lib/docker
```

Find large root directories:

```bash
sudo du -xhd1 / 2>/dev/null | sort -h | tail
```

If `/home` is large:

```bash
sudo du -xhd2 /home 2>/dev/null | sort -h | tail -15
```

Do not run large unrestricted recursive commands unless required.

---

# 4. Check Runner Workspace Usage

Backend:

```bash
du -sh \
  /var/lib/docker/github-runner-work/* 2>/dev/null
```

Check individual workspace directories:

```bash
du -sh /var/lib/docker/github-runner-work/*/* 2>/dev/null | sort -h | tail -15
```

Before manually deleting workspace data, always check:

```bash
pgrep -af Runner.Worker || echo "No active jobs"
```

Do **not** manually clean a runner workspace while a job is active.

---

# 5. Automated Cleanup

## Backend

Script:

```text
/opt/github-runner-maintenance/cleanup.sh
```

Run manually:

```bash
sudo /opt/github-runner-maintenance/cleanup.sh
```

Schedule:

```text
Every 15 minutes
```

Behavior:

- If any `Runner.Worker` exists, workspace and Docker cleanup is skipped.
- `_work/_temp` older than 1 day is removed.
- stale repository workspaces older than 1 day are removed.
- `_actions` content older than 3 days is removed.
- Docker builder/system prune is performed.
- `_diag` logs older than 3 days are removed.
- DNF cache is cleaned.
- journal logs older than 7 days are vacuumed.
- root disk usage is checked.

Disk thresholds:

```text
>= 75%  WARNING
>= 85%  CRITICAL
```

Cleanup log:

```bash
sudo tail -20 /var/log/backend-runner-cleanup.log
```

---

## Frontend / Checks

Run:

```bash
sudo /opt/github-runner-maintenance/cleanup.sh
```

Log:

```bash
sudo tail -20 /var/log/frontend-checks-runner-cleanup.log
```

---

# 6. Self-Heal

Self-heal script:

```text
/usr/local/bin/runner-self-heal.sh
```

Run manually:

```bash
sudo /usr/local/bin/runner-self-heal.sh
```

The script checks:

1. runner systemd service is active
2. corresponding `Runner.Listener` process exists
3. unhealthy runner service is restarted

---

## Backend Expected Output

```text
aws-default-runner.service healthy
aws-backend-github-runner-2.service healthy
aws-backend-github-runner-3.service healthy
```

Backend log:

```bash
sudo tail -20 /var/log/backend-runner-self-heal.log
```

---

## Frontend / Checks Expected Output

```text
aws-frontend-github-runner.service healthy
aws-checks-github-runner.service healthy
```

Log:

```bash
sudo tail -20 /var/log/frontend-checks-runner-self-heal.log
```

---

# 7. Runner Offline in GitHub

First check service:

```bash
systemctl list-units --type=service | grep actions.runner
```

Then listener:

```bash
ps -ef | grep Runner.Listener | grep -v grep
```

Run self-heal:

```bash
sudo /usr/local/bin/runner-self-heal.sh
```

If still offline, inspect the service log:

```bash
sudo journalctl -u <RUNNER_SERVICE> -n 40 --no-pager
```

Example:

```bash
sudo journalctl \
  -u actions.runner.Eshopbox-Enginnering.aws-default-runner.service \
  -n 40 --no-pager
```

Look for:

```text
Connected to GitHub
Listening for Jobs
```

---

# 8. Runner Service Active But GitHub Shows Offline

Check listener:

```bash
ps -ef | grep Runner.Listener | grep -v grep
```

If listener is missing:

```bash
sudo systemctl restart <RUNNER_SERVICE>
```

Then:

```bash
sudo journalctl -u <RUNNER_SERVICE> -n 30 --no-pager
```

---

# 9. Backend `_work` Permission Failure

Backend workspaces are on:

```text
/var/lib/docker/github-runner-work
```

If logs contain:

```text
Fail to create and validate runner's work directory
```

check permissions:

```bash
namei -l /var/lib/docker/github-runner-work/aws-default-runner
```

Test as runner user:

```bash
sudo -u ec2-user touch \
  /var/lib/docker/github-runner-work/aws-default-runner/testfile
```

If `/var/lib/docker` prevents traversal, check ACL:

```bash
getfacl /var/lib/docker
```

The runner user requires traverse access.

Configured fix:

```bash
sudo setfacl -m u:ec2-user:--x /var/lib/docker
```

Test again:

```bash
sudo -u ec2-user touch \
  /var/lib/docker/github-runner-work/aws-default-runner/testfile

sudo rm -f \
  /var/lib/docker/github-runner-work/aws-default-runner/testfile
```

Do **not** broadly change `/var/lib/docker` permissions to `777`.

---

# 10. Docker Disk Usage

Backend Docker has its own 100 GB disk.

Check:

```bash
df -h /var/lib/docker
sudo docker system df
```

Before cleanup:

```bash
pgrep -af Runner.Worker || echo "No active jobs"
```

If no jobs are running:

```bash
sudo docker builder prune -af
sudo docker system prune -af
```

Then:

```bash
df -h /var/lib/docker
```

---

# 11. Temporarily Stop Runners

IMPORTANT: self-heal runs every 5 minutes.

If you manually stop a runner while cron remains active, self-heal may start it again.

Before maintenance:

```bash
sudo systemctl stop crond
```

Confirm:

```bash
systemctl is-active crond
```

Then stop runner services.

Backend:

```bash
sudo systemctl stop actions.runner.Eshopbox-Enginnering.aws-default-runner.service
sudo systemctl stop actions.runner.Eshopbox-Enginnering.aws-backend-github-runner-2.service
sudo systemctl stop actions.runner.Eshopbox-Enginnering.aws-backend-github-runner-3.service
```

Verify:

```bash
pgrep -af Runner.Worker || echo "No active jobs"
ps -ef | grep Runner.Listener | grep -v grep || echo "No listeners"
```

After maintenance:

```bash
sudo systemctl start actions.runner.Eshopbox-Enginnering.aws-default-runner.service
sudo systemctl start actions.runner.Eshopbox-Enginnering.aws-backend-github-runner-2.service
sudo systemctl start actions.runner.Eshopbox-Enginnering.aws-backend-github-runner-3.service

sudo systemctl start crond
```

Then:

```bash
sudo /usr/local/bin/runner-self-heal.sh
```

---

# 12. Cron Validation

Check:

```bash
sudo crontab -l
systemctl is-active crond
```

## Backend

Expected:

```cron
MAILTO=""

*/15 * * * * /usr/bin/flock -n /var/lock/backend-runner-cleanup.lock /opt/github-runner-maintenance/cleanup.sh >> /var/log/backend-runner-cleanup.log 2>&1
*/5 * * * * /usr/bin/flock -n /var/lock/backend-runner-self-heal.lock /usr/local/bin/runner-self-heal.sh >> /var/log/backend-runner-self-heal.log 2>&1
```

## Frontend / Checks

Expected:

```cron
MAILTO=""

*/15 * * * * /usr/bin/flock -n /var/lock/frontend-checks-runner-cleanup.lock /opt/github-runner-maintenance/cleanup.sh >> /var/log/frontend-checks-runner-cleanup.log 2>&1
*/5 * * * * /usr/bin/flock -n /var/lock/frontend-checks-runner-self-heal.lock /usr/local/bin/runner-self-heal.sh >> /var/log/frontend-checks-runner-self-heal.log 2>&1
```

---

# 13. Log Rotation

Backend:

```text
/etc/logrotate.d/backend-github-runner
```

Frontend/checks:

```text
/etc/logrotate.d/frontend-checks-github-runner
```

Validate:

```bash
sudo logrotate -d /etc/logrotate.d/backend-github-runner
```

or:

```bash
sudo logrotate -d /etc/logrotate.d/frontend-checks-github-runner
```

Logs rotate:

```text
weekly
8 rotations
compressed
```

---

# 14. Check Which Runner Is Executing a Job

```bash
pgrep -af Runner.Worker
```

Then:

```bash
ps -ef | grep -E 'Runner.Listener|Runner.Worker' | grep -v grep
```

GitHub job UI also displays:

```text
Runner name
Runner group
Machine name
```

Backend jobs should execute on one of:

```text
aws-default-runner
aws-backend-github-runner-2
aws-backend-github-runner-3
```

Frontend jobs should execute on:

```text
aws-frontend-github-runner
```

Checks jobs should execute on:

```text
aws-checks-github-runner
```

---

# 15. GitHub Job Waiting for Runner

Check locally:

```bash
sudo /usr/local/bin/runner-self-heal.sh
```

Then verify listener:

```bash
ps -ef | grep Runner.Listener | grep -v grep
```

For frontend/checks, also verify that the workflow requests the correct runner group/label.

Frontend:

```yaml
runs-on:
  group: frontend-runners
  labels: frontend-runner
```

Checks:

```yaml
runs-on:
  group: checks-runners
  labels: checks-runner
```

---

# 16. Docker Hub / External Registry Failure

Example:

```text
500 Internal Server Error
registry-1.docker.io
tonistiigi/binfmt
```

Test directly:

```bash
docker pull tonistiigi/binfmt:latest
```

Registry connectivity:

```bash
curl -I https://registry-1.docker.io/v2/
```

A `401 Unauthorized` response from `/v2/` is normal and confirms the registry is reachable.

If direct pull succeeds and retry succeeds, treat the original failure as likely transient registry/network failure rather than immediately modifying the runner.

---

# 17. Application Build Failure vs Runner Failure

Do not classify every failed GitHub Action as a runner issue.

Examples of application failures:

```text
Maven compilation error
cannot find symbol
missing dependency
unit test failure
lint failure
application build failure
```

Runner/infra failures are more likely to contain:

```text
No space left on device
Runner.Listener exited
work directory validation failed
Docker daemon unavailable
permission denied
network/registry connection failure
runner offline
```

Always identify the exact failed step before changing runner infrastructure.

---

# 18. DO NOT DELETE

Never delete these from an active GitHub runner installation:

```text
.runner
.credentials
.credentials_rsaparams
bin/
externals/
runsvc.sh
svc.sh
```

In particular:

```text
bin.*
externals.*
```

may be required during GitHub Actions runner updates.

Do not delete an entire runner installation just to free disk space.

Prefer cleaning:

```text
_work
_work/_temp
old repository workspaces
old _actions content
_diag logs
Docker cache/images
package cache
```

---

# 19. Emergency Disk Recovery

If root reaches ~100% and services are failing:

### Step 1 — Check active jobs

```bash
pgrep -af Runner.Worker || echo "No active jobs"
```

### Step 2 — Check disk

```bash
df -h / /var/lib/docker
```

### Step 3 — Find largest root directories

```bash
sudo du -xhd1 / 2>/dev/null | sort -h | tail
```

### Step 4 — Run standard cleanup

If there are no active jobs:

```bash
sudo /opt/github-runner-maintenance/cleanup.sh
```

### Step 5 — Check Docker

```bash
sudo docker system df
```

If no job is running:

```bash
sudo docker builder prune -af
sudo docker system prune -af
```

### Step 6 — Check disk again

```bash
df -h / /var/lib/docker
```

### Step 7 — Validate runners

```bash
sudo /usr/local/bin/runner-self-heal.sh
```

---

# 20. Complete RCA Snapshot

Use this when sharing an issue with the team:

```bash
echo "===== HOST ====="
hostname

echo
echo "===== DISK ====="
df -h / /var/lib/docker 2>/dev/null

echo
echo "===== RUNNERS ====="
systemctl list-units --type=service | grep actions.runner

echo
echo "===== LISTENERS ====="
ps -ef | grep Runner.Listener | grep -v grep || echo "No listeners"

echo
echo "===== ACTIVE JOBS ====="
pgrep -af Runner.Worker || echo "No active jobs"

echo
echo "===== FAILED SERVICES ====="
systemctl --failed --no-pager

echo
echo "===== CLEANUP LOG ====="
sudo tail -10 /var/log/*runner-cleanup.log 2>/dev/null

echo
echo "===== SELF HEAL LOG ====="
sudo tail -10 /var/log/*runner-self-heal.log 2>/dev/null
```

This intentionally keeps output short because runner logs can be large.

---

# 21. Normal Healthy State

Backend:

```bash
sudo /usr/local/bin/runner-self-heal.sh
df -h / /var/lib/docker
```

Expected:

```text
aws-default-runner healthy
aws-backend-github-runner-2 healthy
aws-backend-github-runner-3 healthy
```

Frontend/checks:

```bash
sudo /usr/local/bin/runner-self-heal.sh
df -h /
```

Expected:

```text
aws-frontend-github-runner healthy
aws-checks-github-runner healthy
```

If all runner services/listeners are healthy and disk usage is within limits, no manual action is required.
