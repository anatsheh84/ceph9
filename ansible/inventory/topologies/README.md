# Topology profiles

Each file here is a **topology profile** — a self-contained set of the
topology-specific variables for one cluster shape. Both supported topologies are
expressed the *same way*, so adding a third later is a copy-and-edit.

| File | `ceph_topology` value | Shape |
|---|---|---|
| `4node-twotier.yml` | `4node-twotier` (default) | 4 nodes, 2 device-class tiers (ssd/hdd) — the original cluster |
| `12node-tiered.yml` | `12node-tiered` | 12 nodes, 3 device-class tiers (nvme/sas/sata) |

## How a profile is selected and loaded

- `ceph_topology` (default in `group_vars/all.yml`, overridable with
  `-e ceph_topology=…` or via `provision.sh`) names the profile.
- Every play loads it with
  `vars_files: ["{{ playbook_dir }}/../inventory/topologies/{{ ceph_topology }}.yml"]`,
  so the same value drives all eight phases.
- **Shared, topology-agnostic** vars (AWS region, network CIDRs, credentials,
  base ports, bastion sizing) stay in `group_vars/all.yml`. A profile only holds
  what differs between topologies.

## Variable contract

Both profiles define:

| Variable | Meaning |
|---|---|
| `ceph_bootstrap_node` | the single host that runs `cephadm bootstrap` + `ceph orch`. The Ansible `admin` group is built from this name; the `_admin` Ceph label is applied separately to every node listing it. |
| `ceph_nodes` | list of `{name, ip, labels[]}`; `labels` drive daemon placement and inventory groups. |

`4node-twotier` adds: `osd_disks_per_node`, `osd_disk_gb`, base pool names, and
the phase-06/07 tier + NLB vars (unchanged from the original `all.yml`).

`12node-tiered` adds: per-node `tier`; `ceph_tiers` (device class, EBS type/size,
instance type, CRUSH rule, RBD pool, CephFS name, RGW storage class + data pool
per tier); `ceph_tier_order`; `osd_data_device` (single disk, set at OSD-create
time); `tiered_meta_crush_rule` (all metadata + non-data RGW pools pin to nvme);
`rgw_count_per_host`; realm/zone names; and `nfs_exports`.

## Placeholders

IPs and hostnames in `12node-tiered.yml` are placeholders following the existing
`10.20.2.x` convention. The `nvme/sas/sata` tiers are **logical CRUSH device
classes** (AWS has no SAS/SATA media); they are backed by `gp3/st1/sc1` EBS
respectively and the class is set explicitly at OSD creation. To change the
single-disk-per-host assumption later, see the repo [`README.md`](../../../README.md).
