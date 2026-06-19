#!/usr/bin/env bash
# Phase 3b — ODF in EXTERNAL mode on ocp-external, connected to the ceph9 cluster.
# OCP consumes ceph9 (RBD/CephFS/RGW) over the Phase-2 VPC peering.
#
#   source env.sh && ./phase3-storage/odf-external.sh
#
# Flow (idempotent):
#   1. Install the ODF operator on ocp-external.
#   2. Extract the version-matched exporter from the rook-ceph image.
#   3. Run it on ceph01 (via bastion, inside cephadm shell) -> connection JSON.
#   4. Import the JSON as secret rook-ceph-external-cluster-details.
#   5. Create the external-mode StorageCluster; wait for Ready + Connected.
#
# Requires (from env.sh): CEPH_SSH_KEY, CEPH_BASTION_IP, CEPH_KNOWN_HOSTS,
#   CEPH_ADMIN_NODE_IP, CEPH_RBD_POOL, CEPH_FS_NAME, CEPH_RGW_ENDPOINT.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib.sh
source "${REPO_ROOT}/lib.sh"

require_env EXT_NAME ODF_CHANNEL \
  CEPH_SSH_KEY CEPH_BASTION_IP CEPH_KNOWN_HOSTS CEPH_ADMIN_NODE_IP \
  CEPH_RBD_POOL CEPH_FS_NAME CEPH_RGW_ENDPOINT
KUBECONFIG="$(cluster_kubeconfig "${EXT_NAME}")"; export KUBECONFIG
[[ -r "${KUBECONFIG}" ]] || fail "kubeconfig not found for ${EXT_NAME} — run Phase 1 first"
require_cli oc jq ssh scp

EXPORTER_DIR="${REPO_ROOT}/phase3-storage/exporter"
EXPORTER="${EXPORTER_DIR}/create-external-cluster-resources.py"
JSON_OUT="${REPO_ROOT}/phase3-storage/${EXT_NAME}.external-details.json"

ssh_ceph() {
  local px="ProxyCommand=ssh -W %h:%p -i ${CEPH_SSH_KEY} -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=${CEPH_KNOWN_HOSTS} ec2-user@${CEPH_BASTION_IP}"
  ssh -i "${CEPH_SSH_KEY}" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${CEPH_KNOWN_HOSTS}" \
      -o ConnectTimeout=20 -o "${px}" "ec2-user@${CEPH_ADMIN_NODE_IP}" "$@"
}
scp_ceph() {  # scp_ceph <local> <remote-path>
  local px="ProxyCommand=ssh -W %h:%p -i ${CEPH_SSH_KEY} -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=${CEPH_KNOWN_HOSTS} ec2-user@${CEPH_BASTION_IP}"
  scp -i "${CEPH_SSH_KEY}" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${CEPH_KNOWN_HOSTS}" \
      -o "${px}" "$1" "ec2-user@${CEPH_ADMIN_NODE_IP}:$2"
}

# ── 1. ODF operator ───────────────────────────────────────────────────────
info "Installing ODF operator on ${EXT_NAME} (channel ${ODF_CHANNEL})..."
oc apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-storage
  labels:
    openshift.io/cluster-monitoring: "true"
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: openshift-storage-operatorgroup
  namespace: openshift-storage
spec:
  targetNamespaces: [openshift-storage]
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: odf-operator
  namespace: openshift-storage
spec:
  channel: ${ODF_CHANNEL}
  name: odf-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
EOF
wait_for_csv_succeeded "${KUBECONFIG}" openshift-storage odf-operator 1200
for _ in $(seq 1 60); do
  oc get crd storageclusters.ocs.openshift.io >/dev/null 2>&1 && break || sleep 5
done
ok "ODF operator ready"

# ── 2. Extract the version-matched exporter from the rook-ceph image ───────
if [[ -r "${EXPORTER}" && "${FORCE_EXTRACT:-false}" != "true" ]]; then
  info "Using cached exporter ${EXPORTER} (set FORCE_EXTRACT=true to re-extract)"
else
  info "Extracting exporter from rook-ceph image..."
  rook_csv=$(oc get csv -n openshift-storage -o name | grep rook-ceph-operator | head -1)
  rook_img=$(oc get "${rook_csv}" -n openshift-storage -o json \
    | jq -r '.spec.relatedImages[]?.image, .spec.install.spec.deployments[]?.spec.template.spec.containers[]?.image' \
    | grep -i rook | head -1)
  [[ -n "${rook_img}" ]] || fail "could not resolve rook image"
  oc delete pod rook-extract -n default --ignore-not-found >/dev/null 2>&1
  oc run rook-extract -n default --image="${rook_img}" --restart=Never --command -- sleep 600 >/dev/null
  for _ in $(seq 1 40); do
    [[ "$(oc get pod rook-extract -n default -o jsonpath='{.status.phase}' 2>/dev/null)" == "Running" ]] && break
    sleep 5
  done
  mkdir -p "${EXPORTER_DIR}"
  oc cp default/rook-extract:/etc/rook-external/create-external-cluster-resources.py "${EXPORTER}" >/dev/null
  oc delete pod rook-extract -n default --ignore-not-found >/dev/null 2>&1
  [[ -s "${EXPORTER}" ]] || fail "exporter extraction failed"
  ok "Exporter extracted to ${EXPORTER}"
fi

# ── 3. Run the exporter on ceph01 -> connection JSON ──────────────────────
info "Running exporter on ceph01 (${CEPH_ADMIN_NODE_IP}) inside cephadm shell..."
scp_ceph "${EXPORTER}" /home/ec2-user/exporter.py >/dev/null 2>&1
ssh_ceph "sudo /usr/sbin/cephadm shell --mount /home/ec2-user/exporter.py -- \
  python3 /mnt/exporter.py \
    --rbd-data-pool-name ${CEPH_RBD_POOL} \
    --cephfs-filesystem-name ${CEPH_FS_NAME} \
    --rgw-endpoint ${CEPH_RGW_ENDPOINT} \
    --v2-port-enable 2>/dev/null" > "${JSON_OUT}" 2>/dev/null
jq -e 'type=="array" and length>0' "${JSON_OUT}" >/dev/null \
  || fail "exporter did not produce valid JSON (see ${JSON_OUT})"
ok "Connection JSON written ($(jq length "${JSON_OUT}") resources): ${JSON_OUT}"

# ── 4. Import JSON as the external-cluster-details secret ──────────────────
info "Creating secret rook-ceph-external-cluster-details..."
oc create secret generic rook-ceph-external-cluster-details -n openshift-storage \
  --from-file=external_cluster_details="${JSON_OUT}" \
  --dry-run=client -o yaml | oc apply -f - >/dev/null
ok "Secret imported"

# ── 5. External-mode StorageCluster ───────────────────────────────────────
info "Creating external-mode StorageCluster..."
oc apply -f - <<EOF
apiVersion: ocs.openshift.io/v1
kind: StorageCluster
metadata:
  name: ocs-external-storagecluster
  namespace: openshift-storage
spec:
  externalStorage:
    enable: true
  labelSelector: {}
EOF

info "Waiting for external StorageCluster to become Ready..."
phase=""; elapsed=0
while (( elapsed < 900 )); do
  phase=$(oc get storagecluster ocs-external-storagecluster -n openshift-storage \
    -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "${phase}" == "Ready" ]] && { ok "External StorageCluster Ready"; break; }
  sleep 20; elapsed=$((elapsed+20))
done
[[ "${phase}" == "Ready" ]] || fail "external StorageCluster not Ready (phase=${phase})"

echo
ok "Phase 3b complete — ODF external is connected to ceph9 on ${EXT_NAME}."
info "CephCluster:"; oc get cephcluster -n openshift-storage 2>/dev/null
info "External StorageClasses:"; oc get sc 2>/dev/null | grep -E 'NAME|ceph|rgw|nfs'
