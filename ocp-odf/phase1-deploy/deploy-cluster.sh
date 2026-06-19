#!/usr/bin/env bash
# Phase 1 — deploy OCP cluster(s) on AWS via openshift-install (full IPI).
# Each cluster builds its OWN VPC (default IPI). No shared/BYO VPC — see README.
#
#   source env.sh
#   ./phase1-deploy/deploy-cluster.sh internal     # cluster A (ODF internal)
#   ./phase1-deploy/deploy-cluster.sh external      # cluster B (ODF external)
#   ./phase1-deploy/deploy-cluster.sh both          # both (parallel by default)
#
# Idempotent: skips a cluster whose clusters/<name>/metadata.json already exists.
# Teardown: openshift-install destroy cluster --dir clusters/<name>

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib.sh
source "${REPO_ROOT}/lib.sh"

require_env AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_BASE_DOMAIN CONTROL_PLANE_TYPE \
  INT_NAME INT_REGION INT_MACHINE_CIDR INT_CLUSTER_CIDR INT_SERVICE_CIDR INT_WORKER_TYPE INT_WORKER_REPLICAS \
  EXT_NAME EXT_REGION EXT_MACHINE_CIDR EXT_CLUSTER_CIDR EXT_SERVICE_CIDR EXT_WORKER_TYPE EXT_WORKER_REPLICAS \
  PULL_SECRET_FILE SSH_PUBLIC_KEY_FILE
require_file "${PULL_SECRET_FILE}" "${SSH_PUBLIC_KEY_FILE}"

render_and_install() {
  local key="$1"
  local name region machine cluster service worker replicas
  case "$key" in
    internal)
      name="$INT_NAME"; region="$INT_REGION"; machine="$INT_MACHINE_CIDR"
      cluster="$INT_CLUSTER_CIDR"; service="$INT_SERVICE_CIDR"
      worker="$INT_WORKER_TYPE"; replicas="$INT_WORKER_REPLICAS" ;;
    external)
      name="$EXT_NAME"; region="$EXT_REGION"; machine="$EXT_MACHINE_CIDR"
      cluster="$EXT_CLUSTER_CIDR"; service="$EXT_SERVICE_CIDR"
      worker="$EXT_WORKER_TYPE"; replicas="$EXT_WORKER_REPLICAS" ;;
    *) fail "unknown cluster key: $key (expected internal|external)" ;;
  esac

  local dir; dir="$(cluster_dir "$name")"
  if cluster_already_installed "$name"; then
    ok "[$name] already installed (metadata.json present) — skipping"
    return 0
  fi

  mkdir -p "$dir"
  info "[$name] rendering install-config (region=$region machineCIDR=$machine worker=$worker x$replicas)"
  CLUSTER_NAME="$name" REGION="$region" BASE_DOMAIN="$AWS_BASE_DOMAIN" \
  MACHINE_CIDR="$machine" CLUSTER_CIDR="$cluster" SERVICE_CIDR="$service" \
  CONTROL_PLANE_TYPE="$CONTROL_PLANE_TYPE" WORKER_TYPE="$worker" WORKER_REPLICAS="$replicas" \
  PULL_SECRET_JSON="$(jq -c . < "$PULL_SECRET_FILE")" \
  SSH_PUBLIC_KEY="$(< "$SSH_PUBLIC_KEY_FILE")" \
    envsubst < "${REPO_ROOT}/templates/aws-install-config.yaml.tmpl" > "${dir}/install-config.yaml"

  if grep -qE '\$\{[A-Z_]+\}' "${dir}/install-config.yaml"; then
    fail "[$name] install-config.yaml has unsubstituted variables"
  fi
  # openshift-install consumes (deletes) install-config.yaml — keep a backup.
  cp "${dir}/install-config.yaml" "${dir}/install-config.yaml.bak"

  info "[$name] openshift-install create cluster (~40 min; log: ${dir}/install.log)"
  openshift-install create cluster --dir "$dir" --log-level info > "${dir}/install.log" 2>&1
  ok "[$name] install complete — kubeconfig: $(cluster_kubeconfig "$name")"
}

targets=()
case "${1:-both}" in
  internal) targets=(internal) ;;
  external) targets=(external) ;;
  both)     targets=(internal external) ;;
  *) fail "usage: $0 [internal|external|both]" ;;
esac

if [[ "${#targets[@]}" -gt 1 && "${PARALLEL_INSTALLS:-true}" == "true" ]]; then
  info "Installing ${targets[*]} in parallel (PARALLEL_INSTALLS=true)..."
  pids=()
  for t in "${targets[@]}"; do render_and_install "$t" & pids+=($!); done
  rc=0
  for p in "${pids[@]}"; do wait "$p" || rc=$?; done
  [[ $rc -eq 0 ]] || fail "one or more installs failed — see clusters/*/install.log"
else
  for t in "${targets[@]}"; do render_and_install "$t"; done
fi

echo
ok "Phase 1 complete. Installed cluster(s): ${targets[*]}"
for t in "${targets[@]}"; do
  case "$t" in internal) n="$INT_NAME" ;; external) n="$EXT_NAME" ;; esac
  [[ -r "$(cluster_kubeconfig "$n")" ]] && ok "  $n → export KUBECONFIG=$(cluster_kubeconfig "$n")"
done
ok "Next: Phase 2 (peering, external only) then Phase 3 (storage operators)"
