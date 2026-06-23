# Cluster topologies

This repo's Ansible can build two cluster shapes, selected at provision time. The
choice is a single global value, `ceph_topology`, which names a profile under
[`ansible/inventory/topologies/`](../ansible/inventory/topologies/). Every play
loads the matching profile via `vars_files`, so one value drives the whole run.

```bash
cd ansible
./provision.sh                       # interactive menu (1 or 2)
# or non-interactive:
ansible-playbook -e ceph_topology=4node-twotier  playbooks/site.yml
ansible-playbook -e ceph_topology=12node-tiered  playbooks/site.yml
```

## Side-by-side

| | **4node-twotier** (default) | **12node-tiered** |
|---|---|---|
| Nodes | 4 | 12 (4 nvme + 4 sas + 4 sata) |
| Device-class tiers | 2: `ssd`, `hdd` | 3: `nvme`, `sas`, `sata` |
| Tier model | build gp3 OSDs as `ssd`, attach 1 st1 disk and **reclassify** to `hdd` (phase 06) | class set **at OSD creation** per node group |
| Data disks / node | 2 gp3 (+1 st1 in phase 06) | **exactly 1** |
| CRUSH rules | `replicated_ssd`, `replicated_hdd` | `rule-nvme`, `rule-sas`, `rule-sata` |
| Replication | replica 3, failure domain host | replica 3, failure domain host |
| RBD pools | `rbd`, `rbd-hdd` | `rbd-nvme`, `rbd-sas`, `rbd-sata` |
| CephFS | 1 filesystem, 2 data pools | **3 filesystems**, metadata all on nvme |
| RGW | 1 zone, STANDARD + HDD classes, 1/host | 1 zone, STANDARD_NVME/SAS/SATA, **2/host** |
| MON / MGR | 3 / 2 | 3 / 3 |
| MDS | 1 active + 1 standby | 3 active + 3 standby-replay |
| RGW daemons | 2 | **12** (6 hosts × 2) |
| NFS-Ganesha | 2 | **6** |
| Client LB | internal AWS NLB (phase 07, in-repo) | AWS NLB **configured outside this repo** |
| `_admin` label | ceph01 | nvme01, sas01, sata01 (bootstrap = nvme01) |

## AWS instance + EBS sizing per tier (placeholders)

AWS has no literal SAS/SATA media, so the three tiers are **logical CRUSH device
classes** backed by different EBS types. Adjust in
[`12node-tiered.yml`](../ansible/inventory/topologies/12node-tiered.yml) →
`ceph_tiers`.

| Tier | CRUSH class | EBS type | Size | Instance type |
|---|---|---|---|---|
| nvme | `nvme` | `gp3` (SSD) | 500 GiB | `m5.2xlarge` |
| sas  | `sas`  | `st1` (throughput HDD) | 500 GiB | `m5.2xlarge` |
| sata | `sata` | `sc1` (cold HDD) | 500 GiB | `m5.2xlarge` |

(Instance types are placeholders — size them for the real workload. `st1`/`sc1`
have a 125 GiB minimum.)

## Single-disk-per-host constraint

The 12-node OSD logic takes **one explicit device** per host —
`osd_data_device` (default `/dev/nvme1n1`, the in-guest name of the single
attached EBS data volume on Nitro instances). It deploys exactly one OSD per
host via a per-tier cephadm drivegroup spec
([`osd-spec.yml.j2`](../ansible/roles/ceph_cluster/templates/osd-spec.yml.j2))
that lists the device path and `crush_device_class`. It never uses
`--all-available-devices` and never scans for spare/unmounted volumes.

**To use more than one disk per host later:** change the drivegroup spec to a
`data_devices` matcher (e.g. `rotational: 0` or a size filter) instead of an
explicit `paths:` list, attach the extra volumes in `aws_infra`, and raise the
OSD-count wait in `ceph_cluster` (`== ceph_nodes | length` → `× disks`).

## AWS NLB front-end (12-node)

There is **no cephadm `ingress`** in the 12-node design. The playbook's job ends
at making the gateway daemons listen on well-known per-host ports; the NLB target
groups are created **outside this repo** and register each daemon by
`host_ip:port`.

| Service | Hosts (label) | Per-host listen ports | NLB target group registers |
|---|---|---|---|
| RGW (S3) | nvme02/04, sas02/04, sata02/04 (`rgw`) | `8080` and `8081` (cephadm auto-increments the 2nd daemon) | each host × {8080, 8081} = 12 targets |
| NFS-Ganesha | nvme03/04, sas03/04, sata03/04 (`nfs`) | `2049` | each host × 2049 = 6 targets |

RBD and CephFS need no LB — clients use the mon quorum + OSD/MDS directly.

> For stateful NFS behind an NLB, enable source-IP stickiness and connection
> termination on the target group (see the NFS notes in the repo history).

## How to run, end to end

**Prerequisites:** `aws` CLI v2, `ansible-core`, `jq`, an SSH-capable controller;
env vars `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`,
`RH_USER`, `RH_PASS`, and optionally `CEPH_DASHBOARD_PASSWORD`, `ADMIN_CIDR`,
`ROUTE53_ZONE_ID`/`ROUTE53_DOMAIN`. Run `ansible-galaxy collection install -r
requirements.yml` once.

```bash
cd ansible
export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...  AWS_DEFAULT_REGION=us-east-2
export RH_USER=...  RH_PASS=...  CEPH_DASHBOARD_PASSWORD=...

./provision.sh            # choose 1 (4node) or 2 (12node)

# 12-node verification (idempotent; 00 only repopulates the in-memory inventory):
ansible-playbook -e ceph_topology=12node-tiered \
  playbooks/00_provision_aws.yml playbooks/validate.yml
```

**Expected runtime** (us-east-2, m5.2xlarge): 4-node ≈ 30–45 min; 12-node ≈
45–70 min (rhel_prep + preflight scale with node count). Teardown:
`ansible-playbook -e ceph_topology=<topology> playbooks/99_destroy.yml`.

## Known limitations & alternatives considered

- **RGW realm/zone.** This branch uses the single cephadm-created **default**
  realm/zone (one realm, one zone) and adds the three storage classes to its
  `default-placement`. A custom-named realm/zonegroup/zone was evaluated but not
  used — it changes RGW pool naming and adds period-commit complexity for no
  functional gain at single-zone scale.
- **cephadm `ingress` was evaluated and replaced with AWS NLB.** keepalived's
  VIP can't float inside an AWS VPC; an internal NLB is the AWS-native
  equivalent. For 12-node the NLB lives outside this repo.
- **Three separate clusters** (one per tier) was considered and **not**
  implemented in this branch — a single cluster with three CRUSH device classes
  meets the requirement with one control plane and shared MON/MGR.
- **NVMe/SAS/SATA are logical classes**, not real media (see sizing table).
- **Single AZ.** All nodes land in one AZ + cluster placement group, as in the
  4-node design. Multi-AZ/rack-aware CRUSH is a future extension.
