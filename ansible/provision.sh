#!/usr/bin/env bash
# provision.sh — friendly front door for the ceph9 lab.
#
# Asks which cluster topology to build, then runs the full site.yml with the
# matching ceph_topology. Credentials are read from the environment (AWS_*,
# RH_USER/RH_PASS, optional CEPH_DASHBOARD_PASSWORD) exactly as the playbooks
# expect — this script never stores or prints secrets.
#
# Usage:
#   ./provision.sh                 # interactive menu
#   ./provision.sh 1|2             # non-interactive (1=4node, 2=12node)
#   CEPH_TOPOLOGY=12node-tiered ./provision.sh
#   ./provision.sh -- --check      # everything after -- is passed to ansible
set -euo pipefail
cd "$(dirname "$0")"

# Split args: a leading 1|2 choice, anything after `--` goes to ansible-playbook.
CHOICE="${1:-}"; EXTRA=()
if [ "${CHOICE:-}" = "--" ]; then CHOICE=""; shift; EXTRA=("$@");
elif [ -n "${CHOICE:-}" ]; then shift || true
  if [ "${1:-}" = "--" ]; then shift; EXTRA=("$@"); fi
fi

topo_from_choice() { case "$1" in 1) echo 4node-twotier ;; 2) echo 12node-tiered ;; *) echo "" ;; esac; }

TOPO="${CEPH_TOPOLOGY:-}"
if [ -z "$TOPO" ] && [ -n "$CHOICE" ]; then TOPO="$(topo_from_choice "$CHOICE")"; fi
if [ -z "$TOPO" ]; then
  echo "Which cluster topology do you want to deploy?"
  echo "   1) 4-node Two-tiers cluster (existing)"
  echo "   2) 12-node 3-tier cluster (NVMe + SAS + SATA)"
  printf " Choice [1-2]: "
  read -r ans
  TOPO="$(topo_from_choice "$ans")"
  [ -z "$TOPO" ] && { echo "error: invalid choice '$ans' (expected 1 or 2)" >&2; exit 1; }
fi

[ -f "inventory/topologies/${TOPO}.yml" ] || { echo "error: no profile inventory/topologies/${TOPO}.yml" >&2; exit 1; }
: "${AWS_ACCESS_KEY_ID:?set AWS_ACCESS_KEY_ID in your environment}"
: "${RH_USER:?set RH_USER in your environment}"

echo "==> Deploying topology: ${TOPO}"
exec ansible-playbook -e "ceph_topology=${TOPO}" playbooks/site.yml "${EXTRA[@]}"
