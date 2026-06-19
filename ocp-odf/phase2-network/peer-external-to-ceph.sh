#!/usr/bin/env bash
# Phase 2 — connect the ODF-external cluster (ocp-external) to the ceph9 cluster
# over private networking via a VPC peering connection.
#
# What it does (all idempotent, tag/lookup-based):
#   1. Resolve the ceph9 VPC (by Name tag) and the ocp-external VPC (by infraID).
#   2. Create + accept a VPC peering connection between them.
#   3. Add reciprocal routes (peer CIDR -> pcx) in EVERY route table of both VPCs.
#   4. Open the ceph9 security group to the ocp-external CIDR on the Ceph ports
#      external mode needs: mon 3300/6789, OSD/mgr 6800-7300, RGW 8080, mgr 9283.
#
#   source env.sh && ./phase2-network/peer-external-to-ceph.sh
#
# Re-runnable. Teardown note: delete the pcx, the added routes, and the SG rule
# (or just delete the peering — routes referencing it become blackholes to clean).

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib.sh
source "${REPO_ROOT}/lib.sh"

require_env CEPH_REGION CEPH_VPC_CIDR CEPH_VPC_NAME_TAG CEPH_SG_NAME_TAG \
            EXT_NAME EXT_MACHINE_CIDR
require_cli aws jq

R=(--region "${CEPH_REGION}")          # both VPCs share the region
PEER_TAG="ceph9-${EXT_NAME}-peering"
CEPH_PORTS=(3300 6789 8080 9283)       # discrete ports; OSD/mgr range handled separately

# ── 1. Resolve VPCs ───────────────────────────────────────────────────────
info "Resolving VPCs..."
ceph_vpc=$(aws ec2 describe-vpcs "${R[@]}" \
  --filters "Name=tag:Name,Values=${CEPH_VPC_NAME_TAG}" \
  --query 'Vpcs[0].VpcId' --output text)
[[ "${ceph_vpc}" == "None" || -z "${ceph_vpc}" ]] && fail "ceph9 VPC (${CEPH_VPC_NAME_TAG}) not found"

ext_infra="$(cluster_infra_id "${EXT_NAME}")"
ext_vpc=$(aws ec2 describe-vpcs "${R[@]}" \
  --filters "Name=tag:kubernetes.io/cluster/${ext_infra},Values=owned,shared" \
  --query 'Vpcs[0].VpcId' --output text)
[[ "${ext_vpc}" == "None" || -z "${ext_vpc}" ]] && fail "ocp-external VPC (infraID ${ext_infra}) not found"

ok "ceph9 VPC=${ceph_vpc} (${CEPH_VPC_CIDR})  |  ocp-external VPC=${ext_vpc} (${EXT_MACHINE_CIDR})"

# ── 2. Peering connection (find-or-create, then accept) ───────────────────
info "Ensuring VPC peering connection..."
pcx=$(aws ec2 describe-vpc-peering-connections "${R[@]}" \
  --filters "Name=requester-vpc-info.vpc-id,Values=${ceph_vpc}" \
            "Name=accepter-vpc-info.vpc-id,Values=${ext_vpc}" \
            "Name=status-code,Values=active,pending-acceptance,provisioning" \
  --query 'VpcPeeringConnections[0].VpcPeeringConnectionId' --output text)

if [[ "${pcx}" == "None" || -z "${pcx}" ]]; then
  pcx=$(aws ec2 create-vpc-peering-connection "${R[@]}" \
    --vpc-id "${ceph_vpc}" --peer-vpc-id "${ext_vpc}" \
    --tag-specifications "ResourceType=vpc-peering-connection,Tags=[{Key=Name,Value=${PEER_TAG}}]" \
    --query 'VpcPeeringConnection.VpcPeeringConnectionId' --output text)
  info "Created peering ${pcx}"
else
  info "Reusing peering ${pcx}"
fi

status=$(aws ec2 describe-vpc-peering-connections "${R[@]}" \
  --vpc-peering-connection-ids "${pcx}" \
  --query 'VpcPeeringConnections[0].Status.Code' --output text)
if [[ "${status}" == "pending-acceptance" ]]; then
  aws ec2 accept-vpc-peering-connection "${R[@]}" --vpc-peering-connection-id "${pcx}" >/dev/null
  info "Accepted peering ${pcx}"
fi
# wait for active
for _ in $(seq 1 30); do
  status=$(aws ec2 describe-vpc-peering-connections "${R[@]}" \
    --vpc-peering-connection-ids "${pcx}" \
    --query 'VpcPeeringConnections[0].Status.Code' --output text)
  [[ "${status}" == "active" ]] && break
  sleep 2
done
[[ "${status}" == "active" ]] || fail "peering ${pcx} not active (status=${status})"
ok "Peering ${pcx} active"

# ── 3. Reciprocal routes in every route table of both VPCs ────────────────
add_routes() {
  local vpc="$1" dest_cidr="$2" label="$3"
  local rtbs
  rtbs=$(aws ec2 describe-route-tables "${R[@]}" \
    --filters "Name=vpc-id,Values=${vpc}" \
    --query 'RouteTables[].RouteTableId' --output text)
  for rtb in ${rtbs}; do
    local out
    out=$(aws ec2 create-route "${R[@]}" --route-table-id "${rtb}" \
      --destination-cidr-block "${dest_cidr}" \
      --vpc-peering-connection-id "${pcx}" 2>&1 || true)
    if echo "${out}" | grep -q '"Return": true\|RouteAlreadyExists'; then
      :   # created or already present
    elif echo "${out}" | grep -qi 'already exists'; then
      :
    else
      warn "  route ${dest_cidr} in ${rtb}: ${out}"
    fi
  done
  ok "Routes to ${dest_cidr} ensured in ${label} route tables"
}
info "Adding routes..."
add_routes "${ceph_vpc}" "${EXT_MACHINE_CIDR}" "ceph9"
add_routes "${ext_vpc}"  "${CEPH_VPC_CIDR}"    "ocp-external"

# ── 4. Open the ceph9 security group to the ocp-external CIDR ──────────────
info "Opening ceph9 security group to ${EXT_MACHINE_CIDR}..."
ceph_sg=$(aws ec2 describe-security-groups "${R[@]}" \
  --filters "Name=tag:Name,Values=${CEPH_SG_NAME_TAG}" \
  --query 'SecurityGroups[0].GroupId' --output text)
[[ "${ceph_sg}" == "None" || -z "${ceph_sg}" ]] && fail "ceph9 SG (${CEPH_SG_NAME_TAG}) not found"

authorize() {   # proto fromPort toPort description
  local out
  out=$(aws ec2 authorize-security-group-ingress "${R[@]}" --group-id "${ceph_sg}" \
    --ip-permissions "IpProtocol=$1,FromPort=$2,ToPort=$3,IpRanges=[{CidrIp=${EXT_MACHINE_CIDR},Description=\"$4\"}]" \
    2>&1 || true)
  if echo "${out}" | grep -qi 'InvalidPermission.Duplicate'; then
    info "  ${2}-${3}/$1 already open"
  elif echo "${out}" | grep -qi '"Return": true\|GroupId'; then
    ok "  opened ${2}-${3}/$1 ($4)"
  else
    warn "  ${2}-${3}/$1: ${out}"
  fi
}
authorize tcp 3300 3300 "ceph-mon-msgr2"
authorize tcp 6789 6789 "ceph-mon-msgr1"
authorize tcp 6800 7300 "ceph-osd-mgr"
authorize tcp 8080 8080 "ceph-rgw-s3"
authorize tcp 9283 9283 "ceph-mgr-prometheus"

echo
ok "Phase 2 complete: ${pcx} active, routes + SG in place."
ok "  ocp-external (${EXT_MACHINE_CIDR}) <-> ceph9 (${CEPH_VPC_CIDR})"
ok "Next: verify connectivity, then Phase 3 (ODF external mode)."
