# Red Hat Ceph Storage 9 — Lab Deployment Guide on AWS RHEL Instances

**Target:** A containerized RHCS 9 test cluster on AWS EC2 (RHEL) that serves Block (RBD), File (CephFS), Object (S3 via RGW), and NFS, with **500 GB – 1 TB usable** capacity.

**Source of truth:** All requirements, sizing rules, and commands in this guide are taken directly from the Red Hat Ceph Storage 9 Installation Guide, Configuration Guide, and Storage Strategies Guide. References are noted inline.

---

## 1. Architecture & sizing decisions

### 1.1 How many nodes, and why?

Red Hat Ceph Storage 9 is supported **only on container-based deployments**, and the same architecture and deployment type must be used across all nodes (no mixed architectures). Deployment is performed with `cephadm`.

For the use cases you want (RBD + CephFS + RGW S3 + NFS), the RHCS 9 Installation Guide gives the following minimum-cluster sizing reference (Chapter 2, "How Colocation Works"):

> *Example 2 — Use case: Block (RBD), File (CephFS), and Object (Ceph Object Gateway). Number of nodes: 4. Replication scheme: 3.*

The guide is explicit on the replica-3 floor:

> *"The minimum size for a storage cluster with three replicas is four nodes… It is a requirement to have a certain number of nodes for the replication factor with an extra node in the cluster to avoid extended periods with the cluster in a degraded state."*

**Decision:** **4 OSD/service nodes + 1 optional admin/jump host.** The first OSD node also acts as the **bootstrap / `_admin`** host (cephadm applies the `_admin` label to it automatically), so a dedicated admin VM is not strictly required for a lab. I recommend adding it anyway in AWS so the bastion role and the cluster admin keyring live somewhere you can still reach if a Ceph node is rebuilt.

### 1.2 Daemon placement (colocation plan)

Colocation rules from Chapter 2.6 of the Installation Guide:

- One non-OSD daemon from {MON+MGR, NFS-Ganesha, RBD-Mirror, Grafana} **plus** one of {RGW, MDS} may colocate with OSDs on the same host.
- Collocating two of the same kind of daemon on a node is not supported.
- `ceph-mon` and `ceph-mgr` count as one for colocation purposes.
- Red Hat **recommends** colocating RGW with OSDs for performance.

Applied to a 4-node cluster (this matches Example 2/3 in the docs):

| Host    | Labels              | Daemons                                  |
|---------|---------------------|------------------------------------------|
| `ceph01` | `_admin`, `mon`, `mgr`, `osd`, `mds` | OSDs, MON, MGR, MDS (active), Grafana |
| `ceph02` | `mon`, `mgr`, `osd`, `rgw`           | OSDs, MON, MGR, RGW                   |
| `ceph03` | `mon`, `osd`, `rgw`, `nfs`           | OSDs, MON, RGW, NFS-Ganesha           |
| `ceph04` | `osd`, `mds`                         | OSDs, MDS (standby)                   |

This satisfies the 3-MON / 2-MGR / 2-RGW / 1 active + 1 standby MDS / 1 NFS / 4 OSD-host topology that the Installation Guide describes for a multi-protocol cluster.

### 1.3 Capacity math (raw → usable)

With a replicated pool of `size=3`, usable ≈ `raw / 3` minus headroom. Ceph triggers `nearfull` at 85% raw utilization, so plan to fill no further than ~70–75%.

| Target usable | Raw needed (× 3) | With 25% headroom | Recommended layout                                  |
|---------------|------------------|-------------------|------------------------------------------------------|
| **500 GB**    | 1.5 TB           | ~2.0 TB           | 4 nodes × **1 × 500 GB** gp3 EBS = **2 TB raw**, **4 OSDs** total |
| **1 TB**      | 3.0 TB           | ~4.0 TB           | 4 nodes × **2 × 500 GB** gp3 EBS = **4 TB raw**, **8 OSDs** total |

The OSD data drive must be **larger than 5 GB**, must have **no partitions, filesystem, mount, or prior BlueStore signature**, and **cannot be the OS disk** — these are hard requirements from §3.20 of the Installation Guide.

### 1.4 VM size selection (per-node resource math)

Per-daemon **minimum** container requirements (Installation Guide §2.8):

| Daemon       | vCPU | RAM   | Local disk             |
|--------------|------|-------|-------------------------|
| `ceph-osd`   | 1    | 5 GB  | 1 dedicated drive       |
| `ceph-mon`   | 1    | 3 GB  | 10 GB (50 GB recommended) |
| `ceph-mgr`   | 1    | 3 GB  | —                       |
| `ceph-mds`   | 1    | 3 GB  | 2 GB (+ 20 GiB CephFS metadata pool) |
| `ceph-rgw`   | 1    | 1 GB  | 5 GB                    |

Worst-case node in our layout = `ceph01` (2× OSD + MON/MGR + MDS + Grafana):
- CPU: ~5 vCPU for daemons + OS/podman overhead → **8 vCPU**
- RAM: 10 (OSDs) + 3 (mon) + 3 (mgr) + 3 (mds) + ~4 (OS) ≈ **23 GB → 32 GB**

**Recommended AWS instance:** `m5.2xlarge` (8 vCPU, 32 GiB RAM, up to 10 Gbps network) for all 4 OSD nodes. `m5.xlarge` (4 vCPU / 16 GiB) works for a very light lab but leaves no headroom for memory autotuning. The admin/jump host can be `t3.medium`.

The Installation Guide is explicit that **a 1 Gb/s Ethernet network is not suitable for production storage clusters**, and recommends 10 Gb/s minimum — so do not undersize the instance bandwidth.

### 1.5 Bill of materials (final)

| # | Role                  | Instance     | OS disk         | OSD disks (gp3)   | Notes |
|---|-----------------------|--------------|------------------|--------------------|-------|
| 1 | `ceph01` (bootstrap/_admin/mon/mgr/osd/mds) | m5.2xlarge | 60 GB gp3 | 2 × 500 GB | Initial Monitor host |
| 2 | `ceph02` (mon/mgr/osd/rgw)                  | m5.2xlarge | 60 GB gp3 | 2 × 500 GB |       |
| 3 | `ceph03` (mon/osd/rgw/nfs)                  | m5.2xlarge | 60 GB gp3 | 2 × 500 GB |       |
| 4 | `ceph04` (osd/mds)                          | m5.2xlarge | 60 GB gp3 | 2 × 500 GB |       |
| 5 | `ceph-admin` (optional jump host)           | t3.medium  | 30 GB gp3 | —          | Ansible admin node |

For 500 GB usable, change OSD disks to **1 × 500 GB per node** (4 OSDs total) and you can drop the instances to `m5.xlarge`.

> Disk-space requirement note from §2.8: daemon working data lives under `/var/lib/ceph/`. The bootstrap process also needs **at least 10 GB free under `/var/lib/containers/`**. The 60 GB OS disk above covers both.

---

## 2. AWS infrastructure prerequisites

### 2.1 Networking

- **VPC + single subnet** is sufficient for a lab. The Installation Guide recommends two networks (public for client/MON traffic, private cluster network for OSD heartbeat/replication/recovery), and notes this gives "significant performance improvement". For AWS, the simplest approach is **one subnet** and let Ceph auto-discover; document the public/cluster CIDRs at bootstrap time if you split them later.
- All Ceph traffic stays inside the VPC. Do not put OSDs on public IPs.
- **MTU:** the Installation Guide recommends jumbo frames (MTU 9000) on the cluster network; AWS supports 9001 inside a placement group. For a lab the default 1500 MTU is fine.

### 2.2 Security group (minimum open ports)

Per the Configuration Guide §2.6 ("Verifying firewall rules"), default Ceph daemons use **TCP 6800–7100**, plus the MON ports.

Open these **inbound from the security group itself** (i.e. node-to-node):

| Port range | Protocol | Purpose                           |
|------------|----------|-----------------------------------|
| 22         | TCP      | SSH between admin and nodes       |
| 3300       | TCP      | MON msgr2                         |
| 6789       | TCP      | MON msgr v1 (legacy)              |
| 6800–7300  | TCP      | OSD/MGR/MDS/RGW daemon traffic    |
| 8443       | TCP      | Ceph Dashboard (HTTPS)            |
| 9095       | TCP      | Prometheus                        |
| 3000       | TCP      | Grafana                           |
| 9100       | TCP      | node-exporter                     |
| 9093       | TCP      | Alertmanager                      |
| 80 / 443   | TCP      | RGW S3 endpoint (from clients)    |
| 2049       | TCP      | NFS-Ganesha                       |

Open the Dashboard / Grafana / S3 / NFS ports inbound from your client IP range, not the world.

### 2.3 IAM & misc

- Each EC2 instance needs **outbound** internet (or VPC endpoint) to reach `registry.redhat.io`, `cdn.redhat.com`, and the RHEL `subscription-manager` endpoints to install packages and pull container images.
- Use a single keypair for `ec2-user` and configure passwordless SSH (see §3.3 below) — cephadm requires it.

---

## 3. Per-node preparation (run on every Ceph node)

### 3.1 RHEL version and registration

The Installation Guide §3.4 specifies **RHEL 9.6, 9.7, 10, or 10.1** with `ansible-core` bundled into AppStream. Use the official Red Hat AMI for RHEL 9 in the AWS Marketplace.

On each node, register and enable the required repos (§3.4):

```bash
sudo subscription-manager register      # enter Red Hat Customer Portal credentials
sudo subscription-manager refresh
sudo subscription-manager repos --disable=*
sudo subscription-manager repos --enable=rhel-9-for-x86_64-baseos-rpms
sudo subscription-manager repos --enable=rhel-9-for-x86_64-appstream-rpms
sudo subscription-manager repos --enable=rhceph-9-tools-for-rhel-9-x86_64-rpms
sudo dnf update -y
```

> Red Hat now uses Simple Content Access; you no longer need to attach a subscription explicitly.

### 3.2 Install cephadm-ansible on the bootstrap (or admin) node only

```bash
sudo dnf install -y cephadm-ansible
```

The package installs the preflight, clients, and purge-cluster playbooks into `/usr/share/cephadm-ansible/`.

### 3.3 Passwordless SSH and hostname resolution

cephadm uses SSH to manage all nodes from the bootstrap host. From `ceph01`:

```bash
# Generate (if not already) and distribute the key to every node
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
for h in ceph02 ceph03 ceph04; do
  ssh-copy-id -i ~/.ssh/id_ed25519.pub ec2-user@$h
done
```

Ensure each node can resolve every other node by short name (`ceph01`, `ceph02`, …). Either add entries to `/etc/hosts` on all four nodes or use Route 53 private zones. The Configuration Guide warns: *"The host option is the short name of the node, not its FQDN."*

> Note: RHEL 9/10 disable root SSH login by default. Either follow §3.6 of the Installation Guide to enable it, or follow §3.10.4 to **bootstrap as a non-root user with passwordless sudo** (this is the cleaner approach on AWS — `ec2-user` already has passwordless sudo on the Red Hat AMI).

### 3.4 Run the preflight playbook

The preflight playbook installs `podman`, `lvm2`, `chrony`, and `cephadm`, and checks OS/CPU/RAM/swap/NIC configuration (§3.9).

```bash
cd /usr/share/cephadm-ansible
cat > hosts <<EOF
ceph02
ceph03
ceph04
[admin]
ceph01
EOF

ansible-playbook -i hosts cephadm-preflight.yml --extra-vars "ceph_origin=rhcs"
```

A `preflight_report.txt` is written to the current directory. All checks are informational and do not block deployment, but resolve any **Failed** entries before proceeding.

---

## 4. Bootstrap the cluster (Day 1)

### 4.1 Create a registry-login JSON to protect credentials

Per §3.10.2, store registry credentials in a JSON file so they don't appear in shell history:

```bash
sudo mkdir -p /etc/ceph
sudo tee /etc/mylogin.json > /dev/null <<EOF
{
  "url": "registry.redhat.io",
  "username": "REDHAT_USERNAME",
  "password": "REDHAT_PASSWORD"
}
EOF
sudo chmod 600 /etc/mylogin.json
```

### 4.2 Bootstrap from `ceph01`

Use the recommended bootstrap options (§3.10.1) — bootstrap as the non-root `ec2-user` with FQDN support:

```bash
sudo cephadm bootstrap \
  --ssh-user ec2-user \
  --mon-ip <PRIVATE_IP_OF_CEPH01> \
  --cluster-network <VPC_CIDR>/24 \
  --allow-fqdn-hostname \
  --registry-json /etc/mylogin.json
```

What cephadm does during bootstrap (§3.10):

- Installs and starts a `ceph-mon` + `ceph-mgr` on `ceph01` as containers.
- Creates `/etc/ceph/` and writes `ceph.conf`, `ceph.client.admin.keyring`, and `ceph.pub`.
- Applies the `_admin` label to `ceph01` and pushes the admin keyring there.
- Deploys the monitoring stack: Prometheus, Grafana, node-exporter, Alertmanager.
- Prints the Dashboard URL (`https://ceph01:8443/`) and a temporary admin password. **You will be required to change the password on first login.**

If the cluster has multiple networks/interfaces, double-check that the `--mon-ip` is on a subnet reachable by every other node. The Installation Guide reiterates this in §3.10.

### 4.3 Open the cephadm shell

All subsequent `ceph` commands run inside the cephadm shell container:

```bash
sudo /usr/sbin/cephadm shell
# you are now in the [ceph: root@ceph01 /]# prompt
ceph -s
```

A freshly bootstrapped cluster reports `HEALTH_WARN` until more hosts/OSDs are added (this is expected per §3.13).

---

## 5. Add the remaining hosts

### 5.1 Distribute the cluster SSH key and run preflight on new hosts

From the admin node:

```bash
ssh-copy-id -f -i /etc/ceph/ceph.pub ec2-user@ceph02
ssh-copy-id -f -i /etc/ceph/ceph.pub ec2-user@ceph03
ssh-copy-id -f -i /etc/ceph/ceph.pub ec2-user@ceph04

cd /usr/share/cephadm-ansible
ansible-playbook -i hosts cephadm-preflight.yml \
  --extra-vars "ceph_origin=rhcs" --limit ceph02,ceph03,ceph04
```

### 5.2 Add hosts from the cephadm shell on `ceph01`

```bash
ceph orch host add ceph02 <PRIVATE_IP_OF_CEPH02>
ceph orch host add ceph03 <PRIVATE_IP_OF_CEPH03>
ceph orch host add ceph04 <PRIVATE_IP_OF_CEPH04>
ceph orch host ls
```

Each `host add` is recorded in §3.14 of the Installation Guide. Hosts on the same subnet as the bootstrap node get a MON daemon scheduled automatically up to 5 nodes; you can override with labels (next step).

### 5.3 Apply labels

```bash
# MONs (3 nodes)
ceph orch host label add ceph01 mon
ceph orch host label add ceph02 mon
ceph orch host label add ceph03 mon
# MGRs (2 nodes)
ceph orch host label add ceph01 mgr
ceph orch host label add ceph02 mgr
# OSDs (all 4)
for h in ceph01 ceph02 ceph03 ceph04; do ceph orch host label add $h osd; done
# Gateways
ceph orch host label add ceph02 rgw
ceph orch host label add ceph03 rgw
ceph orch host label add ceph03 nfs
ceph orch host label add ceph01 mds
ceph orch host label add ceph04 mds
```

### 5.4 Pin the daemons to the labeled hosts

```bash
ceph orch apply mon  --placement="label:mon"
ceph orch apply mgr  --placement="label:mgr"
```

---

## 6. Deploy OSDs

Per §3.20, cephadm only provisions an OSD on a device that has **no partitions, no mount, no filesystem, no prior BlueStore signature, and is larger than 5 GB**.

```bash
# Confirm the EBS volumes are visible and "available"
ceph orch device ls --wide --refresh
```

Two options:

```bash
# Option A — simplest: consume every eligible disk on every host
ceph orch apply osd --all-available-devices

# Option B — per host, per device (more deterministic for lab work)
ceph orch daemon add osd ceph01:/dev/nvme1n1
ceph orch daemon add osd ceph01:/dev/nvme2n1
ceph orch daemon add osd ceph02:/dev/nvme1n1
# ... and so on for every OSD disk
```

> The device names on AWS Nitro instances appear as `/dev/nvme1n1`, `/dev/nvme2n1`, etc. — not `/dev/sdb`. Confirm with `lsblk` before issuing commands.

Verify:

```bash
ceph orch ps --daemon_type=osd
ceph osd tree
ceph -s     # should now show "X osds: X up, X in"
```

---

## 7. Endpoint #1 — Block storage (RBD)

RBD doesn't need a separate daemon; it consumes a replicated RADOS pool.

```bash
ceph osd pool create rbd 32 32 replicated
ceph osd pool application enable rbd rbd
rbd pool init rbd

# Create a 50 GB test image
rbd create rbd/test-vol --size 50G
rbd info rbd/test-vol
```

From any client with `ceph-common` installed and `/etc/ceph/ceph.conf` + `ceph.client.admin.keyring` copied over:

```bash
sudo rbd map rbd/test-vol
sudo mkfs.xfs /dev/rbd0
sudo mount /dev/rbd0 /mnt/test-vol
```

---

## 8. Endpoint #2 — File storage (CephFS)

§3.10.3 notes: *"To deploy the MDS service, you must create a CephFS volume first."* The `ceph fs volume create` shortcut creates the data + metadata pools **and** schedules the MDS for you.

```bash
ceph fs volume create cephfs --placement="label:mds"
ceph fs ls
ceph orch ps --daemon_type=mds
```

The MDS daemons land on `ceph01` (active) and `ceph04` (standby) because those are the hosts labeled `mds`. Recall from §2.8 that each MDS needs **3 GB RAM minimum and 20 GiB of CephFS metadata pool space**.

Client mount (from any RHEL host with `ceph-common`):

```bash
sudo mkdir -p /mnt/cephfs
sudo mount -t ceph ceph01,ceph02,ceph03:/ /mnt/cephfs \
  -o name=admin,secretfile=/etc/ceph/admin.secret
```

---

## 9. Endpoint #3 — Object storage (RGW / S3)

Deploy two RGW daemons, one each on `ceph02` and `ceph03`:

```bash
ceph orch apply rgw s3lab \
  --placement="label:rgw count-per-host:1" \
  --port=8080
```

Create an S3 user and grab credentials:

```bash
radosgw-admin user create \
  --uid=labuser \
  --display-name="Lab User" \
  --email=lab@example.com
# Note the access_key and secret_key in the JSON output
```

Test with `awscli`:

```bash
aws --endpoint-url http://<CEPH02_PRIVATE_IP>:8080 s3 mb s3://lab-bucket
aws --endpoint-url http://<CEPH02_PRIVATE_IP>:8080 s3 cp /etc/hosts s3://lab-bucket/
```

> For HTTPS, deploy RGW behind an AWS NLB or terminate TLS on the RGW itself by adding `--ssl-certificate` and `--ssl-private-key` to the `ceph orch apply rgw` spec.

---

## 10. Endpoint #4 — NFS

Cephadm-managed NFS-Ganesha can re-export either **CephFS** or **RGW buckets** (NFS for RGW is one of the example use cases in §2.6).

### 10.1 Deploy the NFS cluster

```bash
ceph nfs cluster create nfslab "label:nfs"
ceph orch ps --daemon_type=nfs
```

### 10.2 Export the CephFS volume

```bash
ceph nfs export create cephfs nfslab /cephfs cephfs --path=/
ceph nfs export ls nfslab
```

Mount from a client:

```bash
sudo mount -t nfs4 <CEPH03_PRIVATE_IP>:/cephfs /mnt/nfs
```

### 10.3 (Optional) Export an RGW bucket over NFS

```bash
ceph nfs export create rgw --cluster-id=nfslab --pseudo-path=/lab-bucket --bucket=lab-bucket
```

---

## 11. Verify the cluster is healthy

```bash
ceph -s
```

You want to see something like the example output from §3.13:

```
  cluster:
    id:     <uuid>
    health: HEALTH_OK
  services:
    mon: 3 daemons, quorum ceph01,ceph02,ceph03
    mgr: ceph01.xxx(active), standbys: ceph02.xxx
    mds: 1/1 daemons up, 1 standby
    osd: 8 osds: 8 up, 8 in
    rgw: 2 daemons active (2 hosts, 1 zones)
  data:
    volumes: 1/1 healthy
    pools:   <N> pools, <P> pgs
    pgs:     <all> active+clean
```

Additional sanity checks:

```bash
ceph osd tree           # OSDs distributed across all 4 hosts
ceph osd df             # capacity per OSD; nearfull alerts surface here
ceph orch host ls       # all hosts Online, labels look right
ceph orch ps            # every daemon shows running
ceph health detail      # explains any HEALTH_WARN
ceph fs status cephfs   # MDS state, MDS cache memory usage
radosgw-admin zone get  # RGW zone is set up
```

The Dashboard at `https://ceph01:8443/` gives a UI for all of the above.

---

## 12. Day-2 quick reference

| Task                      | Command                                              |
|---------------------------|------------------------------------------------------|
| Add a host                | `ceph orch host add <host> <ip>`                     |
| Drain & remove a host     | `ceph orch host drain <host>; ceph orch host rm <host>` |
| Add an OSD                | `ceph orch daemon add osd <host>:<device>`           |
| Remove an OSD             | `ceph orch osd rm <osd-id>` (then `osd rm status`)   |
| Scale MONs                | `ceph orch apply mon --placement="label:mon"`        |
| Scale MGRs                | `ceph orch apply mgr <N>`                            |
| Scale RGWs                | `ceph orch apply rgw s3lab --placement="label:rgw"`  |
| Scale MDSs                | `ceph orch apply mds cephfs --placement="label:mds count:2"` |
| Restart a daemon          | `ceph orch daemon restart <name>`                    |
| Upgrade the cluster       | `ceph orch upgrade start --ceph-version <new>`       |
| Set replica count         | `ceph osd pool set <pool> size 3`                    |
| Set min replicas for I/O  | `ceph osd pool set <pool> min_size 2`                |
| Purge the cluster         | `ansible-playbook cephadm-purge-cluster.yml`         |

---

## 13. AWS-specific reminders & gotchas

1. **EBS volumes for OSDs must be attached but unformatted.** Don't create an XFS/ext4 filesystem on them — cephadm rejects devices with existing filesystems (§3.20). Just create the EBS volume in the AWS console and attach it; that's it.
2. **NVMe device renaming.** AWS Nitro instances expose EBS as `/dev/nvme*n1`. The mapping between the AWS device hint (`/dev/sdb`) and the OS path can shift after reboots — always confirm with `lsblk` before running `ceph orch daemon add osd …`.
3. **Instance retirement.** When AWS retires a Nitro instance, EBS volumes survive but instance store does not. Always put OSD data on **EBS gp3**, never on instance store.
4. **No swap on OSD nodes (or near-zero `vm.swappiness`).** The preflight playbook does an informational swap check (§3.9) but doesn't enforce. RHEL on AWS does not enable swap by default — that's the correct posture.
5. **Use Placement Groups (AWS, not Ceph PGs) for OSD nodes.** A *cluster* AWS placement group gets the 4 OSD VMs on the same low-latency rack and unlocks 10 Gbps node-to-node — needed to meet the doc's network recommendation.
6. **Snapshots ≠ backups.** EBS snapshots of OSD volumes are crash-consistent at the block layer; they are **not** a substitute for Ceph-native RBD snapshots or RGW versioning.
7. **Time sync is mandatory.** The preflight playbook installs and enables `chrony`. AWS provides the `169.254.169.123` NTP source automatically; do not disable it.

---

## 14. References (all from the project knowledge base)

- *Red Hat Ceph Storage 9 Installation Guide* — Chapter 2 (Considerations & Recommendations), Chapter 3 (Installation), Appendix A (Ceph-Ansible vs Cephadm), Appendix B (cephadm commands).
- *Red Hat Ceph Storage 9 Configuration Guide* — Chapter 2 (Network configuration), Chapter 3 (Monitor configuration), Chapter 6/7 (OSD configuration & MON/OSD interaction).
- *Red Hat Ceph Storage 9 Storage Strategies Guide* — Chapter 3 (Placement Groups), Chapter 4 (PG autoscaler), Chapter 5 (Pools), Chapter 6 (Erasure code optimizations).

For Day-2 RGW topics (multi-site, zones, realms), the Installation Guide points readers to the *Red Hat Ceph Storage 9 Object Gateway Guide*, which is not included in this project knowledge base.
