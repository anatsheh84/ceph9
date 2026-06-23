# ceph9 — Red Hat Ceph Storage 9 on AWS, with selectable cluster topologies

Idempotent Ansible automation that stands up a containerized **Red Hat Ceph
Storage 9** cluster on AWS RHEL instances (via `cephadm`), serving all four
endpoints — **Block (RBD), File (CephFS), Object (S3/RGW), and NFS**. One global
switch chooses the cluster shape at provision time:

| `ceph_topology` | Shape |
|---|---|
| `4node-twotier` *(default)* | 4 nodes · 2 device-class tiers (`ssd`/`hdd`) |
| `12node-tiered` | 12 nodes · 3 device-class tiers (`nvme`/`sas`/`sata`) |

This README is self-contained — it covers both topologies, the AWS sizing, the
run/validate/teardown flow, and the lessons baked into the roles. A companion
OpenShift + self-service storage portal lives under [`ocp-odf/`](ocp-odf/).

> **Repo docs:** this `README.md` is the user guide. [`HANDOFF.md`](HANDOFF.md)
> is a separate engineering handover for later Claude Code / maintainer sessions.

## Repository layout

```
ansible/                     # the Ceph 9 build (this guide)
  provision.sh               # interactive topology picker -> runs site.yml
  inventory/
    group_vars/all.yml       # shared knobs (no secrets)
    topologies/<name>.yml    # per-topology profiles (4node-twotier / 12node-tiered)
    hosts.yml                # group-only skeleton (hosts added at runtime)
  playbooks/{00..07,site,validate,99_destroy}.yml
  roles/{aws_infra,rhel_prep,ssh_trust,cephadm_preflight,cephadm_bootstrap,
         ceph_cluster,ceph_services,smoke_tests,aws_teardown}/
  bin/ceph9-access           # post-build laptop access helper (tunnels + creds)
ocp-odf/                     # companion: 2x OpenShift 4.21 + ODF + storage portal
HANDOFF.md                   # engineering handover (separate from this guide)
```

## Topologies side-by-side

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

### 12-node daemon placement

For each tier, nodes 01–04 carry these daemons:

```
 node 01: _admin · MON · MGR · MDS active      · OSD     (nvme01 = bootstrap)
 node 02: RGW (x2)                             · OSD     (+ Grafana on nvme02)
 node 03: NFS-Ganesha · MDS standby-replay     · OSD
 node 04: NFS-Ganesha · RGW (x2)               · OSD
```

Cluster-wide totals: 3 MON, 3 MGR, `_admin` on all 3 MON hosts, 12 RGW daemons
across 6 hosts, 6 NFS-Ganesha, 6 MDS (3 active + 3 standby-replay for 3 separate
CephFS filesystems), 1 Grafana, 12 OSDs (one per node).

### 12-node pools and CRUSH rules

- **CRUSH rules:** one replicated rule per device class — `rule-nvme`, `rule-sas`,
  `rule-sata` (`default` root, failure domain `host`).
- **RBD:** one replicated pool per tier — `rbd-nvme`, `rbd-sas`, `rbd-sata`.
- **CephFS:** three separate filesystems — `cephfs-{nvme,sas,sata}`, each with its
  own data pool on the tier rule, and **all three metadata pools pinned to
  `rule-nvme`** (fast metadata regardless of data tier). 1 active + 1
  standby-replay MDS per filesystem.
- **RGW:** single realm/zone, three storage classes `STANDARD_{NVME,SAS,SATA}`
  backed by three data pools on their tier rules. RGW system pools (root, index,
  meta, log, control) and the default data pool are pinned to `rule-nvme`.
- **NFS:** one NFS-Ganesha cluster re-exporting the three CephFS filesystems as
  three exports (`/cephfs-nvme`, `/cephfs-sas`, `/cephfs-sata`).

### AWS instance + EBS sizing per tier (placeholders)

AWS has no literal SAS/SATA media, so the three tiers are **logical CRUSH device
classes** backed by different EBS types. The class is a label set explicitly at
OSD creation. Adjust in `ansible/inventory/topologies/12node-tiered.yml`
(`ceph_tiers`).

| Tier | CRUSH class | EBS type | Size | Instance type |
|---|---|---|---|---|
| nvme | `nvme` | `gp3` (SSD) | 500 GiB | `m5.2xlarge` |
| sas  | `sas`  | `st1` (throughput HDD) | 500 GiB | `m5.2xlarge` |
| sata | `sata` | `sc1` (cold HDD) | 500 GiB | `m5.2xlarge` |

(`st1`/`sc1` have a 125 GiB minimum; instance types are placeholders — size for
the real workload.)

### Single-disk-per-host (12-node)

The 12-node OSD logic creates exactly **one OSD per host** via a per-tier cephadm
drivegroup spec
([`osd-spec.yml.j2`](ansible/roles/ceph_cluster/templates/osd-spec.yml.j2)) that
sets `crush_device_class` at creation. It never uses `--all-available-devices`
and never scans for spare volumes. The data disk is matched **by size** (≥ 80% of
the tier's volume size), not by device name, because **AWS Nitro NVMe enumeration
is non-deterministic** — the attached EBS volume is not always `/dev/nvme1n1`
(sometimes it is `/dev/nvme0n1` with the root disk on `nvme1n1`), so a fixed path
can land on the OS disk. To use more than one disk per host later, change the
spec's `data_devices` matcher and raise the OSD-count wait in `ceph_cluster`.

### AWS NLB front-end (12-node)

There is **no cephadm `ingress`** in the 12-node design (keepalived's VIP can't
float in a VPC). The playbook only makes the gateway daemons listen on
well-known per-host ports; the NLB target groups are created **outside this repo**
and register each daemon by `host_ip:port`.

| Service | Hosts (label) | Per-host ports | NLB targets |
|---|---|---|---|
| RGW (S3) | node02/04 of each tier (`rgw`) | `8080` and `8081` (cephadm auto-increments the 2nd daemon) | 6 hosts × {8080,8081} = 12 |
| NFS | node03/04 of each tier (`nfs`) | `2049` | 6 hosts × 2049 = 6 |

RBD and CephFS need no LB — clients use the mon quorum + OSD/MDS directly.

## Validated architecture & sizing (per the RHCS 9 docs)

The 4-node baseline follows the Red Hat Ceph Storage 9 Installation Guide:

- **Replica-3 floor = 4 nodes.** "The minimum size for a storage cluster with
  three replicas is four nodes… an extra node to avoid extended degraded
  periods." The first OSD node is also the bootstrap/`_admin` host.
- **Colocation:** RGW is colocated with OSDs (Red Hat-recommended for
  performance); MON+MGR count as one for colocation; two of the same daemon are
  not colocated on one host.
- **Capacity (size=3):** usable ≈ raw / 3, keep utilization below ~70–75%
  (`nearfull` triggers at 85%). 500 GB usable → 4× 500 GB gp3 = 2 TB raw / 4
  OSDs; 1 TB usable → 8× 500 GB = 4 TB raw / 8 OSDs.
- **OSD disk requirements:** the data drive must be > 5 GB, have no partitions,
  filesystem, mount, or prior BlueStore signature, and **cannot be the OS disk**.
- **Instances:** `m5.2xlarge` Ceph nodes, `t3.large` bastion (defaults; both
  topologies). RHCS 9 is supported **only** on container-based (`cephadm`)
  deployments.

## Environment-variable contract (no secrets in any YAML)

| Var | Required | Purpose |
|---|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | yes | AWS auth |
| `AWS_DEFAULT_REGION` | yes | e.g. `us-east-2` |
| `RH_USER` / `RH_PASS` | yes | Red Hat customer portal login (subscriptions) |
| `CEPH_DASHBOARD_PASSWORD` | no | sets the dashboard/mgr `admin` password (else the random bootstrap one stands) |
| `ADMIN_CIDR` | no | source CIDR for SSH/Dashboard/S3/NFS. Default `auto` (controller's public IP/32) |
| `ROUTE53_ZONE_ID` / `ROUTE53_DOMAIN` | no | optional DNS records; skipped if empty |

Credentials are read from the environment only; override any
`inventory/group_vars/all.yml` knob with `-e key=value`.

## Controller prerequisites

```bash
pip install ansible-core boto3 botocore
brew install awscli jq          # macOS  (or: dnf install -y awscli jq)
cd ansible && ansible-galaxy collection install -r requirements.yml
```

## Run it

```bash
cd ansible
export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...  AWS_DEFAULT_REGION=us-east-2
export RH_USER='you@example.com'  RH_PASS='...'  CEPH_DASHBOARD_PASSWORD='...'

./provision.sh            # interactive: 1) 4-node two-tier   2) 12-node 3-tier
```

`provision.sh` asks which topology to build, then runs `site.yml` with the global
`ceph_topology`. Non-interactive / CI:

```bash
ansible-playbook -e ceph_topology=4node-twotier  playbooks/site.yml   # or 12node-tiered
```

`site.yml` runs phases **00–07** for 4-node (base + HDD tier + NFS HA + internal
NLBs); for 12-node it runs **00–04** (all three tiers are built in phase 04) and
skips the 4-node-only smoke/06/07 phases. **Expected runtime:** 4-node ≈ 30–45
min; 12-node ≈ 45–70 min (rhel_prep + preflight scale with node count).

## Validate (12-node)

```bash
ansible-playbook -e ceph_topology=12node-tiered \
  playbooks/00_provision_aws.yml playbooks/validate.yml
```

`validate.yml` asserts (fail-loud): HEALTH_OK; 3 MON / 3 MGR; 12 OSDs, 4 per
device class; 3 CephFS each with a standby-replay MDS; 12 RGW across 6 hosts; 6
NFS daemons; `_admin` on the 3 MON hosts; per-tier pools on the correct CRUSH
rule; all CephFS-metadata + non-data RGW pools on `nvme`.

## Access the cluster from your laptop

Dashboard/Grafana/S3/NFS live in the private subnet, reached via SSH
port-forwards through the bastion. `bin/ceph9-access` opens the tunnels and
prints a credentials cheat-sheet:

```bash
cd ansible
./bin/ceph9-access            # all tunnels: dashboard 8443 · grafana 3000 · S3 8080 · NFS 2049
./bin/ceph9-access --info     # print URL + verified creds, no tunnel
./bin/ceph9-access --status   # what's tunneled now
./bin/ceph9-access --stop     # stop the script-managed tunnel
```

It **verifies** the dashboard password against the live API (and caches the
verified value at `~/.ceph9-dashboard-pw`), so it shows the password that
actually works — the build-set `CEPH_DASHBOARD_PASSWORD`, not the stale bootstrap
one. Open `https://localhost:8443/` (admin), `http://localhost:3000/` (Grafana),
`http://localhost:8080` (S3 keys printed); mount NFS with
`sudo mount -t nfs4 localhost:/cephfs /mnt/nfs`.

## Teardown

```bash
ansible-playbook -e ceph_topology=<topology> playbooks/99_destroy.yml
```

For 4-node, also delete the phase-07 NLBs/target-groups if not handled. For
12-node, delete the externally-created NLB target groups.

## Lessons baked into the roles

Each workaround discovered during real builds is encoded as Ansible logic:

1. **`manage_repos=0`** is the RHEL AMI default — `rhel_prep` runs
   `subscription-manager config --rhsm.manage_repos=1` before enabling repos.
2. **cephadm-ansible 5.0.2 folded-scalar bug** — a literal space appears in the
   rhceph repo name; `cephadm_preflight` patches it in place.
3. **cephadm-ansible 5.0.2 undefined vars** — pass `client_group=clients`; treat
   the informational "Package Installation Check Result" failure as non-fatal.
4. **Kernel update reboot** — `rhel_prep` reboots when `dnf update` changed the kernel.
5. **Private subnet + NAT GW**, no public IPs on Ceph nodes.
6. **Two pubkeys per node** — `ssh_trust` (ec2-user) and `/etc/ceph/ceph.pub`
   (cephadm); `sudo ssh-copy-id` fails since root has no key.
7. **RGW S3 from EC2** — set `AWS_EC2_METADATA_DISABLED=true` +
   `AWS_DEFAULT_REGION=us-east-1` or RGW rejects with `IllegalLocationConstraintException`.
8. **`ProxyJump` ignores `-o` flags** — use an explicit `ProxyCommand` to carry
   `StrictHostKeyChecking=accept-new` to the inner hop.
9. **Stale host keys across rebuilds** — fixed private IPs + reusable bastion IP;
   `aws_infra` scrubs `known_hosts` before first SSH, `aws_teardown` clears them.
10. **cephadm-preflight can hang** — wrapped in `timeout 900` with `ControlMaster=no`;
    an rc-124 timeout is tolerated so a checks-phase hang never stalls the build.
11. **Dashboard password flag** — Ceph 9 wants `--force-password` (not
    `--force-password-policy`); set with `no_log: true`.
12. **Group-only inventory skeleton** — `hosts.yml` lists no concrete hosts;
    `aws_infra` `add_host`s them at runtime, so 4-node (`ceph01-04`) and 12-node
    (`nvme/sas/sata01-04`) names never collide as phantom hosts.
13. **Nitro NVMe naming** — 12-node OSD spec matches the data disk by size, not
    `/dev/nvme1n1` (see single-disk section).
14. **RGW lazy pools** — `control/meta/log` are created after the first pin pass;
    a final retried re-pin guarantees all non-data RGW pools land on `rule-nvme`.

## Companion: OpenShift + self-service storage portal

[`ocp-odf/`](ocp-odf/) builds two OpenShift 4.21 clusters (ODF internal and
external-mode against this Ceph cluster) and a Red Hat-themed storage portal that
both consumes and self-service-provisions S3/NFS/CephFS/RBD across tiers. See
[`ocp-odf/README.md`](ocp-odf/README.md) and
[`ocp-odf/demo-app/README.md`](ocp-odf/demo-app/README.md).

## Limitations & alternatives considered

- **RGW realm/zone:** uses the single cephadm-created **default** realm/zone (one
  realm, one zone) with three storage classes on `default-placement`. A
  custom-named realm was evaluated but not used — it changes pool naming and adds
  period-commit complexity for no functional gain at single-zone scale.
- **cephadm `ingress` → AWS NLB:** keepalived's VIP can't float in a VPC; for
  12-node the NLB lives outside this repo.
- **Three separate clusters** (one per tier) was considered and not implemented —
  a single cluster with three CRUSH device classes meets the requirement with one
  control plane and shared MON/MGR.
- **NVMe/SAS/SATA are logical classes**, not real media (see sizing table).
- **Single AZ** + cluster placement group; multi-AZ/rack-aware CRUSH is a future
  extension.
