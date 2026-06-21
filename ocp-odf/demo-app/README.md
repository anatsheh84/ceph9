# Ceph External Storage Portal

A Red Hat-themed Flask portal on **ocp-external** that both **consumes** and
**self-service provisions** storage on the external **ceph9** cluster. Two tabs:

### 1. Self-Service Provisioning
- **Log in with Ceph credentials** (mgr/dashboard REST API); connection is verified.
  Ceph **RBAC** is enforced — admins see/do everything, users only what they created.
- **Provision on demand** via the Ceph REST API, choosing a **tier**:
  | Type | Tier0 (SSD) | Tier1 (HDD) | Returns |
  |---|---|---|---|
  | **S3** bucket | pool `rbd`/STANDARD | `HDD` storage class | endpoint + access/secret keys |
  | **NFS** export | nfslab | — | NFS server (NLB) + pseudo-path + mount cmd |
  | **CephFS** subvolume | default data pool | `…data.hdd` | fs + path + mons |
  | **RBD** image | `rbd` | `rbd-hdd` | pool + image + size + map cmd |
- **Live discovery** of everything in Ceph, **retrieve connection details any time**,
  and **browse / upload / preview** S3 objects.

### 2. Consume Mounted Storage
Pre-provisioned **Block** (RWO RBD PVC), **File** (RWX CephFS PVC), **Object** (OBC
bucket) — upload / list / download. Every write lands on ceph9.

### HA / endpoints
The portal targets the **internal NLBs** for the gateway protocols when present
(`deploy.sh` auto-resolves them): S3 → `ceph9-rgw-nlb:8080`, NFS → `ceph9-nfs-nlb:2049`.
The mgr REST API is reached by probing the static node IPs (`CEPH_API_HOSTS`); RBD/CephFS
details use the mon list (`CEPH_MON_HOSTS`).

## Files
```
demo-app/
├── app.py             # Flask app (single file, embedded RH-themed UI)
├── requirements.txt   # Flask, boto3, gunicorn
├── Containerfile      # UBI9 python-311 base; builds the image
└── manifests/         # namespace, PVCs, OBC, ImageStream, BuildConfig, Deployment, Service, Route
    └── kustomization.yaml
```

## Deploy (after pushing this repo to GitHub)

1. **Set your repo URL** in `manifests/05-buildconfig.yaml`
   (`spec.source.git.uri`, and `contextDir`/`ref` if needed).
2. Apply the manifests against the **external** cluster:
   ```bash
   export KUBECONFIG=../clusters/ocp-external/auth/kubeconfig
   oc apply -k manifests/
   ```
3. Kick off (or wait for) the build, then watch it roll out:
   ```bash
   oc -n ceph-demo start-build ceph-demo-app   # if not auto-triggered
   oc -n ceph-demo logs -f bc/ceph-demo-app
   oc -n ceph-demo rollout status deploy/ceph-demo-app
   ```
4. Open the route:
   ```bash
   oc -n ceph-demo get route ceph-demo-app -o jsonpath='https://{.spec.host}{"\n"}'
   ```

> No GitHub yet? You can also build from the local dir without a Git source:
> ```bash
> oc -n ceph-demo new-build --binary --name ceph-demo-app --strategy docker
> oc -n ceph-demo start-build ceph-demo-app --from-dir . --follow
> ```
> (then apply only the namespace, PVCs, OBC, Deployment, Service, Route).

## Notes
- Runs under OpenShift's restricted SCC (random UID) — the UBI base + gunicorn
  handle this; files are written only to the mounted PVCs.
- Object endpoint stays disabled in the UI until the OBC is Bound and its
  ConfigMap/Secret exist.
- Prereqs: Phase 2 (VPC peering) and Phase 3b (ODF external) must be done so the
  external storageclasses + RGW reachability exist.
