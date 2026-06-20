#!/usr/bin/env python3
"""
Ceph External Storage Demo + Self-Service Portal
================================================
Demonstrates an OpenShift cluster (ocp-external) using an EXTERNAL Ceph cluster
(ceph9) two ways:

CONSUME (pre-provisioned storage, mounted/claimed by OpenShift):
  * Block  (RBD)    -> RWO PVC mounted at $BLOCK_DIR
  * File   (CephFS) -> RWX PVC mounted at $FILE_DIR
  * Object (RGW S3) -> bucket from an ObjectBucketClaim (creds via envFrom)

PROVISION (self-service, on demand, straight to the Ceph REST API):
  * S3 bucket     -> POST /api/rgw/user + /api/rgw/bucket  -> returns endpoint + keys
  * NFS export    -> POST /api/nfs-ganesha/export          -> returns server + pseudo
  * CephFS subvol -> POST /api/cephfs/subvolume            -> returns path + mons

The Ceph mgr/dashboard REST API endpoint is auto-discovered by probing the
(static) node IPs, so this keeps working after a lab rebuild. Per-endpoint API
versions are auto-negotiated (Ceph returns the required version on a 415).
"""
import io
import os
import re

import boto3
import requests
import urllib3
from botocore.client import Config
from flask import (Flask, abort, render_template_string, request, send_file,
                   url_for)
from werkzeug.utils import secure_filename

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ceph-external-demo")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024

# ───────────────────────────── CONSUME config ────────────────────────────
BLOCK_DIR = os.environ.get("BLOCK_DIR", "/data/block")
FILE_DIR = os.environ.get("FILE_DIR", "/data/file")
for _d in (BLOCK_DIR, FILE_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass

BUCKET_NAME = os.environ.get("BUCKET_NAME")
BUCKET_HOST = os.environ.get("BUCKET_HOST")
BUCKET_PORT = os.environ.get("BUCKET_PORT", "80")
S3_KEY = os.environ.get("AWS_ACCESS_KEY_ID")
S3_SECRET = os.environ.get("AWS_SECRET_ACCESS_KEY")

ENDPOINTS = {
    "block": {"label": "Block", "tech": "RBD", "kind": "fs", "dir": BLOCK_DIR,
              "blurb": "ReadWriteOnce PVC on Ceph RBD"},
    "file": {"label": "File", "tech": "CephFS", "kind": "fs", "dir": FILE_DIR,
             "blurb": "ReadWriteMany PVC on CephFS"},
    "object": {"label": "Object", "tech": "RGW S3", "kind": "s3",
               "blurb": "S3 bucket on Ceph RGW"},
}

# ───────────────────────────── PROVISION config ──────────────────────────
CEPH_API_HOSTS = [h.strip() for h in os.environ.get(
    "CEPH_API_HOSTS", "10.20.2.11,10.20.2.12,10.20.2.13,10.20.2.14").split(",") if h.strip()]
CEPH_API_PORT = os.environ.get("CEPH_API_PORT", "8443")
CEPH_API_USER = os.environ.get("CEPH_API_USER", "admin")
CEPH_API_PASSWORD = os.environ.get("CEPH_API_PASSWORD", "")
CEPH_RGW_ENDPOINT = os.environ.get("CEPH_RGW_ENDPOINT", f"{BUCKET_HOST}:{BUCKET_PORT}")
CEPH_FS_NAME = os.environ.get("CEPH_FS_NAME", "cephfs")
CEPH_NFS_CLUSTER = os.environ.get("CEPH_NFS_CLUSTER", "nfslab")
CEPH_NFS_SERVER = os.environ.get("CEPH_NFS_SERVER", "")            # ip:port
CEPH_MON_HOSTS = os.environ.get("CEPH_MON_HOSTS", "10.20.2.11,10.20.2.12,10.20.2.13")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")


class CephAPIError(Exception):
    pass


class CephAPI:
    """Minimal client for the Ceph mgr dashboard REST API with mgr discovery
    and automatic per-endpoint API-version negotiation."""

    def __init__(self):
        self.base = None
        self.token = None

    def _login(self):
        if self.token:
            return
        if not CEPH_API_PASSWORD:
            raise CephAPIError("CEPH_API_PASSWORD not set")
        last = "no hosts"
        for h in CEPH_API_HOSTS:
            base = f"https://{h}:{CEPH_API_PORT}/api"
            try:
                r = requests.post(
                    base + "/auth", verify=False, timeout=8,
                    headers={"Accept": "application/vnd.ceph.api.v1.0+json",
                             "Content-Type": "application/json"},
                    json={"username": CEPH_API_USER, "password": CEPH_API_PASSWORD})
                if r.status_code in (200, 201):
                    self.base, self.token = base, r.json()["token"]
                    return
                last = f"{h}: HTTP {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                last = f"{h}: {exc}"
        raise CephAPIError(f"no Ceph mgr reachable ({last})")

    @property
    def active_mgr(self):
        return self.base.split("//", 1)[1].split(":", 1)[0] if self.base else None

    def req(self, method, path, body=None, version="1.0"):
        self._login()
        ver = version
        r = None
        for _ in range(3):
            r = requests.request(
                method, self.base + path, verify=False, timeout=30,
                headers={"Accept": f"application/vnd.ceph.api.v{ver}+json",
                         "Content-Type": "application/json",
                         "Authorization": f"Bearer {self.token}"},
                json=body)
            if r.status_code == 415 and "Incorrect version" in r.text:
                m = re.search(r"endpoint is '([\d.]+)'", r.text)
                if m and m.group(1) != ver:
                    ver = m.group(1)
                    continue
            return r
        return r

    @staticmethod
    def _ok(r, *codes):
        if r.status_code not in (codes or (200, 201)):
            detail = r.text[:200]
            try:
                detail = r.json().get("detail", detail)
            except Exception:  # noqa: BLE001
                pass
            raise CephAPIError(f"HTTP {r.status_code}: {detail}")

    # ── provisioning ──────────────────────────────────────────────────────
    def provision_s3(self, name):
        u = self.req("POST", "/rgw/user", version="1.0", body={
            "uid": name, "display_name": name, "email": "",
            "max_buckets": 1000, "suspended": 0})
        self._ok(u, 200, 201)
        keys = (u.json().get("keys") or [{}])[0]
        b = self.req("POST", "/rgw/bucket", version="1.0",
                     body={"bucket": name, "uid": name})
        self._ok(b, 200, 201)
        return {"_type": "s3", "name": name,
                "Endpoint": f"http://{CEPH_RGW_ENDPOINT}",
                "Bucket": name,
                "Access key": keys.get("access_key", ""),
                "Secret key": keys.get("secret_key", "")}

    def provision_nfs(self, name):
        pseudo = "/" + name
        e = self.req("POST", "/nfs-ganesha/export", version="2.0", body={
            "cluster_id": CEPH_NFS_CLUSTER, "path": "/", "pseudo": pseudo,
            "access_type": "RW", "squash": "no_root_squash",
            "security_label": False, "protocols": [4], "transports": ["TCP"],
            "fsal": {"name": "CEPH", "fs_name": CEPH_FS_NAME}, "clients": []})
        self._ok(e, 200, 201)
        server = CEPH_NFS_SERVER or "<nfs-server>:2049"
        host = server.split(":")[0]
        return {"_type": "nfs", "name": name,
                "NFS server": server, "Export (pseudo)": pseudo,
                "Cluster": CEPH_NFS_CLUSTER,
                "Mount command": f"sudo mount -t nfs4 {host}:{pseudo} /mnt/{name}"}

    def provision_cephfs(self, name):
        c = self.req("POST", "/cephfs/subvolume", version="1.0", body={
            "vol_name": CEPH_FS_NAME, "subvol_name": name, "size": 1073741824})
        self._ok(c, 200, 201)
        info = self.req("GET",
                        f"/cephfs/subvolume/{CEPH_FS_NAME}/info?subvol_name={name}",
                        version="1.0")
        path = ""
        if info.status_code == 200:
            path = info.json().get("path", "")
        mons = ",".join(f"{m}:6789" for m in CEPH_MON_HOSTS.split(","))
        return {"_type": "cephfs", "name": name,
                "Filesystem": CEPH_FS_NAME, "Subvolume": name,
                "Path": path or "(query failed)",
                "Monitors": mons,
                "Note": "Mount in-cluster via the cephfs StorageClass, or export "
                        "this subvolume over NFS for external access."}


ceph = CephAPI()


# ───────────────────────────── consume helpers ───────────────────────────
def object_ready():
    return all([BUCKET_NAME, BUCKET_HOST, S3_KEY, S3_SECRET])


def s3_client():
    return boto3.client(
        "s3", endpoint_url=f"http://{BUCKET_HOST}:{BUCKET_PORT}",
        aws_access_key_id=S3_KEY, aws_secret_access_key=S3_SECRET,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1")


def human(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"


def list_files(ep):
    info = ENDPOINTS[ep]
    out = []
    try:
        if info["kind"] == "fs":
            for name in sorted(os.listdir(info["dir"])):
                p = os.path.join(info["dir"], name)
                if os.path.isfile(p):
                    out.append({"name": name, "size": human(os.path.getsize(p))})
        elif info["kind"] == "s3" and object_ready():
            resp = s3_client().list_objects_v2(Bucket=BUCKET_NAME)
            for o in sorted(resp.get("Contents", []), key=lambda x: x["Key"]):
                out.append({"name": o["Key"], "size": human(o["Size"])})
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "items": []}
    return {"error": None, "items": out}


def render_index(**extra):
    listings = {ep: list_files(ep) for ep in ENDPOINTS}
    return render_template_string(
        PAGE, endpoints=ENDPOINTS, listings=listings,
        object_ready=object_ready(), bucket=BUCKET_NAME or "(not provisioned)",
        node=os.environ.get("NODE_NAME", ""), **extra)


# ───────────────────────────── routes ────────────────────────────────────
@app.route("/")
def index():
    return render_index()


@app.route("/upload", methods=["POST"])
def upload():
    ep = request.form.get("endpoint", "")
    if ep not in ENDPOINTS:
        return render_index(msg=("error", "Unknown storage endpoint."))
    f = request.files.get("file")
    if not f or f.filename == "":
        return render_index(msg=("error", "Please choose a file to upload."))
    name = secure_filename(f.filename)
    info = ENDPOINTS[ep]
    try:
        if info["kind"] == "fs":
            f.save(os.path.join(info["dir"], name))
        else:
            if not object_ready():
                raise RuntimeError("Object endpoint not configured (OBC not bound).")
            s3_client().upload_fileobj(f, BUCKET_NAME, name)
        return render_index(msg=("ok", f"Uploaded '{name}' to {info['label']} ({info['tech']}) on ceph9."))
    except Exception as exc:  # noqa: BLE001
        return render_index(msg=("error", f"Upload to {info['label']} failed: {exc}"))


@app.route("/download/<ep>/<path:name>")
def download(ep, name):
    if ep not in ENDPOINTS:
        abort(404)
    info = ENDPOINTS[ep]
    name = secure_filename(name)
    try:
        if info["kind"] == "fs":
            p = os.path.join(info["dir"], name)
            if not os.path.isfile(p):
                abort(404)
            return send_file(p, as_attachment=True, download_name=name)
        if not object_ready():
            abort(503)
        obj = s3_client().get_object(Bucket=BUCKET_NAME, Key=name)
        return send_file(io.BytesIO(obj["Body"].read()), as_attachment=True,
                         download_name=name)
    except Exception:  # noqa: BLE001
        abort(404)


@app.route("/provision", methods=["POST"])
def provision():
    ptype = request.form.get("ptype", "")
    name = (request.form.get("name", "") or "").strip().lower()
    if ptype not in ("s3", "nfs", "cephfs"):
        return render_index(msg=("error", "Unknown provision type."))
    if not NAME_RE.match(name):
        return render_index(msg=("error",
                                 "Name must be 3-41 chars: lowercase letters, digits, dashes."))
    try:
        result = {"s3": ceph.provision_s3, "nfs": ceph.provision_nfs,
                  "cephfs": ceph.provision_cephfs}[ptype](name)
        return render_index(prov_result=result,
                            msg=("ok", f"Provisioned {ptype.upper()} '{name}' on ceph9."))
    except Exception as exc:  # noqa: BLE001
        return render_index(msg=("error", f"Provision failed: {exc}"))


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ceph External Storage Portal</title>
<style>
  :root{--rh-red:#ee0000;--rh-red-dark:#be0000;--bg:#0f0f0f;--panel:#1b1b1b;
        --panel-2:#242424;--line:#3c3c3c;--text:#fff;--muted:#a0a0a0;--green:#3fb950;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:"Red Hat Display","Red Hat Text",-apple-system,Segoe UI,Roboto,Arial,sans-serif;}
  header{background:#000;border-bottom:4px solid var(--rh-red);padding:18px 28px;display:flex;align-items:center;gap:16px}
  .logo{width:46px;height:32px;background:var(--rh-red);border-radius:3px;display:flex;align-items:center;
        justify-content:center;font-weight:800;font-size:13px;letter-spacing:.5px}
  header h1{font-size:20px;margin:0}
  header .sub{color:var(--muted);font-size:13px;margin-top:2px}
  .wrap{max-width:1100px;margin:0 auto;padding:28px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:22px;margin-bottom:24px}
  .card h2{margin:0 0 4px;font-size:16px}
  .card .hint{color:var(--muted);font-size:12px;margin-bottom:16px}
  form.row{display:flex;flex-wrap:wrap;gap:14px;align-items:end}
  .field{display:flex;flex-direction:column;gap:6px}
  label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  select,input[type=file],input[type=text]{background:var(--panel-2);color:var(--text);border:1px solid var(--line);
        border-radius:6px;padding:10px 12px;font-size:14px;min-width:220px}
  input[type=file]::file-selector-button{background:#333;color:#fff;border:0;border-radius:5px;padding:7px 12px;margin-right:10px;cursor:pointer}
  button{background:var(--rh-red);color:#fff;border:0;border-radius:6px;padding:11px 26px;font-size:14px;font-weight:700;cursor:pointer}
  button:hover{background:var(--rh-red-dark)}
  button.alt{background:#2b2b2b;border:1px solid var(--line)}
  .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
  .ep{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .ep .head{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
  .ep .head .t{font-weight:700}.ep .head .tag{font-size:11px;background:var(--rh-red);padding:2px 8px;border-radius:10px}
  .ep .blurb{color:var(--muted);font-size:12px;padding:10px 18px 0}
  .ep ul{list-style:none;margin:8px 0 0;padding:8px 10px;max-height:280px;overflow:auto}
  .ep li{display:flex;justify-content:space-between;gap:10px;padding:9px 10px;border-radius:6px}
  .ep li:hover{background:var(--panel-2)}.ep li .nm{font-size:13px;word-break:break-all}
  .ep li .sz{color:var(--muted);font-size:11px;white-space:nowrap}
  .ep a.dl{color:#ff5a5a;text-decoration:none;font-size:12px;font-weight:700}
  .empty{color:var(--muted);font-size:13px;padding:14px 18px}.err{color:#ff8080;font-size:12px;padding:10px 18px}
  .flash{padding:12px 16px;border-radius:6px;margin-bottom:18px;font-size:14px}
  .flash.ok{background:#0d2c12;border:1px solid #1f7a33;color:#9be8ab}
  .flash.error{background:#2c0d0d;border:1px solid #7a1f1f;color:#ffb0b0}
  .result{background:#10240f;border:1px solid #2f7a33;border-radius:8px;padding:18px;margin-bottom:24px}
  .result h3{margin:0 0 12px;font-size:15px;color:#9be8ab}
  .kv{display:grid;grid-template-columns:170px 1fr;gap:8px 14px;font-size:13px}
  .kv .k{color:var(--muted)}.kv .v{font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all;
        background:#000;padding:3px 8px;border-radius:4px}
  .sec{display:flex;align-items:center;gap:10px;margin:26px 0 12px}
  .sec h2{font-size:18px;margin:0}.sec .ln{flex:1;height:1px;background:var(--line)}
  footer{color:var(--muted);font-size:12px;text-align:center;padding:20px}
  code{background:#000;padding:1px 6px;border-radius:4px;color:#ff8080}
  .pill{font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px}
  .pill.on{background:#1f7a33}.pill.off{background:#555}
</style>
</head>
<body>
<header>
  <div class="logo">RH</div>
  <div><h1>Ceph External Storage Portal</h1>
  <div class="sub">OpenShift <code>ocp-external</code> &rarr; external Ceph <code>ceph9</code> &nbsp;•&nbsp; consume &amp; self-service provision</div></div>
</header>

<div class="wrap">
  {% if msg %}<div class="flash {{ msg[0] }}">{{ msg[1] }}</div>{% endif %}

  {% if prov_result %}
  <div class="result">
    <h3>✓ Provisioned on ceph9 — connection details</h3>
    <div class="kv">
      {% for k, v in prov_result.items() if not k.startswith('_') and k != 'name' %}
        <div class="k">{{ k }}</div><div class="v">{{ v }}</div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class="sec"><h2>Self-service provisioning</h2><div class="ln"></div></div>
  <div class="card">
    <h2>Provision new storage on ceph9</h2>
    <div class="hint">Calls the Ceph mgr REST API directly to create the resource, then returns its connection details.</div>
    <form class="row" method="post" action="{{ url_for('provision') }}">
      <div class="field">
        <label for="ptype">Type</label>
        <select name="ptype" id="ptype">
          <option value="s3">S3 bucket (RGW) — returns endpoint + keys</option>
          <option value="nfs">NFS export (nfslab) — returns server + path</option>
          <option value="cephfs">CephFS subvolume — returns path + mons</option>
        </select>
      </div>
      <div class="field">
        <label for="name">Name</label>
        <input type="text" name="name" id="name" placeholder="my-bucket-1" pattern="[a-z0-9][a-z0-9-]{2,40}" required>
      </div>
      <button type="submit">Provision +</button>
    </form>
  </div>

  <div class="sec"><h2>Consume mounted storage</h2><div class="ln"></div></div>
  <div class="card">
    <h2>Upload a file to Ceph</h2>
    <div class="hint">Block &amp; File are mounted PVCs; Object is the OBC bucket <code>{{ bucket }}</code>
      <span class="pill {{ 'on' if object_ready else 'off' }}">{{ 'ready' if object_ready else 'pending OBC' }}</span></div>
    <form class="row" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
      <div class="field"><label for="file">File</label><input type="file" name="file" id="file" required></div>
      <div class="field"><label for="endpoint">Endpoint</label>
        <select name="endpoint" id="endpoint">
          <option value="block">Block — Ceph RBD (RWO PVC)</option>
          <option value="file">File — CephFS (RWX PVC)</option>
          <option value="object" {{ '' if object_ready else 'disabled' }}>Object — Ceph RGW S3</option>
        </select></div>
      <button type="submit">Upload &uarr;</button>
    </form>
  </div>

  <div class="grid">
    {% for ep, info in endpoints.items() %}
    <div class="ep">
      <div class="head"><span class="t">{{ info.label }}</span><span class="tag">{{ info.tech }}</span></div>
      <div class="blurb">{{ info.blurb }}</div>
      {% set L = listings[ep] %}
      {% if L.error %}<div class="err">⚠ {{ L.error }}</div>
      {% elif L['items'] %}<ul>
        {% for it in L['items'] %}<li><span class="nm">{{ it.name }}</span>
          <span><span class="sz">{{ it.size }}</span> &nbsp;<a class="dl" href="{{ url_for('download', ep=ep, name=it.name) }}">download</a></span></li>{% endfor %}
        </ul>{% else %}<div class="empty">No files yet.</div>{% endif %}
    </div>
    {% endfor %}
  </div>
</div>

<footer>Provisioned resources and uploaded files live on the external Ceph cluster (ceph9){% if node %} • node {{ node }}{% endif %}.</footer>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
