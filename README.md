# ceph9 — Ceph 9 on AWS + two OpenShift clusters + storage demo

An end-to-end lab that stands up a **Red Hat Ceph Storage 9** cluster on AWS, two
**OpenShift 4.21** clusters that consume it two different ways, and a web app that
demonstrates storing/reading data across **block, file, and object** storage —
all backed by the same external Ceph cluster.

```
                          AWS (us-east-2)
┌───────────────────────────────────────────────────────────────────────┐
│                                                                         │
│   VPC 10.20.0.0/16  ── ceph9 (RHCS 9, cephadm)                          │
│        bastion ─ NAT ─ ceph01..04  (RBD · CephFS · RGW/S3 · NFS)        │
│                              ▲                                          │
│                              │ VPC peering (private)                    │
│   VPC 10.22.0.0/16  ── ocp-external (OCP 4.21)                          │
│        └ ODF EXTERNAL mode ──┘   →  consumes ceph9                      │
│             └ demo app: block / file / object  → all land on ceph9      │
│                                                                         │
│   VPC 10.21.0.0/16  ── ocp-internal (OCP 4.21)                          │
│        └ ODF INTERNAL mode (self-contained Ceph inside OCP)             │
│                                                                         │
└───────────────────────────────────────────────────────────────────────┘
```

## Components

| Path | What |
|---|---|
| [`ansible/`](ansible/) | Provisions the **ceph9** RHCS 9 cluster on AWS (VPC, bastion, 4 nodes, cephadm bootstrap, OSDs, RBD/CephFS/RGW/NFS). See [ansible/README.md](ansible/README.md). |
| [`ocp-odf/`](ocp-odf/) | Phased build of the two OCP clusters + storage. See [ocp-odf/README.md](ocp-odf/README.md). |
| [`ocp-odf/demo-app/`](ocp-odf/demo-app/) | Red Hat-themed Flask app exercising block/file/object against ceph9. See [demo-app/README.md](ocp-odf/demo-app/README.md). |
| [`setup.md`](setup.md) | The hand-validated manual deployment guide (truth source for the Ansible). |
| [`HANDOFF.md`](HANDOFF.md) | Build history + lessons baked into the Ceph automation. |

## The two storage use cases

| Cluster | Storage | How |
|---|---|---|
| `ocp-internal` | **ODF internal** | self-contained Ceph inside OCP (OSDs on gp3 EBS) |
| `ocp-external` | **ODF external** | connects to the existing **ceph9** cluster over VPC peering |

## How it fits together (phases)

| Phase | Script | Result |
|---|---|---|
| ceph9 | `ansible/playbooks/site.yml` | RHCS 9 cluster, HEALTH_OK |
| 0 | `ocp-odf/phase0-prereqs/00-prereqs.sh` | pre-flight checks |
| 1 | `ocp-odf/phase1-deploy/deploy-cluster.sh both` | both OCP clusters (own VPCs) |
| 2 | `ocp-odf/phase2-network/peer-external-to-ceph.sh` | peer `ocp-external` ↔ ceph9 VPC |
| 3a | `ocp-odf/phase3-storage/odf-internal.sh` | ODF internal on `ocp-internal` |
| 3b | `ocp-odf/phase3-storage/odf-external.sh` | ODF external on `ocp-external` → ceph9 |
| 4 | `ocp-odf/demo-app/deploy.sh` | build + deploy the storage demo app |

Each script is idempotent and re-runnable. Credentials come only from a gitignored
`ocp-odf/env.sh` (copy from `env.sh.example`) — no secrets are committed.

## Storage abstractions used by the demo app

| Endpoint | Backed by | Kubernetes object |
|---|---|---|
| Block | Ceph RBD | `PersistentVolumeClaim` (RWO), mounted |
| File | CephFS | `PersistentVolumeClaim` (RWX), mounted |
| Object | Ceph RGW (S3) | `ObjectBucketClaim` (not a PVC — object storage is API-accessed, not mounted) |

## Status

All phases have been built and verified end-to-end against this lab: both OCP
clusters healthy, `ocp-external` connected to ceph9 (matching FSID), and the demo
app round-trips uploads/downloads on all three endpoints into ceph9.

## Credentials & safety

`ocp-odf/env.sh` is the only place real credentials go and is gitignored, along
with `clusters/` (kubeconfigs), `*.pem`, and the ODF external-details JSON
(contains ceph keyrings). Never commit those.
