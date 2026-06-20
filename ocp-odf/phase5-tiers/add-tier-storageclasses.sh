#!/usr/bin/env bash
# Phase 5 — add OpenShift StorageClasses for the Ceph HDD (Tier1) pools.
#
# Clones the existing external StorageClasses (so clusterID + CSI secrets are
# inherited — rebuild-proof) and overrides only the pool, yielding:
#   ceph-rbd-hdd   -> pool rbd-hdd                  (Tier1 block)
#   cephfs-hdd     -> pool cephfs.cephfs.data.hdd   (Tier1 file)
#
# No CSI cap changes needed: rbd users have 'profile rbd' (all rbd pools) and
# the cephfs node user has 'tag cephfs *=*' (all tagged cephfs data pools).
# Prereq: ceph9 phase 06_storage_tiers has created the hdd pools.
#
#   source env.sh && ./phase5-tiers/add-tier-storageclasses.sh

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OCPODF="$(cd "${HERE}/.." && pwd)"
# shellcheck source=../lib.sh
source "${OCPODF}/lib.sh"
require_env EXT_NAME
KUBECONFIG="$(cluster_kubeconfig "${EXT_NAME}")"; export KUBECONFIG
require_cli oc jq

clone_sc() {  # src_sc  new_name  new_pool  extra_jq
  local src="$1" name="$2" pool="$3"
  info "Creating StorageClass ${name} (pool=${pool}) from ${src}..."
  oc get sc "${src}" -o json \
    | jq --arg n "${name}" --arg p "${pool}" '
        del(.metadata.uid, .metadata.resourceVersion, .metadata.creationTimestamp,
            .metadata.generation, .metadata.managedFields, .status)
        | .metadata.name=$n
        | .parameters.pool=$p
        | .metadata.annotations={"storageclass.kubernetes.io/is-default-class":"false"}
        | .metadata.labels={"app.kubernetes.io/part-of":"ceph-external-demo","storage-tier":"hdd"}' \
    | oc apply -f - >/dev/null
  ok "  ${name} applied"
}

clone_sc ocs-external-storagecluster-ceph-rbd ceph-rbd-hdd rbd-hdd
clone_sc ocs-external-storagecluster-cephfs   cephfs-hdd    cephfs.cephfs.data.hdd

echo
info "Tier storageclasses:"
oc get sc | grep -E 'NAME|ceph-rbd|cephfs|tier' || true

# ---- functional test: PVC on the HDD RBD class ----
info "Testing a PVC on ceph-rbd-hdd..."
oc apply -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: tier-hdd-test, namespace: default }
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ceph-rbd-hdd
  resources: { requests: { storage: 2Gi } }
EOF
for _ in $(seq 1 30); do
  st=$(oc -n default get pvc tier-hdd-test -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "${st}" == "Bound" ]] && break; sleep 4
done
if [[ "${st}" == "Bound" ]]; then
  pv=$(oc -n default get pvc tier-hdd-test -o jsonpath='{.spec.volumeName}')
  pool=$(oc get pv "$pv" -o jsonpath='{.spec.csi.volumeAttributes.pool}')
  ok "PVC Bound — provisioned in ceph pool: ${pool} (expect rbd-hdd)"
else
  warn "PVC not Bound (phase=${st})"
fi
oc -n default delete pvc tier-hdd-test >/dev/null 2>&1 || true
echo
ok "Phase 5 complete — Tier1 (HDD) StorageClasses available on ${EXT_NAME}."
