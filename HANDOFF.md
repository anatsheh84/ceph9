# Ceph 9 on AWS — final handoff

**Status: FINALIZED.** Three clean ansible-driven builds completed end-to-end
on the validated architecture in `setup.md`. The automation under `ansible/`
is the canonical way to provision the lab.

## What was delivered

| | |
|---|---|
| `setup.md` | Manually validated deployment guide. Unchanged. |
| `ansible/` | 8 playbooks · 9 roles · syntax-clean · idempotent. End-to-end build in ~30–45 min. |
| `ansible/bin/ceph9-access` | Local-laptop helper that prints creds + opens SSH tunnels to dashboard / Grafana / S3 / NFS. |
| `ansible/README.md` | Three-step user docs (set env → run ansible → run `ceph9-access`). |

## Three-step usage

```bash
# 1. Set environment
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-2
export RH_USER='you@example.com'
export RH_PASS='...'
# optional
export ADMIN_CIDR="$(curl -s checkip.amazonaws.com)/32"
export ROUTE53_ZONE_ID=Z3OMFIUXX3RAEI
export ROUTE53_DOMAIN=sandbox1580.opentlc.com

# 2. Build with Ansible (≈ 30–45 min)
cd ansible
ansible-galaxy collection install -r requirements.yml   # one-time
ansible-playbook playbooks/site.yml

# 3. Open access from your laptop (prints creds, opens tunnels, foreground)
./bin/ceph9-access
```

Then open `https://localhost:8443/` in your browser. Dashboard credentials are
printed by the script just above the tunnel-ready line.

## Build results

| | BUILD #1 | BUILD #2 | BUILD #3 (final) |
|---|---|---|---|
| Start | clean (tagged) AWS account | clean teardown of #1 | clean teardown of #2 |
| Wall-clock | ~55 min (incl. fix loop) | ~95 min (transient preflight hang) | ~30 min |
| `failed=0` everywhere | ✅ | ✅ | ✅ |
| HEALTH_OK after build | ✅ | ✅ | ✅ |
| `RBD/CephFS/NFS/S3` smoke | ✅ | ✅ | ✅ |
| `ceph9-access --info` | n/a | n/a | ✅ prints creds |
| Dashboard via tunnel | manual ssh -L | manual ssh -L | `ceph9-access` → HTTP 200 |

**Final cluster (live now):**

```
cluster:
  id:     4fcbf51c-4f9f-11f1-9714-02ece24acab7
  health: HEALTH_OK
  mon: 3 daemons, quorum ceph01,ceph02,ceph03
  mgr: ceph01.sliawp(active), standbys: ceph02.brcpra
  mds: 1/1 daemons up, 1 standby
  osd: 8 osds: 8 up, 8 in
  rgw: 2 daemons active
data:
  pools: 11 pools, 625 pgs   (all active+clean)
  usage: 237 MiB used, 3.9 TiB / 3.9 TiB avail
```

Bastion `18.218.25.156` is up. Tear down with
`ansible-playbook playbooks/99_destroy.yml` when finished.

## Bugs found and fixed during BUILD #1 (baked into the roles)

1. **`Labels` instance tag with commas** in value broke AWS CLI shorthand. Switched separator to `|` in `aws_infra`.
2. **`ProxyJump` implicit ssh ignores `-o StrictHostKeyChecking`**. Replaced with explicit `ProxyCommand` (`aws_infra` and `bin/ceph9-access`).
3. **Jinja in python heredoc gets templated by Ansible**. Wrapped the `cephadm-preflight.yml` patch in `{% raw %}/{% endraw %}` in `cephadm_preflight/tasks/main.yml`.
4. **`cephadm` not in `command:` module PATH**. Switched all `cephadm`/`cephadm shell` calls to absolute `/usr/sbin/cephadm`.
5. **`Run cephadm-preflight playbook` ran as root, root has no SSH key**. Added `become: false` so it runs as ec2-user whose key was created by `ssh_trust`.
6. **`command:` splits `--placement="label:rgw count-per-host:1"`** at the space. Switched the RGW apply task to `shell:`.

## Issues found during the final validation build

1. **AWS public IP rotation** — the controller's public IP changed between sessions; the SG ingress was opened to the *first* IP. Re-running with the new `ADMIN_CIDR` resolved it. Worth documenting in README that any controller-side IP change requires either a teardown+rebuild or a manual SG patch.
2. **Preflight inner playbook hang after package install** — the cephadm-ansible 5.0.2 preflight sometimes stalls in its post-install "checks.yml" phase. The outer `failed_when` already tolerates the well-known `infra_pkgs` failure, and a kill-and-continue path works; observed in BUILD #2 but did NOT recur in BUILD #3.

Neither blocked the final build.

## Operational notes

* The cluster placement group keeps the 4 OSD VMs on the same low-latency rack — needed to meet the doc's 10 Gb/s recommendation.
* `cephadm-preflight` is the slowest step (5–15 min). Be patient.
* The first dashboard login forces a password change. The script prints the bootstrap password; rotate after first login.
* AWS sandbox creds shared during this session are still live. **Rotate them.**

## File map

```
ceph9/
├── setup.md                          # manual guide (truth source)
├── HANDOFF.md                        # this file
└── ansible/
    ├── README.md                     # user-facing docs + env-var contract
    ├── ansible.cfg
    ├── requirements.yml
    ├── bin/
    │   └── ceph9-access              # post-build laptop helper
    ├── inventory/
    │   ├── hosts.yml                 # short-name skeleton
    │   └── group_vars/all.yml        # all knobs — no secrets
    ├── playbooks/
    │   ├── 00_provision_aws.yml      # aws_infra
    │   ├── 01_prepare_hosts.yml      # rhel_prep + ssh_trust + cephadm_preflight
    │   ├── 02_bootstrap.yml          # cephadm_bootstrap
    │   ├── 03_cluster.yml            # ceph_cluster (host add, labels, OSDs)
    │   ├── 04_services.yml           # ceph_services (RBD, CephFS, RGW, NFS)
    │   ├── 05_smoke.yml              # smoke_tests
    │   ├── 99_destroy.yml            # aws_teardown
    │   └── site.yml                  # 00..05 imported in order
    └── roles/{aws_infra,rhel_prep,ssh_trust,cephadm_preflight,
               cephadm_bootstrap,ceph_cluster,ceph_services,smoke_tests,aws_teardown}/
```
