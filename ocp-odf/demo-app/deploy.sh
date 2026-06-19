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

require_env EXT_NAME GITHUB_USER GITHUB_TOKEN
KUBECONFIG="$(cluster_kubeconfig "${EXT_NAME}")"; export KUBECONFIG
[[ -r "${KUBECONFIG}" ]] || fail "kubeconfig for ${EXT_NAME} not found — run Phase 1"
require_cli oc curl
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

# 2. Everything else (PVCs, OBC, ImageStream, BuildConfig, Deployment, Svc, Route).
info "Applying manifests..."
oc apply -k "${MANIFESTS}" >/dev/null
ok "Manifests applied"

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
