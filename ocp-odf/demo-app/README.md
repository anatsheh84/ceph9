# Ceph External Storage Demo app

A Red Hat-themed Flask web app that demonstrates the **ocp-external** cluster
storing and retrieving files from the external **ceph9** cluster across all
three storage endpoints:

| Endpoint | Backing | How |
|---|---|---|
| **Block** | Ceph RBD | RWO PVC `ceph-demo-block` (`ocs-external-storagecluster-ceph-rbd`) mounted at `/data/block` |
| **File** | CephFS | RWX PVC `ceph-demo-file` (`ocs-external-storagecluster-cephfs`) mounted at `/data/file` |
| **Object** | Ceph RGW (S3) | `ObjectBucketClaim` `ceph-demo-bucket` (`ocs-external-storagecluster-ceph-rgw`); creds via `envFrom` |

The UI lets you pick an endpoint, upload a file, and download files back — every
write lands on ceph9.

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
