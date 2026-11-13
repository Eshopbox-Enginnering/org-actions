#!/usr/bin/env bash
# Verify that the new container image has become Ready, and explain failures.
# Usage: verify-eks-rollout.sh <namespace> <helm-release> <image> <timeout-seconds>
set -Eeuo pipefail

namespace="${1:?namespace required}"
release="${2:?Helm release required}"
expected_image="${3:?expected image required}"
timeout="${4:-300}"
report="${GITHUB_STEP_SUMMARY:-/dev/null}"
deployment=""
reason="Unknown"
owner="Infrastructure/DevOps and application team"
action="Review the deployment events and pod diagnostics below."

add_summary() { printf '%s\n' "$*" >> "$report"; }
notice() { echo "$*"; }
fail_diagnostics() {
  local exit_code=$?
  trap - ERR EXIT
  if [[ "$exit_code" -eq 0 ]]; then return; fi
  echo ""
  echo "==================== EKS ROLLOUT FAILED ===================="
  echo "Release:     ${release}"
  echo "Reason:      ${reason}"
  echo "Owner:       ${owner}"
  echo "Next action: ${action}"
  echo "Deployment:  ${namespace}/${deployment:-not discovered}"
  echo "Expected:    ${expected_image}"
  echo "::error title=EKS rollout failed::${reason}. ${action}"
  add_summary "## ❌ EKS rollout failed"
  add_summary ""
  add_summary "| Field | Details |"
  add_summary "| --- | --- |"
  add_summary "| Deployment | ${namespace}/${deployment:-not discovered} |"
  add_summary "| Failure | **${reason}** |"
  add_summary "| Owner | ${owner} |"
  add_summary "| Next step | ${action} |"
  add_summary "| Expected image | ${expected_image} |"
  if [[ -z "$deployment" ]]; then return "$exit_code"; fi

  local counts selector pod events
  counts="$(kubectl get deployment "$deployment" -n "$namespace" -o jsonpath='{.spec.replicas}{" desired, "}{.status.updatedReplicas}{" updated, "}{.status.readyReplicas}{" ready, "}{.status.availableReplicas}{" available"}' 2>/dev/null || true)"
  echo "Replicas:    ${counts:-unavailable}"
  add_summary "| Replicas | ${counts:-unavailable} |"
  add_summary ""
  add_summary "### Pod status and warnings"
  add_summary "Check the failed step for the affected pods and their recent Warning events."

  selector="$(kubectl get deployment "$deployment" -n "$namespace" -o go-template='{{range $k,$v := .spec.selector.matchLabels}}{{printf "%s=%s," $k $v}}{{end}}' 2>/dev/null || true)"
  selector="${selector%,}"
  if [[ -z "$selector" ]]; then
    echo "No pod selector found; inspect the deployment manually."
    return "$exit_code"
  fi

  echo ""
  echo "PODS (deployment only)"
  kubectl get pods -n "$namespace" -l "$selector" -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,NODE:.spec.nodeName' 2>/dev/null || true
  echo ""
  echo "RECENT POD WARNINGS (deployment only)"
  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    events="$(kubectl get events -n "$namespace" --field-selector "involvedObject.kind=Pod,involvedObject.name=$pod,type=Warning" --sort-by='.lastTimestamp' -o custom-columns='REASON:.reason,MESSAGE:.message' --no-headers 2>/dev/null | tail -n 5 || true)"
    [[ -n "$events" ]] || continue
    echo "--- ${pod} ---"
    printf '%s\n' "$events"
  done < <(kubectl get pods -n "$namespace" -l "$selector" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
  echo ""
  echo "For deeper investigation: kubectl describe pod <pod-name> -n ${namespace}"
  return "$exit_code"
}

trap fail_diagnostics ERR EXIT

# Match the actual deployed Helm release rather than assuming Helm's chart name.
mapfile -t deployments < <(kubectl get deployments -n "$namespace" -l "app.kubernetes.io/instance=${release}" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if (( ${#deployments[@]} != 1 )); then
  # Some historical charts use custom Helm labels. Fall back only to a uniquely named deployment.
  mapfile -t deployments < <(kubectl get deployments -n "$namespace" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -Fx -e "${release}" -e "${release}-eshopbox" || true)
fi
if (( ${#deployments[@]} != 1 )); then
  reason="Unable to uniquely identify deployment for Helm release ${release}"
  action="Inspect Helm release labels and Deployment names; update the verification selector."
  exit 1
fi
deployment="${deployments[0]}"
notice "Verifying ${namespace}/${deployment}, expected image ${expected_image}"

images="$(kubectl get deployment "$deployment" -n "$namespace" -o jsonpath='{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}')"
if ! grep -Fxq "$expected_image" <<< "$images"; then
  reason="Deployment template does not reference expected image"
  action="Check Helm values and image repository/tag overrides before retrying."
  exit 1
fi

# kubectl rollout status fails on deadline, authorization failure, or incomplete rollout.
revision="$(kubectl get deployment "$deployment" -n "$namespace" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null || true)"
if [[ -z "$revision" ]]; then
  reason="Unable to read Deployment revision for rollout verification"
  action="Check Deployment metadata and Kubernetes API access."
  exit 1
fi
if ! kubectl rollout status "deployment/${deployment}" -n "$namespace" --revision="$revision" --timeout="${timeout}s"; then
  reason="Rollout timed out or failed"
  action="Inspect pod conditions and events to determine the underlying issue."
  selector="$(kubectl get deployment "$deployment" -n "$namespace" -o go-template='{{range $k,$v := .spec.selector.matchLabels}}{{printf "%s=%s," $k $v}}{{end}}' 2>/dev/null || true)"
  selector="${selector%,}"
  pods="$(kubectl get pods -n "$namespace" -l "$selector" -o json 2>/dev/null || true)"
  events=""
  while read -r pod_name; do
    [[ -n "$pod_name" ]] || continue
    events+="$(kubectl get events -n "$namespace" --field-selector "involvedObject.name=$pod_name" -o json 2>/dev/null || true)"
  done < <(kubectl get pods -n "$namespace" -l "$selector" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
  if grep -q 'FailedScheduling' <<< "$events"; then
    reason="FailedScheduling: new pod could not be assigned to a node"
    owner="Infrastructure/DevOps"
    action="Check node selectors/affinity, taints, node pod capacity, and Karpenter NodePool limits."
  elif grep -Eq 'ErrImagePull|ImagePullBackOff|Failed to pull image' <<< "$pods $events"; then
    reason="ImagePullBackOff / image pull failure"
    owner="Infrastructure/DevOps"
    action="Check ECR image/tag availability, node IAM permissions and registry connectivity."
  elif grep -Eq 'CrashLoopBackOff|OOMKilled' <<< "$pods"; then
    reason="Application crash or OOMKilled"
    owner="Application development (and Infrastructure for memory limits)"
    action="Inspect container logs, startup configuration and memory requests/limits."
  elif grep -q 'Unhealthy' <<< "$events"; then
    reason="Readiness/liveness probe failure"
    owner="Application development and Infrastructure/DevOps"
    action="Check probe endpoints, startup time, application logs, and probe thresholds."
  fi
  exit 1
fi

# Recheck the image after rollout; do not report an overlapping later release as our success.
images_after="$(kubectl get deployment "$deployment" -n "$namespace" -o jsonpath='{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}')"
revision_after="$(kubectl get deployment "$deployment" -n "$namespace" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
if [[ "$revision_after" != "$revision" ]] || ! grep -Fxq "$expected_image" <<< "$images_after"; then
  reason="Deployment was superseded by another release during verification"
  action="Check concurrent deployment runs and confirm which release should be deployed."
  exit 1
fi

# Do not accept a completed rollout with zero available replicas.
desired="$(kubectl get deployment "$deployment" -n "$namespace" -o jsonpath='{.spec.replicas}')"
ready="$(kubectl get deployment "$deployment" -n "$namespace" -o jsonpath='{.status.readyReplicas}')"
available="$(kubectl get deployment "$deployment" -n "$namespace" -o jsonpath='{.status.availableReplicas}')"
desired="${desired:-1}"; ready="${ready:-0}"; available="${available:-0}"
updated="$(kubectl get deployment "$deployment" -n "$namespace" -o jsonpath='{.status.updatedReplicas}')"
updated="${updated:-0}"
if (( desired < 1 || updated < desired || ready < desired || available < desired )); then
  reason="Rollout replicas incomplete (desired=${desired}, updated=${updated}, ready=${ready}, available=${available})"
  action="Inspect pod readiness and deployment conditions."
  exit 1
fi

trap - ERR EXIT
add_summary "### ✅ EKS rollout successful"
add_summary ""
add_summary "| Field | Value |"
add_summary "| --- | --- |"
add_summary "| Deployment | \`${namespace}/${deployment}\` |"
add_summary "| Image | \`${expected_image}\` |"
add_summary "| Replicas | ${ready}/${desired} Ready, ${available}/${desired} Available |"
add_summary "| Result | New image rolled out successfully |"
notice "Deployment healthy: ${deployment} (${ready}/${desired} Ready)"
