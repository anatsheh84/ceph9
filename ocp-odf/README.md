# OCP + ODF lab on top of the ceph9 cluster

Two OpenShift clusters on AWS, each showcasing one OpenShift Data Foundation
(ODF) consumption model against the **existing `ceph9` RHCS 9 cluster**:

| Cluster | Name (default) | Storage model | VPC | Talks to ceph9? |
|---|---|---|---|---|
| **A** | `ocp-internal` | ODF **internal** mode (self-contained Ceph inside OCP) | own `10.21.0.0/16` | no |
| **B** | `ocp-external` | ODF **external** mode (connects to ceph9 as external Ceph) | own `10.22.0.0/16` | yes — via VPC peering |
| — | `ceph9-lab` | RHCS 9 (already deployed by `../ansible`) | `10.20.0.0/16` | — |

## Design decisions (why it's shaped this way)

- **Each OCP cluster gets its OWN VPC (default IPI).** `openshift-install`
  building its own VPC is the trivial path; installing *into* the existing
  ceph9 VPC would force the fiddly BYO-VPC flow (pre-created + tagged subnets
  across AZs, `subnets:` in install-config, hand-carved CIDRs). Separate VPCs +
  one peering connection is simpler and only Cluster B needs it.
- **No Ansible needed.** Provisioning is pure `openshift-install`; storage is
  `oc` + a few manifests. The multicloud Ansible stack is multi-cluster *management*
  (ACM/GitOps/DR) and does not do ODF external mode — irrelevant here.
- **Only Cluster B is peered to ceph9.** Cluster A (internal ODF) is fully
  self-contained and never touches the ceph9 VPC.
- **Non-overlapping CIDRs are mandatory for peering.** ceph9 is `10.20.0.0/16`;
  B's machineNetwork is `10.22.0.0/16`. A is `10.21.0.0/16` for tidiness.

## Phases

| Phase | What | Scripts | Applies to | Status |
|---|---|---|---|---|
| **0** | Pre-flight checks (tools, env, creds, DNS zone) | `phase0-prereqs/00-prereqs.sh` | both | ✅ tested |
| **1** | Deploy the OCP cluster(s) via `openshift-install` | `phase1-deploy/deploy-cluster.sh` | both | ✅ tested |
| **2** | Network: peer Cluster B's VPC ↔ ceph9 VPC + routes + SG rules | `phase2-network/peer-external-to-ceph.sh` | B only | ✅ tested |
| **3a** | ODF **internal** mode | `phase3-storage/odf-internal.sh` | A | ✅ tested |
| **3b** | ODF **external** mode → ceph9 | `phase3-storage/odf-external.sh` | B | ✅ tested |

> All phases are implemented and have been run end-to-end against the ceph9
> cluster. Each script is idempotent and re-runnable.

### Phase 0 — prerequisites
Verifies `oc`, `openshift-install`, `aws`, `jq`, `envsubst`, `python3`; required
env vars; pull secret + SSH key; AWS auth; and that the public Route 53 zone
matches `AWS_BASE_DOMAIN` (IPI requires a real public zone).

### Phase 1 — deploy clusters
Renders an install-config from `templates/aws-install-config.yaml.tmpl` and runs
`openshift-install create cluster`. Idempotent (skips if `metadata.json` exists).
Install state lands in `clusters/<name>/` (gitignored). Both clusters can run in
parallel (`PARALLEL_INSTALLS=true`, default).

### Phase 2 — network (Cluster B ↔ ceph9)
1. Find ceph9 VPC (`Name=ceph9-lab-vpc`) and Cluster B's VPC (via its `infraID`
   from `clusters/<EXT_NAME>/metadata.json`).
2. Create + accept a VPC peering connection.
3. Add routes for the peer CIDR in both VPCs' route tables.
4. Open the ceph9 security group (`ceph9-lab-sg`) to `EXT_MACHINE_CIDR` on the
   Ceph ports: mon `3300`/`6789`, OSD/mgr `6800-7300`, RGW `8080`.

### Phase 3 — storage operators
- **Cluster A (internal):** installs the ODF operator (OLM Subscription) and
  creates an internal-mode `StorageCluster` (3× gp3 OSDs). Yields
  `ocs-storagecluster-ceph-rbd` / `-cephfs` storageclasses.
- **Cluster B (external):** extracts the version-matched exporter
  (`create-external-cluster-resources.py`) from the rook-ceph image, runs it on
  `ceph01` inside `cephadm shell` to emit the connection JSON, imports it as the
  `rook-ceph-external-cluster-details` secret, and creates an external-mode
  `StorageCluster`. Yields `ocs-external-storagecluster-ceph-rbd` / `-cephfs` /
  `-ceph-rgw` storageclasses backed by ceph9 (verified: CephCluster `Connected`,
  FSID matches ceph9, test PVC bound an RBD image in the ceph9 `rbd` pool).

> Note (ODF 4.21): the exporter is no longer exposed via the ocs-operator CSV
> `export-script` annotation. `odf-external.sh` extracts it from the rook-ceph
> image at `/etc/rook-external/create-external-cluster-resources.py` instead.

## Usage

```bash
cd ocp-odf
cp env.sh.example env.sh
$EDITOR env.sh            # fill AWS creds, base domain + zone id, pull secret path
source env.sh

./phase0-prereqs/00-prereqs.sh          # fast, read-only
./phase1-deploy/deploy-cluster.sh both  # ~40 min, parallel (or: internal | external)
# Phase 2 / 3 once implemented:
# ./phase2-network/peer-external-to-ceph.sh
# ./phase3-storage/odf-internal.sh
# ./phase3-storage/odf-external.sh
```

## Prerequisites you must supply
- `oc` + `openshift-install` matching `OCP_VERSION` (download from the OCP mirror).
- A Red Hat pull secret at `PULL_SECRET_FILE` (from console.redhat.com).
- A public Route 53 hosted zone for `AWS_BASE_DOMAIN`. The ceph9 lab used the
  optional opentlc zone (`sandboxNNNN.opentlc.com`) — reuse it here.
- AWS On-Demand vCPU quota ≥ ~100 in `us-east-2` (two clusters of 3 masters + 3
  workers each).

## Credentials
`env.sh` is the only place real credentials go and is gitignored. Scripts read
them from the environment — no secrets in committed files.
