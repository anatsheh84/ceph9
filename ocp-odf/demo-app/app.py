#!/usr/bin/env python3
"""
Ceph External Storage Portal
============================
OpenShift (ocp-external) using an EXTERNAL Ceph cluster (ceph9), two views:

  TAB 1  Self-Service Provisioning  — log in with Ceph creds, then create,
         browse, and retrieve connection details for S3 buckets / NFS exports /
         CephFS subvolumes via the Ceph REST API. RBAC: you see what you
         created; admins see everything.
  TAB 2  Consume Mounted Storage    — block (RBD PVC), file (CephFS PVC),
         object (OBC bucket): upload / list / download.

Provisioning uses the logged-in user's Ceph token (Ceph enforces RBAC). The mgr
endpoint is auto-discovered and per-endpoint API versions auto-negotiated, so it
survives a lab rebuild. A small registry (on the block PVC) records the creator
of each provisioned resource for app-level RBAC.
"""
import io
import json
import mimetypes
import os
import re
import time

import boto3
import requests
import urllib3
from botocore.client import Config
from flask import (Flask, abort, redirect, render_template_string, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ceph-external-portal-secret")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

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
CEPH_ADMIN_USER = os.environ.get("CEPH_ADMIN_USER", "admin")
CEPH_RGW_ENDPOINT = os.environ.get("CEPH_RGW_ENDPOINT", f"{BUCKET_HOST}:{BUCKET_PORT}")
CEPH_FS_NAME = os.environ.get("CEPH_FS_NAME", "cephfs")
CEPH_NFS_CLUSTER = os.environ.get("CEPH_NFS_CLUSTER", "nfslab")
CEPH_NFS_SERVER = os.environ.get("CEPH_NFS_SERVER", "")
CEPH_MON_HOSTS = os.environ.get("CEPH_MON_HOSTS", "10.20.2.11,10.20.2.12,10.20.2.13")
CEPH_RBD_POOL = os.environ.get("CEPH_RBD_POOL", "rbd")

TYPES = ("s3", "nfs", "cephfs", "rbd")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
REGISTRY_PATH = os.path.join(BLOCK_DIR, ".portal-registry.json")
PREVIEWABLE = ("text/", "image/", "application/json")


class CephAPIError(Exception):
    pass


class CephAPI:
    """Ceph mgr dashboard REST client: discovery + API-version negotiation.
    Auth is per-call: callers pass (base, token)."""

    @staticmethod
    def login(username, password):
        if not password:
            raise CephAPIError("password required")
        last = "no hosts configured"
        for h in CEPH_API_HOSTS:
            base = f"https://{h}:{CEPH_API_PORT}/api"
            try:
                r = requests.post(
                    base + "/auth", verify=False, timeout=8,
                    headers={"Accept": "application/vnd.ceph.api.v1.0+json",
                             "Content-Type": "application/json"},
                    json={"username": username, "password": password})
                if r.status_code in (200, 201):
                    d = r.json()
                    is_admin = (username == CEPH_ADMIN_USER) or \
                        ("rgw" in (d.get("permissions") or {}) and
                         "nfs-ganesha" in (d.get("permissions") or {}))
                    return base, d["token"], is_admin
                if r.status_code in (400, 401):
                    raise CephAPIError("invalid Ceph username or password")
                last = f"{h}: HTTP {r.status_code}"
            except CephAPIError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = f"{h}: {exc}"
        raise CephAPIError(f"no Ceph mgr reachable ({last})")

    @staticmethod
    def req(base, token, method, path, body=None, version="1.0"):
        ver = version
        r = None
        for _ in range(3):
            r = requests.request(
                method, base + path, verify=False, timeout=30,
                headers={"Accept": f"application/vnd.ceph.api.v{ver}+json",
                         "Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
                json=body)
            if r.status_code == 415 and "Incorrect version" in r.text:
                m = re.search(r"endpoint is '([\d.]+)'", r.text)
                if m and m.group(1) != ver:
                    ver = m.group(1)
                    continue
            return r
        return r

    @classmethod
    def _ok(cls, r, *codes):
        if r.status_code not in (codes or (200, 201)):
            detail = r.text[:200]
            try:
                detail = r.json().get("detail", detail)
            except Exception:  # noqa: BLE001
                pass
            if r.status_code == 403:
                raise CephAPIError("permission denied by Ceph RBAC")
            raise CephAPIError(f"HTTP {r.status_code}: {detail}")

    # ── provisioning ──────────────────────────────────────────────────────
    @classmethod
    def provision_s3(cls, base, token, name):
        u = cls.req(base, token, "POST", "/rgw/user", version="1.0", body={
            "uid": name, "display_name": name, "email": "",
            "max_buckets": 1000, "suspended": 0})
        cls._ok(u, 200, 201)
        b = cls.req(base, token, "POST", "/rgw/bucket", version="1.0",
                    body={"bucket": name, "uid": name})
        cls._ok(b, 200, 201)
        return cls.s3_details(base, token, name)

    @classmethod
    def provision_nfs(cls, base, token, name):
        pseudo = "/" + name
        e = cls.req(base, token, "POST", "/nfs-ganesha/export", version="2.0", body={
            "cluster_id": CEPH_NFS_CLUSTER, "path": "/", "pseudo": pseudo,
            "access_type": "RW", "squash": "no_root_squash",
            "security_label": False, "protocols": [4], "transports": ["TCP"],
            "fsal": {"name": "CEPH", "fs_name": CEPH_FS_NAME}, "clients": []})
        cls._ok(e, 200, 201)
        return cls.nfs_details(base, token, name)

    @classmethod
    def provision_cephfs(cls, base, token, name):
        c = cls.req(base, token, "POST", "/cephfs/subvolume", version="1.0", body={
            "vol_name": CEPH_FS_NAME, "subvol_name": name, "size": 1073741824})
        cls._ok(c, 200, 201)
        return cls.cephfs_details(base, token, name)

    # ── details (re-derived live, any time) ───────────────────────────────
    @classmethod
    def rgw_keys(cls, base, token, uid):
        u = cls.req(base, token, "GET", f"/rgw/user/{uid}", version="1.0")
        cls._ok(u, 200)
        return (u.json().get("keys") or [{}])[0]

    @classmethod
    def s3_details(cls, base, token, name):
        k = cls.rgw_keys(base, token, name)
        return {"_type": "s3", "name": name,
                "Endpoint": f"http://{CEPH_RGW_ENDPOINT}", "Bucket": name,
                "Access key": k.get("access_key", ""),
                "Secret key": k.get("secret_key", "")}

    @classmethod
    def nfs_details(cls, base, token, name):
        pseudo = "/" + name
        lst = cls.req(base, token, "GET", "/nfs-ganesha/export", version="1.0")
        cls._ok(lst, 200)
        match = next((e for e in lst.json() if e.get("pseudo") == pseudo), None)
        if not match:
            raise CephAPIError(f"NFS export {pseudo} not found")
        server = CEPH_NFS_SERVER or "<nfs-server>:2049"
        host = server.split(":")[0]
        return {"_type": "nfs", "name": name, "NFS server": server,
                "Export (pseudo)": pseudo, "Cluster": CEPH_NFS_CLUSTER,
                "Export ID": str(match.get("export_id", "")),
                "Mount command": f"sudo mount -t nfs4 {host}:{pseudo} /mnt/{name}"}

    @classmethod
    def cephfs_details(cls, base, token, name):
        info = cls.req(base, token, "GET",
                       f"/cephfs/subvolume/{CEPH_FS_NAME}/info?subvol_name={name}",
                       version="1.0")
        cls._ok(info, 200)
        path = info.json().get("path", "")
        mons = ",".join(f"{m}:6789" for m in CEPH_MON_HOSTS.split(","))
        return {"_type": "cephfs", "name": name, "Filesystem": CEPH_FS_NAME,
                "Subvolume": name, "Path": path, "Monitors": mons,
                "Note": "Mount in-cluster via the cephfs StorageClass, or export "
                        "this subvolume over NFS for external access."}

    @classmethod
    def provision_rbd(cls, base, token, name):
        c = cls.req(base, token, "POST", "/block/image", version="1.0", body={
            "pool_name": CEPH_RBD_POOL, "name": name, "size": 1073741824,
            "features": ["layering"]})
        cls._ok(c, 200, 201)
        return cls.rbd_details(base, token, name)

    @classmethod
    def rbd_details(cls, base, token, name):
        img = next((i for i in cls.list_rbd_full(base, token) if i.get("name") == name), None)
        if not img:
            raise CephAPIError(f"RBD image {name} not found")
        size = img.get("size", 0)
        gib = f"{size / (1 << 30):.0f} GiB" if size else "?"
        mons = ",".join(f"{m}:6789" for m in CEPH_MON_HOSTS.split(","))
        return {"_type": "rbd", "name": name, "Pool": CEPH_RBD_POOL, "Image": name,
                "Size": gib, "Monitors": mons,
                "Map command": f"sudo rbd map {CEPH_RBD_POOL}/{name} --id <client> --keyring <keyring>",
                "Note": "Consume in-cluster via the ceph-rbd StorageClass (block PVC); "
                        "raw 'rbd map' needs a cephx key."}

    @classmethod
    def details(cls, base, token, typ, name):
        return {"s3": cls.s3_details, "nfs": cls.nfs_details,
                "cephfs": cls.cephfs_details, "rbd": cls.rbd_details}[typ](base, token, name)

    # ── live discovery (list everything that exists in Ceph) ──────────────
    @classmethod
    def _list(cls, base, token, path, version="1.0"):
        r = cls.req(base, token, "GET", path, version=version)
        if r.status_code == 403:
            return None  # caller lacks the capability for this resource type
        cls._ok(r, 200)
        return r.json()

    @classmethod
    def list_s3(cls, base, token):
        d = cls._list(base, token, "/rgw/bucket")
        return list(d) if d else []

    @classmethod
    def list_nfs(cls, base, token):
        d = cls._list(base, token, "/nfs-ganesha/export")
        return [e.get("pseudo", "").lstrip("/") for e in (d or []) if e.get("pseudo")]

    @classmethod
    def list_cephfs(cls, base, token):
        d = cls._list(base, token, f"/cephfs/subvolume/{CEPH_FS_NAME}")
        return [s.get("name") for s in (d or []) if s.get("name")]

    @classmethod
    def list_rbd_full(cls, base, token):
        d = cls._list(base, token, "/block/image", version="2.0")
        out = []
        for grp in (d or []):
            for img in grp.get("value", []):
                if img.get("pool_name") == CEPH_RBD_POOL:
                    out.append(img)
        return out

    @classmethod
    def list_rbd(cls, base, token):
        return [i.get("name") for i in cls.list_rbd_full(base, token) if i.get("name")]

    @classmethod
    def list_all(cls, base, token):
        return {"s3": cls.list_s3(base, token), "nfs": cls.list_nfs(base, token),
                "cephfs": cls.list_cephfs(base, token), "rbd": cls.list_rbd(base, token)}


# ───────────────────────────── registry (app RBAC) ───────────────────────
def load_registry():
    try:
        with open(REGISTRY_PATH) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return []


def save_registry(items):
    try:
        tmp = REGISTRY_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(items, fh)
        os.replace(tmp, REGISTRY_PATH)
    except Exception:  # noqa: BLE001
        pass


def registry_add(typ, name, creator):
    items = load_registry()
    if not any(e["type"] == typ and e["name"] == name for e in items):
        items.append({"type": typ, "name": name, "creator": creator,
                      "created": int(time.time())})
        save_registry(items)


def find_entry(typ, name):
    return next((e for e in load_registry()
                 if e["type"] == typ and e["name"] == name), None)


def live_entries(ctx):
    """LIVE discovery: list everything that actually exists in Ceph. Admins see
    all; non-admins see only resources the registry attributes to them."""
    reg = {(e["type"], e["name"]): e for e in load_registry()}
    listers = {"s3": CephAPI.list_s3, "nfs": CephAPI.list_nfs,
               "cephfs": CephAPI.list_cephfs, "rbd": CephAPI.list_rbd}
    out = []
    for typ, lister in listers.items():
        try:
            names = lister(ctx["base"], ctx["token"])
        except Exception:  # noqa: BLE001
            names = []
        for name in names:
            entry = reg.get((typ, name))
            if ctx["is_admin"]:
                out.append({"type": typ, "name": name,
                            "creator": entry["creator"] if entry else "(external)"})
            elif entry and entry.get("creator") == ctx["user"]:
                out.append({"type": typ, "name": name, "creator": ctx["user"]})
    out.sort(key=lambda x: (x["type"], x["name"]))
    return out


def authorize(ctx, typ, name):
    """Admins may access anything live; non-admins only what they created."""
    entry = find_entry(typ, name)
    if ctx["is_admin"]:
        return entry or {"type": typ, "name": name, "creator": "(external)"}
    if not entry or entry.get("creator") != ctx["user"]:
        abort(403)
    return entry


# ───────────────────────────── consume helpers ───────────────────────────
def object_ready():
    return all([BUCKET_NAME, BUCKET_HOST, S3_KEY, S3_SECRET])


def s3_client_consume():
    return boto3.client(
        "s3", endpoint_url=f"http://{BUCKET_HOST}:{BUCKET_PORT}",
        aws_access_key_id=S3_KEY, aws_secret_access_key=S3_SECRET,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1")


def s3_client_for(access, secret):
    return boto3.client(
        "s3", endpoint_url=f"http://{CEPH_RGW_ENDPOINT}",
        aws_access_key_id=access, aws_secret_access_key=secret,
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
                if name.startswith("."):
                    continue
                p = os.path.join(info["dir"], name)
                if os.path.isfile(p):
                    out.append({"name": name, "size": human(os.path.getsize(p))})
        elif info["kind"] == "s3" and object_ready():
            resp = s3_client_consume().list_objects_v2(Bucket=BUCKET_NAME)
            for o in sorted(resp.get("Contents", []), key=lambda x: x["Key"]):
                out.append({"name": o["Key"], "size": human(o["Size"])})
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "items": []}
    return {"error": None, "items": out}


# ───────────────────────────── session / render ──────────────────────────
def ceph_ctx():
    if session.get("ceph_token"):
        return {"base": session["ceph_base"], "token": session["ceph_token"],
                "user": session["ceph_user"], "is_admin": session.get("ceph_admin", False)}
    return None


def require_ceph():
    ctx = ceph_ctx()
    if not ctx:
        abort(401)
    return ctx


def render_index(**extra):
    listings = {ep: list_files(ep) for ep in ENDPOINTS}
    ctx = ceph_ctx()
    entries = live_entries(ctx) if ctx else []
    return render_template_string(
        PAGE, endpoints=ENDPOINTS, listings=listings, object_ready=object_ready(),
        bucket=BUCKET_NAME or "(not provisioned)", node=os.environ.get("NODE_NAME", ""),
        ceph=ctx, entries=entries, active_tab=extra.pop("active_tab", "provision"), **extra)


# ───────────────────────────── routes: core/auth ─────────────────────────
@app.route("/")
def index():
    return render_index(active_tab=request.args.get("tab", "provision"))


@app.route("/ceph/login", methods=["POST"])
def ceph_login():
    user = (request.form.get("username") or "").strip()
    pw = request.form.get("password") or ""
    try:
        base, token, is_admin = CephAPI.login(user, pw)
        session.update(ceph_base=base, ceph_token=token, ceph_user=user, ceph_admin=is_admin)
        mgr = base.split("//", 1)[1].split(":", 1)[0]
        role = "administrator" if is_admin else "user"
        return render_index(active_tab="provision",
                            msg=("ok", f"Connected to Ceph mgr {mgr} as '{user}' ({role})."))
    except Exception as exc:  # noqa: BLE001
        return render_index(active_tab="provision", msg=("error", f"Ceph login failed: {exc}"))


@app.route("/ceph/logout", methods=["POST"])
def ceph_logout():
    for k in ("ceph_base", "ceph_token", "ceph_user", "ceph_admin"):
        session.pop(k, None)
    return render_index(active_tab="provision", msg=("ok", "Disconnected from Ceph."))


@app.route("/provision", methods=["POST"])
def provision():
    ctx = ceph_ctx()
    if not ctx:
        return render_index(active_tab="provision", msg=("error", "Log in to Ceph first."))
    ptype = request.form.get("ptype", "")
    name = (request.form.get("name", "") or "").strip().lower()
    if ptype not in TYPES:
        return render_index(active_tab="provision", msg=("error", "Unknown provision type."))
    if not NAME_RE.match(name):
        return render_index(active_tab="provision",
                            msg=("error", "Name: 3-41 chars, lowercase letters/digits/dashes."))
    try:
        fn = {"s3": CephAPI.provision_s3, "nfs": CephAPI.provision_nfs,
              "cephfs": CephAPI.provision_cephfs, "rbd": CephAPI.provision_rbd}[ptype]
        result = fn(ctx["base"], ctx["token"], name)
        registry_add(ptype, name, ctx["user"])
        return render_index(active_tab="provision", prov_result=result,
                            msg=("ok", f"Provisioned {ptype.upper()} '{name}' on ceph9."))
    except Exception as exc:  # noqa: BLE001
        return render_index(active_tab="provision", msg=("error", f"Provision failed: {exc}"))


# ───────────────────────────── routes: details / browse ──────────────────
@app.route("/details/<typ>/<name>")
def details(typ, name):
    ctx = require_ceph()
    entry = authorize(ctx, typ, name)
    try:
        d = CephAPI.details(ctx["base"], ctx["token"], typ, name)
    except Exception as exc:  # noqa: BLE001
        return render_template_string(DETAILS_PAGE, name=name, typ=typ, entry=entry,
                                      detail=None, error=str(exc))
    return render_template_string(DETAILS_PAGE, name=name, typ=typ, entry=entry,
                                  detail=d, error=None)


@app.route("/browse/<name>")
def browse(name):
    ctx = require_ceph()
    authorize(ctx, "s3", name)
    error, objs = None, []
    try:
        k = CephAPI.rgw_keys(ctx["base"], ctx["token"], name)
        cli = s3_client_for(k["access_key"], k["secret_key"])
        resp = cli.list_objects_v2(Bucket=name)
        for o in sorted(resp.get("Contents", []), key=lambda x: x["Key"]):
            ctype = mimetypes.guess_type(o["Key"])[0] or "application/octet-stream"
            objs.append({"key": o["Key"], "size": human(o["Size"]),
                         "preview": ctype.startswith(PREVIEWABLE)})
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    return render_template_string(BROWSE_PAGE, bucket=name, objs=objs, error=error,
                                  endpoint=CEPH_RGW_ENDPOINT)


@app.route("/browse/<name>/upload", methods=["POST"])
def browse_upload(name):
    ctx = require_ceph()
    authorize(ctx, "s3", name)
    f = request.files.get("file")
    if f and f.filename:
        try:
            k = CephAPI.rgw_keys(ctx["base"], ctx["token"], name)
            s3_client_for(k["access_key"], k["secret_key"]).upload_fileobj(
                f, name, secure_filename(f.filename))
        except Exception:  # noqa: BLE001
            pass
    return redirect(url_for("browse", name=name))


@app.route("/browse/<name>/obj/<path:key>")
def browse_object(name, key):
    ctx = require_ceph()
    authorize(ctx, "s3", name)
    inline = request.args.get("inline") == "1"
    key = secure_filename(key)
    try:
        k = CephAPI.rgw_keys(ctx["base"], ctx["token"], name)
        obj = s3_client_for(k["access_key"], k["secret_key"]).get_object(Bucket=name, Key=key)
        data = obj["Body"].read()
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        return send_file(io.BytesIO(data), mimetype=ctype if inline else None,
                         as_attachment=not inline, download_name=key)
    except Exception:  # noqa: BLE001
        abort(404)


# ───────────────────────────── routes: consume ───────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    ep = request.form.get("endpoint", "")
    if ep not in ENDPOINTS:
        return render_index(active_tab="consume", msg=("error", "Unknown storage endpoint."))
    f = request.files.get("file")
    if not f or f.filename == "":
        return render_index(active_tab="consume", msg=("error", "Please choose a file."))
    name = secure_filename(f.filename)
    info = ENDPOINTS[ep]
    try:
        if info["kind"] == "fs":
            f.save(os.path.join(info["dir"], name))
        else:
            if not object_ready():
                raise RuntimeError("Object endpoint not configured (OBC not bound).")
            s3_client_consume().upload_fileobj(f, BUCKET_NAME, name)
        return render_index(active_tab="consume",
                            msg=("ok", f"Uploaded '{name}' to {info['label']} ({info['tech']}) on ceph9."))
    except Exception as exc:  # noqa: BLE001
        return render_index(active_tab="consume", msg=("error", f"Upload failed: {exc}"))


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
        obj = s3_client_consume().get_object(Bucket=BUCKET_NAME, Key=name)
        return send_file(io.BytesIO(obj["Body"].read()), as_attachment=True, download_name=name)
    except Exception:  # noqa: BLE001
        abort(404)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


# ───────────────────────────── templates ─────────────────────────────────
STYLE = r"""
<style>
  :root{--rh-red:#ee0000;--rh-red-dark:#be0000;--bg:#0f0f0f;--panel:#1b1b1b;
        --panel-2:#242424;--line:#3c3c3c;--text:#fff;--muted:#a0a0a0;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:"Red Hat Display","Red Hat Text",-apple-system,Segoe UI,Roboto,Arial,sans-serif;}
  header{background:#000;border-bottom:4px solid var(--rh-red);padding:18px 28px;display:flex;align-items:center;gap:16px}
  .logo{width:46px;height:32px;background:var(--rh-red);border-radius:3px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px}
  header h1{font-size:20px;margin:0}header .sub{color:var(--muted);font-size:13px;margin-top:2px}
  a.back{color:#ff5a5a;text-decoration:none;font-weight:700;font-size:13px}
  .wrap{max-width:1100px;margin:0 auto;padding:0 28px 28px}
  .tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:0 0 24px;position:sticky;top:0;background:var(--bg);padding-top:20px}
  .tab{background:none;border:0;color:var(--muted);font-size:15px;font-weight:700;padding:14px 22px;cursor:pointer;border-bottom:3px solid transparent;font-family:inherit}
  .tab.active{color:#fff;border-bottom-color:var(--rh-red)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:22px;margin:0 0 24px}
  .card h2{margin:0 0 4px;font-size:16px}.card .hint{color:var(--muted);font-size:12px;margin-bottom:16px}
  form.row{display:flex;flex-wrap:wrap;gap:14px;align-items:end}
  .field{display:flex;flex-direction:column;gap:6px}
  label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  select,input[type=file],input[type=text],input[type=password]{background:var(--panel-2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:10px 12px;font-size:14px;min-width:220px}
  input[type=file]::file-selector-button{background:#333;color:#fff;border:0;border-radius:5px;padding:7px 12px;margin-right:10px;cursor:pointer}
  button.go{background:var(--rh-red);color:#fff;border:0;border-radius:6px;padding:11px 26px;font-size:14px;font-weight:700;cursor:pointer}
  button.go:hover{background:var(--rh-red-dark)}
  button.ghost,a.ghost{background:#2b2b2b;color:#fff;border:1px solid var(--line);border-radius:6px;padding:7px 14px;font-weight:700;cursor:pointer;font-size:12px;text-decoration:none;display:inline-block}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  td .t{font-size:11px;background:var(--rh-red);padding:2px 8px;border-radius:10px}
  .flash{padding:12px 16px;border-radius:6px;margin-bottom:18px;font-size:14px}
  .flash.ok{background:#0d2c12;border:1px solid #1f7a33;color:#9be8ab}
  .flash.error{background:#2c0d0d;border:1px solid #7a1f1f;color:#ffb0b0}
  .result{background:#10240f;border:1px solid #2f7a33;border-radius:8px;padding:18px;margin-bottom:24px}
  .result h3{margin:0 0 12px;font-size:15px;color:#9be8ab}
  .kv{display:grid;grid-template-columns:170px 1fr;gap:8px 14px;font-size:13px}
  .kv .k{color:var(--muted)}.kv .v{font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all;background:#000;padding:3px 8px;border-radius:4px}
  .status{display:flex;align-items:center;gap:10px;font-size:13px;color:var(--muted)}
  .dot{width:9px;height:9px;border-radius:50%}.dot.on{background:#3fb950}
  .badge{font-size:11px;padding:2px 8px;border-radius:10px;background:#1f7a33}.badge.user{background:#444}
  .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
  .ep{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .ep .head{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
  .ep .head .t{font-weight:700}.ep .head .tag{font-size:11px;background:var(--rh-red);padding:2px 8px;border-radius:10px}
  .ep .blurb{color:var(--muted);font-size:12px;padding:10px 18px 0}
  .ep ul{list-style:none;margin:8px 0 0;padding:8px 10px;max-height:280px;overflow:auto}
  .ep li{display:flex;justify-content:space-between;gap:10px;padding:9px 10px;border-radius:6px}
  .ep li:hover{background:var(--panel-2)}.ep li .nm{font-size:13px;word-break:break-all}
  .ep li .sz{color:var(--muted);font-size:11px;white-space:nowrap}.ep a.dl{color:#ff5a5a;text-decoration:none;font-size:12px;font-weight:700}
  .empty{color:var(--muted);font-size:13px;padding:14px 18px}.err{color:#ff8080;font-size:12px;padding:10px 0}
  code{background:#000;padding:1px 6px;border-radius:4px;color:#ff8080}
  .pill{font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px}.pill.on{background:#1f7a33}.pill.off{background:#555}
  .hidden{display:none}
</style>
"""

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ceph External Storage Portal</title>""" + STYLE + r"""</head><body>
<header><div class="logo">RH</div><div><h1>Ceph External Storage Portal</h1>
<div class="sub">OpenShift <code>ocp-external</code> &rarr; external Ceph <code>ceph9</code></div></div></header>
<div class="wrap">
  <div class="tabs">
    <button class="tab" id="tab-provision" onclick="showTab('provision')">Self-Service Provisioning</button>
    <button class="tab" id="tab-consume" onclick="showTab('consume')">Consume Mounted Storage</button>
  </div>
  {% if msg %}<div class="flash {{ msg[0] }}">{{ msg[1] }}</div>{% endif %}

  <section id="view-provision">
    <div class="card">
      <h2>Ceph connection</h2>
      <div class="hint">Self-service provisioning uses <b>your</b> Ceph credentials — Ceph enforces RBAC.</div>
      {% if ceph %}
        <div class="status"><span class="dot on"></span>Connected as <b style="color:#fff">&nbsp;{{ ceph.user }}</b>
          <span class="badge {{ '' if ceph.is_admin else 'user' }}">{{ 'administrator' if ceph.is_admin else 'user' }}</span>
          <form method="post" action="{{ url_for('ceph_logout') }}" style="margin-left:14px">
            <button class="ghost" type="submit">Disconnect</button></form></div>
      {% else %}
        <form class="row" method="post" action="{{ url_for('ceph_login') }}">
          <div class="field"><label>Ceph username</label><input type="text" name="username" value="admin" required></div>
          <div class="field"><label>Ceph password</label><input type="password" name="password" placeholder="••••••••" required></div>
          <button class="go" type="submit">Connect &amp; verify</button></form>
      {% endif %}
    </div>

    {% if ceph %}
      {% if prov_result %}
      <div class="result"><h3>✓ Provisioned on ceph9 — connection details</h3>
        <div class="kv">{% for k, v in prov_result.items() if not k.startswith('_') and k != 'name' %}
          <div class="k">{{ k }}</div><div class="v">{{ v }}</div>{% endfor %}</div></div>
      {% endif %}
      <div class="card"><h2>Provision new storage on ceph9</h2>
        <div class="hint">Creates the resource via the Ceph mgr REST API and records you as its owner.</div>
        <form class="row" method="post" action="{{ url_for('provision') }}">
          <div class="field"><label>Type</label><select name="ptype">
            <option value="s3">S3 bucket (RGW)</option><option value="nfs">NFS export (nfslab)</option>
            <option value="cephfs">CephFS subvolume</option><option value="rbd">RBD image (block)</option></select></div>
          <div class="field"><label>Name</label><input type="text" name="name" placeholder="my-bucket-1" pattern="[a-z0-9][a-z0-9-]{2,40}" required></div>
          <button class="go" type="submit">Provision +</button></form></div>

      <div class="card"><h2>Provisioned endpoints on ceph9 <span class="badge">live</span>
        {% if ceph.is_admin %}<span class="badge">admin: all resources</span>{% endif %}</h2>
        <div class="hint">Discovered live from Ceph (S3 · NFS · CephFS · RBD). Retrieve connection details any time, or browse S3 content.{% if not ceph.is_admin %} You see resources you created.{% endif %}</div>
        {% if entries %}
        <table><tr><th>Type</th><th>Name</th><th>Owner</th><th>Actions</th></tr>
          {% for e in entries %}<tr>
            <td><span class="t">{{ e.type|upper }}</span></td>
            <td style="font-family:ui-monospace,monospace">{{ e.name }}</td>
            <td>{{ e.creator }}</td>
            <td><a class="ghost" href="{{ url_for('details', typ=e.type, name=e.name) }}">details</a>
              {% if e.type == 's3' %}<a class="ghost" href="{{ url_for('browse', name=e.name) }}">browse</a>{% endif %}</td>
          </tr>{% endfor %}</table>
        {% else %}<div class="empty">No endpoints provisioned yet.</div>{% endif %}
      </div>
    {% endif %}
  </section>

  <section id="view-consume" class="hidden">
    <div class="card"><h2>Upload a file to mounted Ceph storage</h2>
      <div class="hint">Block &amp; File are mounted PVCs; Object is the OBC bucket <code>{{ bucket }}</code>
        <span class="pill {{ 'on' if object_ready else 'off' }}">{{ 'ready' if object_ready else 'pending OBC' }}</span></div>
      <form class="row" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
        <div class="field"><label>File</label><input type="file" name="file" required></div>
        <div class="field"><label>Endpoint</label><select name="endpoint">
          <option value="block">Block — Ceph RBD (RWO PVC)</option><option value="file">File — CephFS (RWX PVC)</option>
          <option value="object" {{ '' if object_ready else 'disabled' }}>Object — Ceph RGW S3</option></select></div>
        <button class="go" type="submit">Upload &uarr;</button></form></div>
    <div class="grid">{% for ep, info in endpoints.items() %}
      <div class="ep"><div class="head"><span class="t">{{ info.label }}</span><span class="tag">{{ info.tech }}</span></div>
        <div class="blurb">{{ info.blurb }}</div>
        {% set L = listings[ep] %}
        {% if L.error %}<div class="err" style="padding:10px 18px">⚠ {{ L.error }}</div>
        {% elif L['items'] %}<ul>{% for it in L['items'] %}<li><span class="nm">{{ it.name }}</span>
          <span><span class="sz">{{ it.size }}</span> &nbsp;<a class="dl" href="{{ url_for('download', ep=ep, name=it.name) }}">download</a></span></li>{% endfor %}</ul>
        {% else %}<div class="empty">No files yet.</div>{% endif %}
      </div>{% endfor %}</div>
  </section>
</div>
<footer style="color:#a0a0a0;font-size:12px;text-align:center;padding:20px">Resources and files live on the external Ceph cluster (ceph9){% if node %} • node {{ node }}{% endif %}.</footer>
<script>
  function showTab(t){for(const n of ['provision','consume']){
    document.getElementById('view-'+n).classList.toggle('hidden', n!==t);
    document.getElementById('tab-'+n).classList.toggle('active', n===t);}
    history.replaceState(null,'','?tab='+t);}
  showTab({{ active_tab|tojson }});
</script></body></html>
"""

DETAILS_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Details — {{ name }}</title>""" + STYLE + r"""</head><body>
<header><div class="logo">RH</div><div><h1>Connection details</h1>
<div class="sub"><a class="back" href="/?tab=provision">&larr; back to portal</a></div></div></header>
<div class="wrap" style="padding-top:24px">
  <div class="card"><h2><span class="t" style="background:var(--rh-red);padding:2px 8px;border-radius:10px">{{ typ|upper }}</span> &nbsp;{{ name }}</h2>
    <div class="hint">Owner: {{ entry.creator }} — re-derived live from Ceph.</div>
    {% if error %}<div class="err">⚠ {{ error }}</div>
    {% else %}<div class="kv">{% for k, v in detail.items() if not k.startswith('_') and k != 'name' %}
      <div class="k">{{ k }}</div><div class="v">{{ v }}</div>{% endfor %}</div>{% endif %}
  </div></div></body></html>
"""

BROWSE_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Browse — {{ bucket }}</title>""" + STYLE + r"""</head><body>
<header><div class="logo">RH</div><div><h1>Browse S3 bucket</h1>
<div class="sub"><a class="back" href="/?tab=provision">&larr; back to portal</a> &nbsp;•&nbsp; <code>{{ bucket }}</code> @ {{ endpoint }}</div></div></header>
<div class="wrap" style="padding-top:24px">
  <div class="card"><h2>Upload to {{ bucket }}</h2>
    <form class="row" method="post" action="{{ url_for('browse_upload', name=bucket) }}" enctype="multipart/form-data">
      <div class="field"><label>File</label><input type="file" name="file" required></div>
      <button class="go" type="submit">Upload &uarr;</button></form></div>
  <div class="card"><h2>Objects</h2>
    {% if error %}<div class="err">⚠ {{ error }}</div>
    {% elif objs %}<table><tr><th>Key</th><th>Size</th><th>Actions</th></tr>
      {% for o in objs %}<tr><td style="font-family:ui-monospace,monospace">{{ o.key }}</td><td>{{ o.size }}</td>
        <td>{% if o.preview %}<a class="ghost" target="_blank" href="{{ url_for('browse_object', name=bucket, key=o.key) }}?inline=1">preview</a>{% endif %}
          <a class="ghost" href="{{ url_for('browse_object', name=bucket, key=o.key) }}">download</a></td></tr>{% endfor %}</table>
    {% else %}<div class="empty">Bucket is empty.</div>{% endif %}
  </div></div></body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
