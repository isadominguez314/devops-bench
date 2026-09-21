#!/usr/bin/env bash
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Destroy-time companion to setup.sh. The frontend Services created there make
# the GKE cloud controller provision regional forwarding rules, target pools
# and k8s-fw-* firewall rules outside the Terraform state; deleting the
# clusters does not delete them, and while a forwarding rule still holds a
# reserved address the google_compute_address destroy is rejected ("resource
# in use"), which leaks every network-LB resource and blocks a re-apply.
# Delete the Services first so the controller cleans up after itself, wait for
# the reserved IPs to be released, then sweep whatever is left. Every lookup
# is filtered on this run's two reserved IP values, so a concurrent run's
# resources cannot be selected. Runs under on_failure = continue: best effort,
# never blocks the rest of the destroy.
set -uo pipefail # deliberately no -e: each step is best-effort

: "${PROJECT_ID:?}" "${NAMESPACE:?}"
: "${EAST_CLUSTER:?}" "${EAST_ZONE:?}" "${WEST_CLUSTER:?}" "${WEST_ZONE:?}"
: "${EAST_IP:?}" "${WEST_IP:?}"

# The provisioner-local kubeconfig may have been reshaped since setup (the
# agent owns the run's ambient one); re-credential into a scratch file.
SCRATCH_KUBECONFIG="$(mktemp)"
trap 'rm -f "$SCRATCH_KUBECONFIG"' EXIT
export KUBECONFIG="$SCRATCH_KUBECONFIG"

delete_frontend_svc() {
  local cluster="$1" zone="$2"
  echo "==> [teardown] deleting frontend Service on ${cluster}"
  if gcloud container clusters get-credentials "$cluster" --zone "$zone" \
    --project "$PROJECT_ID" >/dev/null 2>&1; then
    kubectl --context "gke_${PROJECT_ID}_${zone}_${cluster}" -n "$NAMESPACE" \
      delete svc frontend --ignore-not-found --timeout=90s || true
  else
    echo "==> [teardown] ${cluster} unreachable; relying on the sweep below"
  fi
}

delete_frontend_svc "$EAST_CLUSTER" "$EAST_ZONE"
delete_frontend_svc "$WEST_CLUSTER" "$WEST_ZONE"

list_rules() {
  gcloud compute forwarding-rules list --project "$PROJECT_ID" \
    --filter="IPAddress=(${EAST_IP} ${WEST_IP})" \
    --format="$1" 2>/dev/null || true
}

# The controller releases the reserved IPs only once its NLB teardown
# finishes; google_compute_address can be destroyed after that.
echo "==> [teardown] waiting for forwarding rules on ${EAST_IP} / ${WEST_IP} to clear"
remaining=""
for _ in $(seq 1 18); do
  remaining="$(list_rules 'value(name)')"
  if [[ -z "$remaining" ]]; then
    echo "==> [teardown] reserved IPs released"
    exit 0
  fi
  sleep 10
done

# The Service path did not converge (cluster already gone, controller wedged).
# The controller names the forwarding rule, target pool and k8s-fw firewall
# rule after the Service's UID, so the rule name selects its companions.
echo "==> [teardown] sweeping leftover NLB resources: ${remaining}"
while IFS=, read -r name region; do
  [[ -z "$name" ]] && continue
  gcloud compute forwarding-rules delete "$name" --region "$region" \
    --project "$PROJECT_ID" --quiet || true
  gcloud compute target-pools delete "$name" --region "$region" \
    --project "$PROJECT_ID" --quiet 2>/dev/null || true
  while read -r fw; do
    [[ -z "$fw" ]] && continue
    gcloud compute firewall-rules delete "$fw" --project "$PROJECT_ID" --quiet || true
  done < <(gcloud compute firewall-rules list --project "$PROJECT_ID" \
    --filter="name~^k8s-fw-${name}$" --format='value(name)' 2>/dev/null || true)
done < <(list_rules 'csv[no-heading](name,region.basename())')

if [[ -n "$(list_rules 'value(name)')" ]]; then
  echo "==> [teardown] WARNING: forwarding rules still present; the address destroy may fail" >&2
fi
exit 0
