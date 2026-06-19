# Ceph 9 on AWS RHEL — automated lab build

Idempotent Ansible automation for the architecture validated in
`../setup.md`: a 4-node containerized RHCS 9 cluster on `m5.2xlarge`
RHEL 9 instances behind a bastion, with RBD, CephFS, S3 (RGW) and NFS
endpoints.

## What it builds

```
                                  +-- ceph01 (admin/mon/mgr/osd/mds)
   bastion (public) ── NAT GW ── +-- ceph02 (mon/mgr/osd/rgw)
   t3.large                       +-- ceph03 (mon/osd/rgw/nfs)
                                  +-- ceph04 (osd/mds)
              cluster placement group, private subnet 10.20.2.0/24
```

* 8 OSDs across 4 hosts → ~1 TiB usable @ replica 3
* `cephadm`-managed daemons (RHCS 9 only supports container-based deployments)
* Endpoints: RBD pool, CephFS volume, RGW S3 (port 8080), NFS-Ganesha exporting `/cephfs`
* Optional Route 53 DNS for `ceph0{1..4}`, `bastion`, `dashboard`, `s3`

## Environment-variable contract (no secrets in any YAML)

| Var | Required | Purpose |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | yes | AWS auth (read by `aws` CLI and amazon.aws) |
| `AWS_SECRET_ACCESS_KEY` | yes | AWS auth |
| `AWS_DEFAULT_REGION` | yes | e.g. `us-east-2` |
| `RH_USER` | yes | Red Hat customer portal username |
| `RH_PASS` | yes | Red Hat customer portal password |
| `ADMIN_CIDR` | no | Source CIDR for SSH/Dashboard/S3/NFS. Default `auto` (controller's public IP/32). |
| `ROUTE53_ZONE_ID` | no | Hosted zone ID; if empty, DNS is skipped |
| `ROUTE53_DOMAIN` | no | e.g. `sandbox1580.opentlc.com`; if empty, DNS is skipped |

You may also override anything in `inventory/group_vars/all.yml` with `-e key=value`.

## Controller prerequisites

```bash
pip install ansible-core boto3 botocore
ansible-galaxy collection install -r requirements.yml
brew install awscli jq      # macOS
# or:  dnf install -y awscli jq
```

## How to run — three steps

### Step 1 — set environment

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-2
export RH_USER='you@example.com'
export RH_PASS='...'

# optional
export ADMIN_CIDR="$(curl -s checkip.amazonaws.com)/32"
export ROUTE53_ZONE_ID=ZXXXXX
export ROUTE53_DOMAIN=example.com
```

### Step 2 — build the cluster with Ansible

```bash
cd ansible

# Full end-to-end build (≈ 30–60 min)
ansible-playbook playbooks/site.yml

# Or step-by-step
ansible-playbook playbooks/00_provision_aws.yml     # VPC, instances, NAT, DNS
ansible-playbook playbooks/01_prepare_hosts.yml     # RHEL subs, preflight
ansible-playbook playbooks/02_bootstrap.yml         # cephadm bootstrap
ansible-playbook playbooks/03_cluster.yml           # add hosts, labels, OSDs
ansible-playbook playbooks/04_services.yml          # RBD/CephFS/RGW/NFS
ansible-playbook playbooks/05_smoke.yml             # end-to-end smoke tests
```

When `05_smoke.yml` finishes, the cluster is HEALTH_OK and all four endpoints
have been exercised end-to-end. Ansible's job is **done**.

### Step 3 — open access from your laptop with `bin/ceph9-access`

The dashboard, Grafana, S3, and NFS endpoints live in the **private** subnet,
so you reach them via SSH port-forwards through the bastion. The
`bin/ceph9-access` helper script does everything in one command:

* writes a `/etc/hosts` block on the bastion so the forwards resolve `ceph01..04`
* fetches the dashboard bootstrap password from `/var/log/ceph/cephadm.log`
* fetches the S3 `labuser` access/secret keys from `radosgw-admin`
* prints the URL + credentials cheat-sheet
* opens the requested tunnel and stays in foreground (Ctrl-C to stop)

```bash
./bin/ceph9-access                # open all 4 tunnels (dashboard + grafana + S3 + NFS)
./bin/ceph9-access dashboard      # only the dashboard
./bin/ceph9-access --info         # print URL + creds, no tunnel
./bin/ceph9-access --status       # what's tunneled right now
./bin/ceph9-access --stop         # stop the script-managed tunnel
```

While `ceph9-access` runs, open these in your browser:

* Dashboard:  https://localhost:8443/   (login `admin` + the printed password — Ceph forces a password change on first login)
* Grafana:    http://localhost:3000/    (same admin/password)
* S3 endpoint: http://localhost:8080    (use the printed access/secret keys)

To mount the CephFS export over NFS while the tunnel is open:

```bash
sudo mount -t nfs4 localhost:/cephfs /mnt/nfs
```

> **Why a shell script for step 3, not Ansible?** Opening and *maintaining* an
> SSH tunnel on your laptop is a long-running local process. Ansible is the
> right tool for declarative configuration (steps 1–2). A small foreground
> script is the right tool for "keep this tunnel open while I work."

### Teardown

```bash
ansible-playbook playbooks/99_destroy.yml
```

## Lessons baked in

Each workaround discovered during the manual run is encoded as ansible logic:

1. **`manage_repos=0`** is the RHEL AMI default — `rhel_prep` runs
   `subscription-manager config --rhsm.manage_repos=1` before enabling repos.
2. **cephadm-ansible 5.0.2 packaging bug #1**: folded-scalar in
   `cephadm-preflight.yml` produces a literal space inside the rhceph repo
   name. `cephadm_preflight` patches it in place.
3. **cephadm-ansible 5.0.2 packaging bug #2**: undefined `client_group` and
   `infra_pkgs`. We pass `client_group=clients` and treat the informational
   "Package Installation Check Result" failure as non-fatal.
4. **Kernel update on a fresh RHEL AMI** triggers a reboot in `rhel_prep`
   when `dnf update` reports changes.
5. **Private subnet + NAT GW**, not public IPs on Ceph nodes.
6. **Two pubkeys per node**: `ec2-user@ceph01`'s key (`ssh_trust`) and
   `/etc/ceph/ceph.pub` (`ceph_cluster`). `sudo ssh-copy-id` fails — root has
   no key.
7. **`AWS_EC2_METADATA_DISABLED=true` + `AWS_DEFAULT_REGION=us-east-1`** when
   exercising RGW S3 from an EC2 instance — otherwise awscli picks up the
   real region from IMDS and RGW rejects it with
   `IllegalLocationConstraintException`.
8. **`ProxyJump`'s implicit inner ssh ignores `-o` flags** — use an explicit
   `ProxyCommand` to propagate `StrictHostKeyChecking=accept-new` and
   `UserKnownHostsFile=...` to the inner hop.
9. **Stale host keys across rebuilds** — ceph nodes have fixed private IPs and
   the bastion's public IP can be reused, so a prior build's keys make ssh abort
   with "REMOTE HOST IDENTIFICATION HAS CHANGED" (`accept-new` only adds keys
   for *unknown* hosts, never overrides a *changed* one), hanging
   `wait_for_connection`. `aws_infra` scrubs the relevant entries from
   `known_hosts_file` before the first SSH, and `aws_teardown` clears them on
   destroy.

## Layout

```
ansible/
├── ansible.cfg
├── requirements.yml
├── bin/
│   └── ceph9-access            # post-build access script (run from laptop)
├── inventory/
│   ├── hosts.yml               # short-name skeleton (populated at runtime)
│   └── group_vars/all.yml      # all knobs — no secrets
├── playbooks/
│   ├── 00_provision_aws.yml    ── aws_infra
│   ├── 01_prepare_hosts.yml    ── rhel_prep + ssh_trust + cephadm_preflight
│   ├── 02_bootstrap.yml        ── cephadm_bootstrap
│   ├── 03_cluster.yml          ── ceph_cluster (host add, labels, OSDs)
│   ├── 04_services.yml         ── ceph_services (RBD, CephFS, RGW, NFS)
│   ├── 05_smoke.yml            ── smoke_tests
│   ├── 99_destroy.yml          ── aws_teardown
│   └── site.yml                ── 00..05 imported in order
└── roles/{aws_infra,rhel_prep,ssh_trust,cephadm_preflight,
           cephadm_bootstrap,ceph_cluster,ceph_services,smoke_tests,aws_teardown}/
```
