#!/usr/bin/env bash
# Phase 3a — ODF in INTERNAL mode on the ocp-internal cluster.
# Self-contained Ceph inside OCP: OSDs are backed by cluster-provisioned gp3 EBS.
#
#   source env.sh && ./phase3-storage/odf-internal.sh
#
# Idempotent: re-applies the operator + StorageCluster; safe to re-run.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib.sh
source "${REPO_ROOT}/lib.sh"

require_env INT_NAME ODF_CHANNEL
KUBECONFIG="$(cluster_kubeconfig "${INT_NAME}")"; export KUBECONFIG
[[ -r "${KUBECONFIG}" ]] || fail "kubeconfig not found for ${INT_NAME} — run Phase 1 first"
require_cli oc

OSD_SIZE="${ODF_OSD_SIZE:-512Gi}"
OSD_STORAGECLASS="${ODF_OSD_STORAGECLASS:-gp3-csi}"

# ── 1. Operator: namespace + OperatorGroup + Subscription ─────────────────
info "Installing ODF operator (channel ${ODF_CHANNEL})..."
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
  targetNamespaces:
    - openshift-storage
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

# ── 2. Wait for the operator CSV ──────────────────────────────────────────
wait_for_csv_succeeded "${KUBECONFIG}" openshift-storage odf-operator 1200
# ocs-operator is pulled in as a dependency; wait for it too (provides StorageCluster CRD)
info "Waiting for StorageCluster CRD to register..."
for _ in $(seq 1 60); do
  oc get crd storageclusters.ocs.openshift.io >/dev/null 2>&1 && break || sleep 5
done
oc get crd storageclusters.ocs.openshift.io >/dev/null 2>&1 \
  || fail "StorageCluster CRD never appeared — check ocs-operator install"
ok "ODF operator ready"

# ── 3. Label worker nodes for ODF ─────────────────────────────────────────
info "Labeling worker nodes for openshift-storage..."
for n in $(oc get nodes -l node-role.kubernetes.io/worker -o jsonpath='{.items[*].metadata.name}'); do
  oc label node "$n" cluster.ocs.openshift.io/openshift-storage="" --overwrite >/dev/null
done
ok "Workers labeled"

# ── 4. Internal StorageCluster ────────────────────────────────────────────
info "Creating internal StorageCluster (3 x ${OSD_SIZE} OSDs on ${OSD_STORAGECLASS})..."
oc apply -f - <<EOF
apiVersion: ocs.openshift.io/v1
kind: StorageCluster
metadata:
  name: ocs-storagecluster
  namespace: openshift-storage
spec:
  storageDeviceSets:
    - name: ocs-deviceset
      count: 1
      replica: 3
      portable: true
      deviceType: ssd
      dataPVCTemplate:
        spec:
          accessModes: [ReadWriteOnce]
          volumeMode: Block
          storageClassName: ${OSD_STORAGECLASS}
          resources:
            requests:
              storage: ${OSD_SIZE}
EOF

# ── 5. Wait for StorageCluster Ready ──────────────────────────────────────
info "Waiting for StorageCluster to become Ready (OSD creation ~10-15 min)..."
elapsed=0
while (( elapsed < 1500 )); do
  phase=$(oc get storagecluster ocs-storagecluster -n openshift-storage \
    -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "${phase}" == "Ready" ]] && { ok "StorageCluster Ready"; break; }
  sleep 20; elapsed=$((elapsed+20))
done
[[ "${phase}" == "Ready" ]] || fail "StorageCluster not Ready within timeout (phase=${phase})"

echo
ok "Phase 3a complete — ODF internal is up on ${INT_NAME}."
info "StorageClasses:"
oc get sc 2>/dev/null | grep -E 'ceph|noobaa|NAME' || true
