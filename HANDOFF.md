# ceph9 — engineering handover

Maintainer / Claude-Code handover. The user-facing guide is [`README.md`](README.md);
this file is the "what's the state, what's tricky, what to know next" companion.

## Current status

- **Two selectable topologies**, chosen by the global `ceph_topology` var (default
  `4node-twotier`): `4node-twotier` (4 nodes, ssd/hdd) and `12node-tiered`
  (12 nodes, nvme/sas/sata). Picker: `ansible/provision.sh`.
- **`4node-twotier`** — finalized; built clean end-to-end 3× (table below).
- **`12node-tiered`** — implemented on branch **`12-node-cluster`** (pushed to
  `origin`, **no PR yet** — review locally first). Built + validated end-to-end
  on a blank AWS account (`failed=0`, `validate.yml` all green).
- **Docs consolidated:** `README.md` is now self-contained (the old
  `ansible/README.md`, `docs/topologies.md`, and `setup.md` were folded in and
  removed). This `HANDOFF.md` stays separate.

## How it's structured (mechanism a future session must know)

- **Topology profiles:** `ansible/inventory/topologies/<name>.yml` hold all
  topology-specific vars (`ceph_nodes`, tiers, pools, …). `group_vars/all.yml`
  holds only shared vars + the `ceph_topology` default. Every play loads the
  selected profile via `vars_files: …/topologies/{{ ceph_topology }}.yml`.
- **`hosts.yml` is a group-only skeleton** (no concrete hosts). `aws_infra`
  `add_host`s real nodes at runtime, so 4-node (`ceph01-04`) and 12-node
  (`nvme/sas/sata01-04`) names never collide. The Ansible `admin` group = the
  single `ceph_bootstrap_node`; the Ceph `_admin` *label* is applied separately
  to all MON hosts.
- **`ceph_services`** dispatches: `base_services.yml` (4-node, original logic) vs
  `tiered.yml` → `tiered_{crush_pools,cephfs,rgw,nfs}.yml` (12-node).
- **OSDs:** 4-node uses `--all-available-devices`; 12-node applies one cephadm
  drivegroup spec per tier with `crush_device_class` at creation, matching the
  single data disk **by size** (Nitro-safe).
- For 12-node, `site.yml` skips phases 05/06/07 (smoke + 4-node NLB/tier logic);
  everything is built in phase 04. Verify with `playbooks/validate.yml`.

## 12-node validation evidence (blank AWS account, end-to-end)

`site.yml` → all 13 hosts `failed=0`. `validate.yml` → `VALIDATE_EXIT=0`:

```
health=HEALTH_OK  mon=3  mgr=3 (1 active + 2 standby)  osd=12 (4 per class)
cephfs=3 (each + standby-replay)  rgw=12 across 6 hosts  nfs=6
_admin on 3 MON hosts  per-tier pools -> correct rules  meta + rgw-system on rule-nvme
```

Two real bugs were found *because* we test-deployed, and fixed in-code:
1. **AWS Nitro NVMe naming is non-deterministic** — the data disk isn't always
   `/dev/nvme1n1` (on some hosts it's `nvme0n1`, root on `nvme1n1`). The OSD spec
   now matches the data disk **by size** (≥ 80% of tier size), not by path.
2. **RGW system pools are created lazily** — `control/meta/log` appear after the
   first pin pass. A final retried re-pin guarantees all non-data RGW pools land
   on `rule-nvme`. (Also: `validate.yml` counts MGRs via `ceph mgr dump`, not
   `ceph mgr stat` which has no standbys list.)

## 4-node build history (finalized)

| | BUILD #1 | BUILD #2 | BUILD #3 |
|---|---|---|---|
| Wall-clock | ~55 min (incl. fix loop) | ~95 min (transient preflight hang) | ~30 min |
| `failed=0` / HEALTH_OK | ✅ | ✅ | ✅ |
| RBD/CephFS/NFS/S3 smoke | ✅ | ✅ | ✅ |

Bugs found and fixed during BUILD #1 (baked into roles): comma in `Labels` tag
(switched to `|`); `ProxyJump` ignoring `-o` (explicit `ProxyCommand`); Jinja in a
python heredoc (`{% raw %}`); `cephadm` not on `command:` PATH (absolute
`/usr/sbin/cephadm`); preflight running as root with no key (`become: false`);
`command:` splitting `--placement="… …"` (switched to `shell:`). Full list of
baked-in lessons is in the README.

## Operational notes for the next session

- **cephadm-preflight is the slowest step** (5–15 min); wrapped in `timeout 900`,
  rc-124 tolerated — a hang can't stall the build.
- **12 nodes share one bastion** → `wait_for_connection` hits sshd MaxStartups and
  connects via retries (slow, not stuck). Not a failure.
- **Dashboard password:** set `CEPH_DASHBOARD_PASSWORD`; `bin/ceph9-access`
  verifies the live password and caches it at `~/.ceph9-dashboard-pw`.
- **Never commit secrets.** `env.sh`, `*.pem`, `.provisioned.json`, `clusters/`,
  `*.log`, and `*.external-details.json` are gitignored; everything reads creds
  from env vars. Any AWS/RH/GitHub creds shared in a session should be rotated.
- **AWS controller IP rotation** can leave the SG opened to a stale `ADMIN_CIDR`;
  re-run with the new CIDR or set `ADMIN_CIDR=0.0.0.0/0` for throwaway sandboxes.

## File map

```
ceph9/
├── README.md                         # self-contained user guide
├── HANDOFF.md                        # this file
├── ocp-odf/                          # companion: OpenShift + ODF + storage portal
└── ansible/
    ├── provision.sh                  # interactive topology picker
    ├── ansible.cfg · requirements.yml
    ├── bin/ceph9-access              # post-build laptop helper (verifies dash pw)
    ├── inventory/
    │   ├── hosts.yml                 # group-only skeleton (runtime add_host)
    │   ├── group_vars/all.yml        # shared knobs + ceph_topology default
    │   └── topologies/{4node-twotier,12node-tiered}.yml + README.md
    ├── playbooks/
    │   ├── 00..05 · 06_storage_tiers · 07_nfs_ha_lb   # 4-node uses 00-07
    │   ├── validate.yml              # 12-node assertions
    │   ├── 99_destroy.yml · site.yml # site.yml: 12-node runs 00-04, skips 05-07
    └── roles/{aws_infra,rhel_prep,ssh_trust,cephadm_preflight,cephadm_bootstrap,
               ceph_cluster(+templates/osd-spec.yml.j2),
               ceph_services(+tiered_*.yml),smoke_tests,aws_teardown}/
```
