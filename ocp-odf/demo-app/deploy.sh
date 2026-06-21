#!/usr/bin/env bash
# Phase 4 — deploy the Ceph external storage demo app to ocp-external.
# Builds the image in-cluster from the GitHub repo, then deploys + exposes it.
#
#   source env.sh && ./demo-app/deploy.sh
#
# Idempotent. Re-running rebuilds and rolls out the latest.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../ocp-odf/demo-app
OCPODF="$(cd "${HERE}/.." && pwd)"                       # .../ocp-odf
# shellcheck source=../lib.sh
source "${OCPODF}/lib.sh"

require_env EXT_NAME GITHUB_USER GITHUB_TOKEN CEPH_API_PASSWORD
KUBECONFIG="$(cluster_kubeconfig "${EXT_NAME}")"; export KUBECONFIG
[[ -r "${KUBECONFIG}" ]] || fail "kubeconfig for ${EXT_NAME} not found — run Phase 1"
require_cli oc curl aws
NS=ceph-demo
MANIFESTS="${HERE}/manifests"

# 1. Namespace first, then the git source secret (so the build has creds before
#    the BuildConfig is created).
info "Ensuring namespace ${NS}..."
oc apply -f "${MANIFESTS}/00-namespace.yaml" >/dev/null

info "Creating GitHub source secret..."
oc create secret generic github-source-secret -n "${NS}" \
  --type=kubernetes.io/basic-auth \
  --from-literal=username="${GITHUB_USER}" \
  --from-literal=password="${GITHUB_TOKEN}" \
  --dry-run=client -o yaml | oc apply -f - >/dev/null

info "Creating Ceph dashboard API secret (for self-service provisioning)..."
oc create secret generic ceph-dashboard-creds -n "${NS}" \
  --from-literal=password="${CEPH_API_PASSWORD}" \
  --dry-run=client -o yaml | oc apply -f - >/dev/null

# 2. Everything else (PVCs, OBC, ImageStream, BuildConfig, Deployment, Svc, Route).
info "Applying manifests..."
oc apply -k "${MANIFESTS}" >/dev/null
ok "Manifests applied"

# If the RGW NLB (phase 07) exists, point the app's S3 endpoint at it for HA;
# otherwise the manifest default (a single rgw node) stands.
nlb_ip() {  # echo the private IP of an internal NLB by name, or empty
  aws elbv2 describe-load-balancers --names "$1" >/dev/null 2>&1 || return 0
  aws ec2 describe-network-interfaces \
    --filters "Name=description,Values=ELB net/$1/*" \
    --query 'NetworkInterfaces[0].PrivateIpAddress' --output text 2>/dev/null
}
rgw_nlb_ip="$(nlb_ip ceph9-rgw-nlb)"
if [[ -n "${rgw_nlb_ip}" && "${rgw_nlb_ip}" != "None" ]]; then
  oc -n "${NS}" set env deploy/ceph-demo-app "CEPH_RGW_ENDPOINT=${rgw_nlb_ip}:8080" >/dev/null
  ok "S3 endpoint  -> RGW NLB ${rgw_nlb_ip}:8080 (HA)"
fi
nfs_nlb_ip="$(nlb_ip ceph9-nfs-nlb)"
if [[ -n "${nfs_nlb_ip}" && "${nfs_nlb_ip}" != "None" ]]; then
  oc -n "${NS}" set env deploy/ceph-demo-app "CEPH_NFS_SERVER=${nfs_nlb_ip}:2049" >/dev/null
  ok "NFS endpoint -> NFS NLB ${nfs_nlb_ip}:2049 (HA)"
fi

# 3. Build the image from GitHub.
info "Starting build from ${GITHUB_USER}/ceph9 (contextDir ocp-odf/demo-app)..."
build=$(oc -n "${NS}" start-build ceph-demo-app -o name)   # e.g. build/ceph-demo-app-1
info "Build: ${build} — streaming (image build ~3-5 min)..."
oc -n "${NS}" logs -f "${build}" 2>/dev/null | tail -8 || true
phase=$(oc -n "${NS}" get "${build}" -o jsonpath='{.status.phase}' 2>/dev/null)
[[ "${phase}" == "Complete" ]] || fail "build ${build} phase=${phase} — see: oc -n ${NS} logs ${build}"
ok "Build Complete"

# 4. Roll out the app.
info "Waiting for deployment rollout..."
oc -n "${NS}" rollout status deploy/ceph-demo-app --timeout=300s

# 5. ObjectBucketClaim (object endpoint) — bind check (non-fatal).
info "Checking ObjectBucketClaim (object endpoint)..."
st=""
for _ in $(seq 1 36); do
  st=$(oc -n "${NS}" get obc ceph-demo-bucket -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "${st}" == "Bound" ]] && break; sleep 5
done
[[ "${st}" == "Bound" ]] && ok "OBC Bound (object endpoint ready)" \
  || warn "OBC phase=${st:-<none>} — block/file work; object endpoint will activate once bound"

# 6. Route + health.
host=$(oc -n "${NS}" get route ceph-demo-app -o jsonpath='{.spec.host}')
info "Health check https://${host}/healthz ..."
for _ in $(seq 1 20); do
  code=$(curl -ksS -o /dev/null -w '%{http_code}' "https://${host}/healthz" || true)
  [[ "${code}" == "200" ]] && break; sleep 5
done
[[ "${code}" == "200" ]] || fail "health check failed (HTTP ${code})"

echo
ok "Demo app is up:  https://${host}"
ok "Storage endpoints: Block (RBD) · File (CephFS) · Object (RGW S3) — all backed by ceph9"
