#!/usr/bin/env bash
# Phase 0 — pre-flight checks. Read-only; creates nothing. Fail fast before
# burning ~40 min of openshift-install time.
#
#   source env.sh && ./phase0-prereqs/00-prereqs.sh

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib.sh
source "${REPO_ROOT}/lib.sh"

# 1. CLI tools
info "Checking required CLI tools..."
require_cli oc openshift-install aws jq envsubst python3 curl
ok "CLI tools present"

# 2. Env vars
info "Checking required env vars..."
require_env \
  AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION \
  AWS_BASE_DOMAIN AWS_ROUTE53_ZONE_ID \
  INT_NAME INT_REGION INT_MACHINE_CIDR INT_CLUSTER_CIDR INT_SERVICE_CIDR INT_WORKER_TYPE INT_WORKER_REPLICAS \
  EXT_NAME EXT_REGION EXT_MACHINE_CIDR EXT_CLUSTER_CIDR EXT_SERVICE_CIDR EXT_WORKER_TYPE EXT_WORKER_REPLICAS \
  CONTROL_PLANE_TYPE \
  CEPH_REGION CEPH_VPC_CIDR CEPH_VPC_NAME_TAG CEPH_SG_NAME_TAG \
  PULL_SECRET_FILE SSH_PUBLIC_KEY_FILE SSH_PRIVATE_KEY_FILE \
  OCP_VERSION ODF_CHANNEL
ok "Env vars set"

# 3. Local files
info "Checking pull secret + SSH keys..."
require_file "${PULL_SECRET_FILE}" "${SSH_PUBLIC_KEY_FILE}" "${SSH_PRIVATE_KEY_FILE}"
if ! jq -e '.auths | has("cloud.openshift.com") and has("quay.io") and has("registry.redhat.io")' \
     "${PULL_SECRET_FILE}" >/dev/null 2>&1; then
  fail "${PULL_SECRET_FILE} missing required registry auths (cloud.openshift.com, quay.io, registry.redhat.io)"
fi
ok "Pull secret + SSH keys valid"

# 4. CIDR sanity — Cluster B must not overlap the ceph9 VPC.
if [[ "${EXT_MACHINE_CIDR}" == "${CEPH_VPC_CIDR}" ]]; then
  fail "EXT_MACHINE_CIDR (${EXT_MACHINE_CIDR}) must not equal ceph VPC CIDR (${CEPH_VPC_CIDR}) — peering needs distinct CIDRs"
fi
# Crude /16 first-two-octets overlap guard for the common case.
ext_pfx="$(echo "${EXT_MACHINE_CIDR}" | cut -d. -f1-2)"
ceph_pfx="$(echo "${CEPH_VPC_CIDR}" | cut -d. -f1-2)"
[[ "${ext_pfx}" == "${ceph_pfx}" ]] && fail "EXT_MACHINE_CIDR overlaps ceph VPC /16 (${ceph_pfx}.x) — choose a different range"
ok "Cluster B CIDR does not overlap ceph9 VPC"

# 5. AWS access
info "Validating AWS credentials..."
aws sts get-caller-identity --output text >/dev/null \
  || fail "aws sts get-caller-identity failed — bad/expired keys"
ok "AWS auth OK"

# 6. Route 53 zone matches base domain
info "Validating Route 53 zone ${AWS_ROUTE53_ZONE_ID}..."
aws route53 get-hosted-zone --id "${AWS_ROUTE53_ZONE_ID}" --output text >/dev/null \
  || fail "Route 53 zone ${AWS_ROUTE53_ZONE_ID} not found / not accessible"
zone_name=$(aws route53 get-hosted-zone --id "${AWS_ROUTE53_ZONE_ID}" --query 'HostedZone.Name' --output text)
zone_name="${zone_name%.}"
[[ "${zone_name}" == "${AWS_BASE_DOMAIN}" ]] \
  || fail "Route 53 zone (${zone_name}) != AWS_BASE_DOMAIN (${AWS_BASE_DOMAIN})"
ok "Route 53 zone matches AWS_BASE_DOMAIN"

# 7. openshift-install version vs OCP_VERSION
info "Checking openshift-install version..."
oi_ver=$(openshift-install version | head -1 | awk '{print $2}')
oi_minor=$(echo "${oi_ver}" | cut -d. -f1-2)
if [[ "${oi_minor}" != "${OCP_VERSION}" ]]; then
  warn "openshift-install ${oi_minor} != OCP_VERSION ${OCP_VERSION} (env.sh)"
else
  ok "openshift-install ${oi_ver} matches OCP_VERSION"
fi

# 8. ceph9 VPC reachable (so Phase 2 peering will have a target)
info "Looking up ceph9 VPC by tag Name=${CEPH_VPC_NAME_TAG}..."
ceph_vpc=$(aws ec2 describe-vpcs --region "${CEPH_REGION}" \
  --filters "Name=tag:Name,Values=${CEPH_VPC_NAME_TAG}" \
  --query 'Vpcs[0].VpcId' --output text 2>/dev/null || echo None)
if [[ "${ceph_vpc}" == "None" || -z "${ceph_vpc}" ]]; then
  warn "ceph9 VPC (${CEPH_VPC_NAME_TAG}) not found in ${CEPH_REGION} — Phase 2 (external peering) will need it"
else
  ok "ceph9 VPC found: ${ceph_vpc}"
fi

echo
ok "Phase 0 pre-flight passed. Next: ./phase1-deploy/deploy-cluster.sh both"
