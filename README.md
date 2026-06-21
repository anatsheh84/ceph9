# ceph9 — Ceph 9 on AWS + two OpenShift clusters + self-service storage portal

An end-to-end lab that stands up a **Red Hat Ceph Storage 9** cluster on AWS, two
**OpenShift 4.21** clusters that consume it two different ways, and a Red Hat-themed
web **portal** that both *consumes* and *self-service provisions* storage across
**block, file, and object** — with **two storage tiers** (SSD/HDD) and **HA gateway
endpoints** behind internal AWS load balancers.

```
                          AWS (us-east-2)
┌────────────────────────────────────────────────────────────────────────────┐
│   VPC 10.20.0.0/16  ── ceph9 (RHCS 9, cephadm)                               │
│     bastion ─ NAT ─ ceph01..04                                               │
│       OSD tiers:  Tier0 ssd (gp3)  ·  Tier1 hdd (st1)                        │
│       gateways:   RGW/S3  ─ internal NLB ┐   NFS ─ internal NLB ┐            │
│                              ▲ (8080)    │        ▲ (2049)       │           │
│                              │  VPC peering (private)            │           │
│   VPC 10.22.0.0/16  ── ocp-external (OCP 4.21)                   │           │
│     └ ODF EXTERNAL mode → consumes ceph9                         │           │
│        └ portal: provision + consume (block/file/object, SSD/HDD)┘           │
│                                                                              │
│   VPC 10.21.0.0/16  ── ocp-internal (OCP 4.21)                               │
│     └ ODF INTERNAL mode (self-contained Ceph inside OCP)                     │
└────────────────────────────────────────────────────────────────────────────┘
```

## Components

| Path | What |
|---|---|
| [`ansible/`](ansible/) | Provisions **ceph9** (VPC, bastion, 4 nodes, cephadm, OSDs, RBD/CephFS/RGW/NFS) + optional phases for **storage tiers** and **HA endpoints**. See [ansible/README.md](ansible/README.md). |
| [`ocp-odf/`](ocp-odf/) | Phased build of the two OCP clusters + ODF (internal & external) + tier StorageClasses. See [ocp-odf/README.md](ocp-odf/README.md). |
| [`ocp-odf/demo-app/`](ocp-odf/demo-app/) | The self-service storage **portal** (Flask). See [demo-app/README.md](ocp-odf/demo-app/README.md). |
| [`setup.md`](setup.md) · [`HANDOFF.md`](HANDOFF.md) | Manual deployment guide + build history/lessons. |

## The two storage use cases
| Cluster | Storage | How |
|---|---|---|
| `ocp-internal` | **ODF internal** | self-contained Ceph inside OCP (OSDs on gp3 EBS) |
| `ocp-external` | **ODF external** | connects to the existing **ceph9** cluster over VPC peering |

## Phases (each idempotent & re-runnable)

| Phase | Script | Result |
|---|---|---|
| ceph9 base | `ansible/playbooks/site.yml` | RHCS 9 cluster, HEALTH_OK |
| ceph9 06 *(opt)* | `ansible/playbooks/06_storage_tiers.yml` | **Tier1 HDD** (st1 OSDs, `hdd` class, rbd/cephfs/rgw hdd pools) |
| ceph9 07 *(opt)* | `ansible/playbooks/07_nfs_ha_lb.yml` | **HA**: 2 ganesha + **internal NLBs** for NFS (2049) & RGW (8080) |
| ocp 0 | `ocp-odf/phase0-prereqs/00-prereqs.sh` | pre-flight checks |
| ocp 1 | `ocp-odf/phase1-deploy/deploy-cluster.sh both` | both OCP clusters (own VPCs) |
| ocp 2 | `ocp-odf/phase2-network/peer-external-to-ceph.sh` | peer `ocp-external` ↔ ceph9 VPC |
| ocp 3a/3b | `ocp-odf/phase3-storage/odf-{internal,external}.sh` | ODF internal / external→ceph9 |
| ocp 4 | `ocp-odf/demo-app/deploy.sh` | build + deploy the portal |
| ocp 5 *(opt)* | `ocp-odf/phase5-tiers/add-tier-storageclasses.sh` | HDD-tier StorageClasses (`ceph-rbd-hdd`, `cephfs-hdd`) |

## The portal (demo-app)
Two tabs:
- **Self-Service Provisioning** — log in with Ceph credentials (RBAC enforced); create
  **S3 / NFS / CephFS / RBD** on demand, choosing **Tier0 (SSD)** or **Tier1 (HDD)**;
  live-discover existing resources; retrieve connection details any time; browse/upload/
  preview S3 objects. Admins see everything; users see only what they created.
- **Consume Mounted Storage** — block (RBD PVC), file (CephFS PVC), object (OBC bucket):
  upload / list / download.

## Storage abstractions
| Endpoint | Backed by | Kubernetes object | Tiering |
|---|---|---|---|
| Block | Ceph RBD | `PersistentVolumeClaim` (RWO) | pool `rbd` / `rbd-hdd` |
| File | CephFS | `PersistentVolumeClaim` (RWX) | data pool `…/data` / `…/data.hdd` |
| Object | Ceph RGW (S3) | `ObjectBucketClaim` (API-accessed, not mounted) | `STANDARD` / `HDD` storage class |

## HA endpoints
- **RGW (S3)** and **NFS** sit behind **internal AWS NLBs** (`ceph9-rgw-nlb` :8080,
  `ceph9-nfs-nlb` :2049). The portal's S3 and NFS paths target the NLBs (resolved
  automatically by `deploy.sh`). RGW failover is **seamless** (stateless); NFS failover
  is **recover-with-pause** (stateful — NFSv4 grace reclaim).
- **RBD/CephFS** need no LB — clients use the mon quorum + OSD replication (+ MDS
  standby for CephFS); HA is native.
- *Why NLB, not Ceph `ingress`:* keepalived's VIP can't float in an AWS VPC; an internal
  NLB is the AWS-native equivalent.

## Status
Built and verified end-to-end: both OCP clusters healthy; `ocp-external` connected to
ceph9 (matching FSID); portal round-trips block/file/object and self-service provisions
all four types into the correct tier; NFS & RGW served through internal NLBs with
failover verified.

## Credentials & safety
`ocp-odf/env.sh` is the only place real credentials go and is gitignored, along with
`clusters/` (kubeconfigs), `*.pem`, and the ODF external-details JSON (ceph keyrings).
Never commit those.
