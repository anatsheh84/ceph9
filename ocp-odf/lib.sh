# Shared bash helpers for the ocp-odf phases. Sourced, not executed.
# Expects env.sh to already be sourced. Adapted from the multicloudocp-lab
# bootstrap/lib.sh.

set -euo pipefail

# ────────────────────────────── Output styling ───────────────────────────
_red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
_green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
_yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
_blue()   { printf '\033[0;34m%s\033[0m\n' "$*"; }

info()    { _blue   "[INFO]  $*"; }
ok()      { _green  "[OK]    $*"; }
warn()    { _yellow "[WARN]  $*"; }
fail()    { _red    "[FAIL]  $*" >&2; exit 1; }

# ────────────────────────────── Repo paths ───────────────────────────────
# REPO_ROOT is set by each caller (the ocp-odf dir). Default if not.
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
CLUSTERS_DIR="${REPO_ROOT}/clusters"

cluster_dir()        { echo "${CLUSTERS_DIR}/$1"; }
cluster_kubeconfig() { echo "${CLUSTERS_DIR}/$1/auth/kubeconfig"; }
cluster_metadata()   { echo "${CLUSTERS_DIR}/$1/metadata.json"; }

# infraID openshift-install stamps on every AWS resource for a cluster.
cluster_infra_id() {
  local name="$1" meta
  meta="$(cluster_metadata "$name")"
  [[ -f "$meta" ]] || fail "metadata.json not found for $name — run Phase 1 first"
  jq -r '.infraID' < "$meta"
}

# ────────────────────────────── Env / file / CLI validation ──────────────
require_env() {
  local missing=0
  for v in "$@"; do
    if [[ -z "${!v:-}" ]]; then _red "[FAIL] env var $v is not set" >&2; missing=1; fi
  done
  [[ $missing -eq 0 ]] || exit 1
}

require_file() { for f in "$@"; do [[ -r "$f" ]] || fail "required file not readable: $f"; done; }
require_cli()  { for c in "$@"; do command -v "$c" >/dev/null 2>&1 || fail "required CLI not in PATH: $c"; done; }

# ────────────────────────────── openshift-install helpers ────────────────
cluster_already_installed() { [[ -f "$(cluster_metadata "$1")" ]]; }

# ────────────────────────────── oc helpers ───────────────────────────────
# Wait for a CSV whose name starts with <prefix> in <ns> to reach Succeeded.
wait_for_csv_succeeded() {
  local kubeconfig="$1" ns="$2" prefix="$3" timeout="${4:-900}"
  local elapsed=0 sleep_s=15
  info "Waiting for CSV '${prefix}*' Succeeded in ns ${ns} (timeout ${timeout}s)..."
  while (( elapsed < timeout )); do
    local phase
    phase=$(KUBECONFIG="$kubeconfig" oc get csv -n "$ns" -o \
      jsonpath="{range .items[*]}{.metadata.name}{'='}{.status.phase}{'\n'}{end}" 2>/dev/null \
      | awk -F= -v p="$prefix" 'index($1,p)==1 {print $2; exit}')
    [[ "$phase" == "Succeeded" ]] && { ok "CSV ${prefix}* Succeeded"; return 0; }
    sleep "$sleep_s"; elapsed=$((elapsed + sleep_s))
  done
  fail "CSV ${prefix}* not Succeeded within ${timeout}s in ns ${ns}"
}
