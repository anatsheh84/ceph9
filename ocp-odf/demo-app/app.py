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

# --- storage tiers ---
RBD_POOLS = {"ssd": CEPH_RBD_POOL, "hdd": os.environ.get("CEPH_RBD_HDD_POOL", "rbd-hdd")}
CEPHFS_HDD_POOL = os.environ.get("CEPH_CEPHFS_HDD_POOL", "cephfs.cephfs.data.hdd")
RGW_HDD_SC = os.environ.get("CEPH_RGW_HDD_SC", "HDD")
# RGW placement target whose default (STANDARD) class lives on the hdd pool —
# buckets created here are HDD by default (bucket-level tier).
RGW_HDD_PLACEMENT = os.environ.get("CEPH_RGW_HDD_PLACEMENT", "hdd-placement")
TIERS = ("ssd", "hdd")
TIER_LABEL = {"ssd": "Tier0 · SSD", "hdd": "Tier1 · HDD"}
RBD_POOL_TIER = {v: k for k, v in RBD_POOLS.items()}   # pool name -> tier

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
    def provision_s3(cls, base, token, name, tier="ssd"):
        # Create the RGW user via the dashboard API (returns S3 keys)...
        u = cls.req(base, token, "POST", "/rgw/user", version="1.0", body={
            "uid": name, "display_name": name, "email": "",
            "max_buckets": 1000, "suspended": 0})
        cls._ok(u, 200, 201)
        k = (u.json().get("keys") or [{}])[0]
        ak, sk = k.get("access_key"), k.get("secret_key")
        if not ak:
            raise CephAPIError("RGW user created without S3 keys")
        # ...then create the bucket via S3 so we can pin its placement target.
        # tier=hdd -> ':hdd-placement' (bucket is HDD by default, no per-object header).
        cli = s3_client_for(ak, sk)
        if tier == "hdd":
            cli.create_bucket(Bucket=name,
                              CreateBucketConfiguration={"LocationConstraint": ":" + RGW_HDD_PLACEMENT})
        else:
            cli.create_bucket(Bucket=name)
        return cls.s3_details(base, token, name, tier)

    @classmethod
    def provision_nfs(cls, base, token, name, tier="ssd"):
        # tier=hdd -> export an hdd-pool CephFS subvolume (data lands on hdd);
        # tier=ssd -> export the fs root (default data pool).
        path = "/"
        if tier == "hdd":
            sub = "nfs-" + name
            c = cls.req(base, token, "POST", "/cephfs/subvolume", version="1.0", body={
                "vol_name": CEPH_FS_NAME, "subvol_name": sub, "size": 1073741824,
                "pool_layout": CEPHFS_HDD_POOL})
            cls._ok(c, 200, 201)
            info = cls.req(base, token, "GET",
                           f"/cephfs/subvolume/{CEPH_FS_NAME}/info?subvol_name={sub}", version="1.0")
            if info.status_code == 200:
                path = info.json().get("path", "/")
        pseudo = "/" + name
        e = cls.req(base, token, "POST", "/nfs-ganesha/export", version="2.0", body={
            "cluster_id": CEPH_NFS_CLUSTER, "path": path, "pseudo": pseudo,
            "access_type": "RW", "squash": "no_root_squash",
            "security_label": False, "protocols": [4], "transports": ["TCP"],
            "fsal": {"name": "CEPH", "fs_name": CEPH_FS_NAME}, "clients": []})
        cls._ok(e, 200, 201)
        return cls.nfs_details(base, token, name, tier)

    @classmethod
    def provision_cephfs(cls, base, token, name, tier="ssd"):
        body = {"vol_name": CEPH_FS_NAME, "subvol_name": name, "size": 1073741824}
        if tier == "hdd":
            body["pool_layout"] = CEPHFS_HDD_POOL
        c = cls.req(base, token, "POST", "/cephfs/subvolume", version="1.0", body=body)
        cls._ok(c, 200, 201)
        return cls.cephfs_details(base, token, name, tier)

    # ── details (re-derived live, any time) ───────────────────────────────
    @classmethod
    def rgw_keys(cls, base, token, uid):
        u = cls.req(base, token, "GET", f"/rgw/user/{uid}", version="1.0")
        cls._ok(u, 200)
        return (u.json().get("keys") or [{}])[0]

    @classmethod
    def bucket_owner(cls, base, token, bucket):
        r = cls.req(base, token, "GET", f"/rgw/bucket/{bucket}", version="1.0")
        cls._ok(r, 200)
        owner = r.json().get("owner")
        if not owner:
            raise CephAPIError(f"could not determine owner of bucket '{bucket}'")
        return owner

    @classmethod
    def bucket_creds(cls, base, token, bucket):
        """Resolve a bucket's real owner, then fetch that user's S3 keys.
        (Owner uid is NOT always the bucket name — e.g. labuser, OBC users.)"""
        owner = cls.bucket_owner(base, token, bucket)
        k = cls.rgw_keys(base, token, owner)
        if not k.get("access_key"):
            raise CephAPIError(f"bucket owner '{owner}' has no S3 access keys")
        return k["access_key"], k["secret_key"], owner

    @classmethod
    def s3_details(cls, base, token, name, tier=None):
        # Tier is authoritative from the bucket's live placement_rule.
        r = cls.req(base, token, "GET", f"/rgw/bucket/{name}", version="1.0")
        cls._ok(r, 200)
        info = r.json()
        owner = info.get("owner")
        placement = info.get("placement_rule") or "default-placement"
        dtier = "hdd" if placement == RGW_HDD_PLACEMENT else "ssd"
        k = cls.rgw_keys(base, token, owner)
        return {"_type": "s3", "name": name, "Tier": TIER_LABEL.get(dtier, dtier),
                "Placement": placement, "Endpoint": f"http://{CEPH_RGW_ENDPOINT}",
                "Bucket": name, "Owner": owner,
                "Access key": k.get("access_key", ""), "Secret key": k.get("secret_key", "")}

    @classmethod
    def nfs_details(cls, base, token, name, tier="ssd"):
        pseudo = "/" + name
        lst = cls.req(base, token, "GET", "/nfs-ganesha/export", version="1.0")
        cls._ok(lst, 200)
        match = next((e for e in lst.json() if e.get("pseudo") == pseudo), None)
        if not match:
            raise CephAPIError(f"NFS export {pseudo} not found")
        server = CEPH_NFS_SERVER or "<nfs-server>:2049"
        host = server.split(":")[0]
        d = {"_type": "nfs", "name": name, "Tier": TIER_LABEL.get(tier, tier),
             "NFS server": server, "Export (pseudo)": pseudo, "Cluster": CEPH_NFS_CLUSTER,
             "Export ID": str(match.get("export_id", "")), "Backing path": match.get("path", "/"),
             "Mount command": f"sudo mount -t nfs4 {host}:{pseudo} /mnt/{name}"}
        if tier == "hdd":
            d["Data pool"] = CEPHFS_HDD_POOL
        return d

    @classmethod
    def cephfs_details(cls, base, token, name, tier="ssd"):
        info = cls.req(base, token, "GET",
                       f"/cephfs/subvolume/{CEPH_FS_NAME}/info?subvol_name={name}",
                       version="1.0")
        cls._ok(info, 200)
        path = info.json().get("path", "")
        mons = ",".join(f"{m}:6789" for m in CEPH_MON_HOSTS.split(","))
        return {"_type": "cephfs", "name": name, "Tier": TIER_LABEL.get(tier, tier),
                "Filesystem": CEPH_FS_NAME, "Subvolume": name, "Path": path,
                "Data pool": (CEPHFS_HDD_POOL if tier == "hdd" else "default"),
                "Monitors": mons,
                "Note": "Mount in-cluster via the cephfs StorageClass, or export "
                        "this subvolume over NFS for external access."}

    @classmethod
    def provision_rbd(cls, base, token, name, tier="ssd"):
        pool = RBD_POOLS.get(tier, CEPH_RBD_POOL)
        c = cls.req(base, token, "POST", "/block/image", version="1.0", body={
            "pool_name": pool, "name": name, "size": 1073741824, "features": ["layering"]})
        cls._ok(c, 200, 201)
        return cls.rbd_details(base, token, name)

    @classmethod
    def rbd_details(cls, base, token, name, tier=None):
        img = next((i for i in cls.list_rbd_full(base, token) if i.get("name") == name), None)
        if not img:
            raise CephAPIError(f"RBD image {name} not found")
        pool = img.get("pool_name", CEPH_RBD_POOL)
        rtier = RBD_POOL_TIER.get(pool, "ssd")
        size = img.get("size", 0)
        gib = f"{size / (1 << 30):.0f} GiB" if size else "?"
        mons = ",".join(f"{m}:6789" for m in CEPH_MON_HOSTS.split(","))
        return {"_type": "rbd", "name": name, "Tier": TIER_LABEL.get(rtier, rtier),
                "Pool": pool, "Image": name, "Size": gib, "Monitors": mons,
                "Map command": f"sudo rbd map {pool}/{name} --id <client> --keyring <keyring>",
                "Note": "Consume in-cluster via a ceph-rbd StorageClass (block PVC); "
                        "raw 'rbd map' needs a cephx key."}

    @classmethod
    def details(cls, base, token, typ, name, tier="ssd"):
        if typ == "s3":
            return cls.s3_details(base, token, name, tier)
        if typ == "cephfs":
            return cls.cephfs_details(base, token, name, tier)
        if typ == "nfs":
            return cls.nfs_details(base, token, name, tier)
        return cls.rbd_details(base, token, name)

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
                if img.get("pool_name") in RBD_POOLS.values():
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


def registry_add(typ, name, creator, tier="ssd"):
    items = load_registry()
    if not any(e["type"] == typ and e["name"] == name for e in items):
        items.append({"type": typ, "name": name, "creator": creator,
                      "tier": tier, "created": int(time.time())})
        save_registry(items)


def find_entry(typ, name):
    return next((e for e in load_registry()
                 if e["type"] == typ and e["name"] == name), None)


def live_entries(ctx):
    """LIVE discovery: list everything that actually exists in Ceph. Admins see
    all; non-admins see only resources the registry attributes to them. Tier is
    derived from the live pool for RBD, else from the registry."""
    reg = {(e["type"], e["name"]): e for e in load_registry()}
    found = []   # (type, name, tier)
    # RBD — tier from the actual pool the image lives in
    try:
        for img in CephAPI.list_rbd_full(ctx["base"], ctx["token"]):
            found.append(("rbd", img.get("name"),
                          RBD_POOL_TIER.get(img.get("pool_name"), "ssd")))
    except Exception:  # noqa: BLE001
        pass
    for typ, lister in (("s3", CephAPI.list_s3), ("nfs", CephAPI.list_nfs),
                        ("cephfs", CephAPI.list_cephfs)):
        try:
            names = lister(ctx["base"], ctx["token"])
        except Exception:  # noqa: BLE001
            names = []
        for name in names:
            e = reg.get((typ, name))
            found.append((typ, name, (e.get("tier", "ssd") if e else "—")))
    out = []
    for typ, name, tier in found:
        e = reg.get((typ, name))
        if ctx["is_admin"]:
            owner = e["creator"] if e else "(external)"
        elif e and e.get("creator") == ctx["user"]:
            owner = ctx["user"]
        else:
            continue
        out.append({"type": typ, "name": name, "tier": tier,
                    "tierlabel": TIER_LABEL.get(tier, tier), "creator": owner})
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
    tier = (request.form.get("tier", "ssd") or "ssd").lower()
    if tier not in TIERS:
        tier = "ssd"
    try:
        fn = {"s3": CephAPI.provision_s3, "nfs": CephAPI.provision_nfs,
              "cephfs": CephAPI.provision_cephfs, "rbd": CephAPI.provision_rbd}[ptype]
        result = fn(ctx["base"], ctx["token"], name, tier)
        registry_add(ptype, name, ctx["user"], tier)
        return render_index(active_tab="provision", prov_result=result,
                            msg=("ok", f"Provisioned {ptype.upper()} '{name}' ({TIER_LABEL.get(tier, tier)}) on ceph9."))
    except Exception as exc:  # noqa: BLE001
        return render_index(active_tab="provision", msg=("error", f"Provision failed: {exc}"))


# ───────────────────────────── routes: details / browse ──────────────────
@app.route("/details/<typ>/<name>")
def details(typ, name):
    ctx = require_ceph()
    entry = authorize(ctx, typ, name)
    tier = entry.get("tier", "ssd") if entry else "ssd"
    try:
        d = CephAPI.details(ctx["base"], ctx["token"], typ, name, tier)
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
        access, secret, _ = CephAPI.bucket_creds(ctx["base"], ctx["token"], name)
        cli = s3_client_for(access, secret)
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
            # Bucket placement (hdd-placement) already pins the tier; no per-object header.
            access, secret, _ = CephAPI.bucket_creds(ctx["base"], ctx["token"], name)
            s3_client_for(access, secret).upload_fileobj(f, name, secure_filename(f.filename))
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
        access, secret, _ = CephAPI.bucket_creds(ctx["base"], ctx["token"], name)
        obj = s3_client_for(access, secret).get_object(Bucket=name, Key=key)
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
  a.navlink{margin-left:auto;color:#fff;text-decoration:none;font-weight:700;font-size:13px;background:var(--rh-red);padding:8px 16px;border-radius:6px}
  a.navlink:hover{background:var(--rh-red-dark)}
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
<div class="sub">OpenShift <code>ocp-external</code> &rarr; external Ceph <code>ceph9</code></div></div>
<a class="navlink" href="/api-methods">How it works &rarr;</a>
<a class="navlink" href="/architecture" style="margin-left:10px">Architecture &rarr;</a></header>
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
          <div class="field"><label>Tier</label><select name="tier">
            <option value="ssd">Tier0 · SSD (gp3)</option><option value="hdd">Tier1 · HDD (st1)</option></select></div>
          <div class="field"><label>Name</label><input type="text" name="name" placeholder="my-bucket-1" pattern="[a-z0-9][a-z0-9-]{2,40}" required></div>
          <button class="go" type="submit">Provision +</button></form></div>

      <div class="card"><h2>Provisioned endpoints on ceph9 <span class="badge">live</span>
        {% if ceph.is_admin %}<span class="badge">admin: all resources</span>{% endif %}</h2>
        <div class="hint">Discovered live from Ceph (S3 · NFS · CephFS · RBD). Retrieve connection details any time, or browse S3 content.{% if not ceph.is_admin %} You see resources you created.{% endif %}</div>
        {% if entries %}
        <table><tr><th>Type</th><th>Tier</th><th>Name</th><th>Owner</th><th>Actions</th></tr>
          {% for e in entries %}<tr>
            <td><span class="t">{{ e.type|upper }}</span></td>
            <td>{{ e.tierlabel }}</td>
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

@app.route("/architecture")
def architecture():
    return ARCH_HTML


ARCH_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Architecture — Ceph External Storage</title>
<style>
  :root{--rh-red:#ee0000;--rh-red-d:#be0000;--bg:#0f0f0f;--panel:#1b1b1b;--panel2:#242424;
        --line:#3c3c3c;--text:#fff;--muted:#a0a0a0;--ssd:#3b82f6;--hdd:#f0a429;
        --s3:#ee0000;--nfs:#f0a429;--bf:#3fb6a8;--mgmt:#a679f0;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:"Red Hat Display","Red Hat Text",-apple-system,Segoe UI,Roboto,Arial,sans-serif}
  header{background:#000;border-bottom:4px solid var(--rh-red);padding:16px 26px;display:flex;align-items:center;gap:14px}
  .logo{width:42px;height:30px;background:var(--rh-red);border-radius:3px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px}
  header h1{font-size:18px;margin:0}.sub{color:var(--muted);font-size:12px;margin-top:2px}
  a.back{color:#ff6a6a;text-decoration:none;font-weight:700;font-size:13px;margin-left:auto}
  .wrap{max-width:1280px;margin:0 auto;padding:18px 22px 40px}
  .bar{display:flex;flex-wrap:wrap;gap:18px;align-items:center;margin-bottom:16px}
  .toggles{display:flex;gap:8px;flex-wrap:wrap}
  .tg{background:var(--panel2);border:1px solid var(--line);color:#fff;border-radius:20px;padding:7px 14px;font-size:12px;font-weight:700;cursor:pointer;font-family:inherit;display:flex;align-items:center;gap:7px}
  .tg .d{width:9px;height:9px;border-radius:50%;background:#555}
  .tg.on{border-color:#777}.tg.on .d{background:#3fb950}
  .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--muted);margin-left:auto}
  .legend span{display:inline-flex;align-items:center;gap:5px}
  .lz{width:18px;height:0;border-top:3px solid}
  .stage{position:relative}
  #wires{position:absolute;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;overflow:visible}
  .aws{border:1px dashed #555;border-radius:12px;padding:30px 16px 16px;position:relative;z-index:1}
  .aws>.tag{position:absolute;top:-11px;left:16px;background:var(--bg);padding:0 8px;color:var(--muted);font-size:12px;font-weight:700}
  .toprow{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:16px}
  .vpc{border:1px solid var(--line);border-radius:10px;padding:28px 14px 14px;position:relative;background:#151515;flex:1;min-width:320px}
  .vpc>.tag{position:absolute;top:-10px;left:14px;background:#151515;padding:0 8px;font-size:12px;font-weight:700}
  .vpc .meta{color:var(--muted);font-size:11px;margin:-4px 0 12px}
  .box{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:12px;cursor:pointer;transition:border-color .15s,transform .1s}
  .box:hover{border-color:#888;transform:translateY(-1px)}
  .box.sel{border-color:var(--rh-red);box-shadow:0 0 0 1px var(--rh-red) inset}
  .box .t{font-weight:700;font-size:14px;display:flex;align-items:center;gap:8px;justify-content:space-between}
  .box .ip{color:var(--muted);font-size:11px;font-family:ui-monospace,monospace}
  .chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}
  .chip{font-size:10px;padding:2px 7px;border-radius:10px;background:#2c2c2c;color:#ddd;border:1px solid #3a3a3a}
  .chip.ha{background:#10240f;border-color:#1f7a33;color:#9be8ab}
  .disks{display:flex;gap:5px;margin-top:9px}
  .disk{font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700;color:#000}
  .disk.ssd{background:#2c2c2c;color:#9bc2ff;border:1px solid #2c4a7a}
  .disk.hdd{background:#2c2c2c;color:#f0c069;border:1px solid #7a5a1f}
  .stage.tiers .disk.ssd{background:var(--ssd);color:#04284f}
  .stage.tiers .disk.hdd{background:var(--hdd);color:#412402}
  .lbrow{display:flex;gap:16px;justify-content:center;margin:0 0 16px}
  .nlb{border:1px solid var(--rh-red);background:#1c1212;border-radius:8px;padding:11px 16px;cursor:pointer;min-width:230px;text-align:center}
  .nlb .t{font-weight:700;font-size:13px;color:#ff8a8a}.nlb .s{color:var(--muted);font-size:11px;margin-top:2px}
  .nlb .tag2{font-size:9px;background:var(--rh-red);color:#fff;padding:1px 7px;border-radius:9px;margin-left:6px}
  .cephvpc>.tag{color:#ff8a8a}
  .nodes{display:flex;gap:14px;flex-wrap:wrap}
  .node{flex:1;min-width:230px}
  .ha-badge{display:none;font-size:9px;background:#1f7a33;color:#fff;padding:1px 6px;border-radius:8px}
  .stage.ha .ha-badge{display:inline-block}
  .stage.ha .box .chip.ha,.stage.ha .nlb{box-shadow:0 0 0 1px #3fb950 inset}
  .edge{fill:none;stroke-width:2.5;opacity:0;transition:opacity .3s}
  .stage.flows .edge{opacity:.9;stroke-dasharray:7 6;animation:dash 1s linear infinite}
  @keyframes dash{to{stroke-dashoffset:-26}}
  .edge-s3{stroke:var(--s3)}.edge-nfs{stroke:var(--nfs)}.edge-bf{stroke:var(--bf)}.edge-mgmt{stroke:var(--mgmt)}
  .panel{margin-top:18px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;min-height:90px}
  .panel h3{margin:0 0 8px;font-size:15px;color:#ff8a8a}
  .panel p{margin:0;color:#ddd;font-size:13px;line-height:1.6}
  .panel .hint{color:var(--muted);font-size:12px}
  .foot{color:var(--muted);font-size:11px;text-align:center;margin-top:18px}
  code{background:#000;padding:1px 5px;border-radius:4px;color:#ff9b9b;font-size:.92em}
</style></head><body>
<header><div class="logo">RH</div>
  <div><h1>Ceph External Storage — Architecture</h1>
  <div class="sub">AWS · Red Hat Ceph Storage 9 · two OpenShift 4.21 clusters · storage tiers &amp; HA</div></div>
  <a class="back" href="/api-methods" style="margin-left:auto">How it works</a>
  <a class="back" href="/?tab=provision" style="margin-left:18px">&larr; back to portal</a>
</header>
<div class="wrap">
  <div class="bar">
    <div class="toggles">
      <button class="tg on" id="t-flows"><span class="d"></span>Data flows</button>
      <button class="tg on" id="t-ha"><span class="d"></span>HA components</button>
      <button class="tg on" id="t-tiers"><span class="d"></span>Storage tiers</button>
    </div>
    <div class="legend">
      <span><span class="lz" style="border-color:var(--s3)"></span>S3</span>
      <span><span class="lz" style="border-color:var(--nfs)"></span>NFS</span>
      <span><span class="lz" style="border-color:var(--bf)"></span>RBD/CephFS</span>
      <span><span class="lz" style="border-color:var(--mgmt)"></span>provisioning API</span>
      <span><span class="lz" style="border-color:var(--ssd);border-top-style:solid"></span>SSD (gp3)</span>
      <span><span class="lz" style="border-color:var(--hdd)"></span>HDD (st1)</span>
    </div>
  </div>

  <div class="stage flows ha tiers" id="stage">
    <svg id="wires"></svg>
    <div class="aws"><span class="tag">AWS · us-east-2</span>

      <div class="toprow">
        <div class="vpc" style="max-width:430px"><span class="tag">ocp-internal</span>
          <div class="meta">OCP 4.21 · VPC 10.21.0.0/16</div>
          <div class="box" data-comp="ocp-internal" id="ocp-internal">
            <div class="t">ODF — internal mode</div>
            <div class="chips"><span class="chip">self-contained Ceph in OCP</span><span class="chip">OSDs on gp3</span>
              <span class="chip">standalone</span></div>
            <div class="meta" style="margin:9px 0 0">Not connected to ceph9 — independent cluster.</div>
          </div>
        </div>
        <div class="vpc"><span class="tag">ocp-external</span>
          <div class="meta">OCP 4.21 · VPC 10.22.0.0/16 · peered to ceph9</div>
          <div class="box" data-comp="webapp" id="webapp">
            <div class="t">Storage portal (this app) <span class="ha-badge">consumes ceph9</span></div>
            <div class="chips"><span class="chip">ODF external mode</span><span class="chip">consume + provision</span>
              <span class="chip">S3 · NFS · CephFS · RBD</span></div>
            <div class="meta" style="margin:9px 0 0">Talks to ceph9 via the NLBs, mon quorum and mgr API.</div>
          </div>
        </div>
      </div>

      <div class="vpc cephvpc"><span class="tag">ceph9 — Red Hat Ceph Storage 9 (cephadm)</span>
        <div class="meta">VPC 10.20.0.0/16 · 4× m5.2xlarge + bastion/NAT</div>

        <div class="lbrow">
          <div class="nlb" data-comp="rgw-nlb" id="rgw-nlb">
            <div class="t">RGW NLB <span class="tag2">LB · we added</span></div>
            <div class="s">internal NLB · TCP 8080 · seamless failover</div>
          </div>
          <div class="nlb" data-comp="nfs-nlb" id="nfs-nlb">
            <div class="t">NFS NLB <span class="tag2">LB · we added</span></div>
            <div class="s">internal NLB · TCP 2049 · HA failover</div>
          </div>
        </div>

        <div class="nodes">
          <div class="node box" data-comp="ceph01" id="ceph01">
            <div class="t">ceph01 <span class="ip">10.20.2.11</span></div>
            <div class="chips"><span class="chip">_admin</span><span class="chip ha">MON quorum</span>
              <span class="chip ha">MGR active</span><span class="chip ha">MDS active</span><span class="chip">OSD</span></div>
            <div class="disks"><span class="disk ssd">SSD</span><span class="disk ssd">SSD</span><span class="disk hdd">HDD</span></div>
          </div>
          <div class="node box" data-comp="ceph02" id="ceph02">
            <div class="t">ceph02 <span class="ip">10.20.2.12</span></div>
            <div class="chips"><span class="chip ha">MON quorum</span><span class="chip ha">MGR standby</span>
              <span class="chip">OSD</span><span class="chip ha">RGW</span></div>
            <div class="disks"><span class="disk ssd">SSD</span><span class="disk ssd">SSD</span><span class="disk hdd">HDD</span></div>
          </div>
          <div class="node box" data-comp="ceph03" id="ceph03">
            <div class="t">ceph03 <span class="ip">10.20.2.13</span></div>
            <div class="chips"><span class="chip ha">MON quorum</span><span class="chip">OSD</span>
              <span class="chip ha">RGW</span><span class="chip ha">NFS</span></div>
            <div class="disks"><span class="disk ssd">SSD</span><span class="disk ssd">SSD</span><span class="disk hdd">HDD</span></div>
          </div>
          <div class="node box" data-comp="ceph04" id="ceph04">
            <div class="t">ceph04 <span class="ip">10.20.2.14</span></div>
            <div class="chips"><span class="chip">OSD</span><span class="chip ha">MDS standby</span><span class="chip ha">NFS</span></div>
            <div class="disks"><span class="disk ssd">SSD</span><span class="disk ssd">SSD</span><span class="disk hdd">HDD</span></div>
          </div>
        </div>
        <div class="meta" style="margin-top:12px">
          Tier0 (ssd): pools <code>rbd</code> · <code>cephfs…data</code> · <code>rgw…data</code> &nbsp;|&nbsp;
          Tier1 (hdd): <code>rbd-hdd</code> · <code>cephfs…data.hdd</code> · <code>hdd-placement</code> &nbsp;|&nbsp;
          replica ×3 · 8 ssd + 4 hdd OSDs
        </div>
      </div>
    </div>
  </div>

  <div class="panel" id="panel">
    <h3>Click any component</h3>
    <p class="hint">Select a node, gateway, load balancer or cluster to see what it is and its role in HA &amp; tiering. Use the toggles to highlight data flows, HA components and storage tiers.</p>
  </div>
  <div class="foot">Block &amp; file &amp; object all served from ceph9 · RBD/CephFS use the mon quorum directly (native HA) · RGW &amp; NFS sit behind internal AWS NLBs.</div>
</div>
<script>
const INFO={
 "ocp-internal":["ocp-internal — ODF internal mode","OpenShift 4.21 in its own VPC (10.21.0.0/16). Runs ODF in INTERNAL mode: a self-contained Ceph deployed inside OpenShift, with OSDs on the cluster's own gp3 EBS. It is standalone and does NOT connect to the external ceph9 cluster — it's the contrast case to external mode."],
 "webapp":["Storage portal (ocp-external)","The Red Hat-themed portal you're using. Runs on ocp-external (ODF external mode). It both CONSUMES ceph9 storage (mounted PVCs / OBC bucket) and SELF-SERVICE PROVISIONS S3, NFS, CephFS and RBD via the Ceph REST API. It reaches ceph9 over VPC peering: S3 and NFS through the internal NLBs, RBD/CephFS through the mon quorum, and provisioning through the mgr API (:8443)."],
 "rgw-nlb":["RGW NLB — internal load balancer (we added)","An internal AWS Network Load Balancer (TCP 8080) in front of the two RGW S3 gateways (ceph02, ceph03). Health-checked with seamless failover because RGW is stateless. We added this deliberately — Ceph's native 'ingress' uses a keepalived VIP, which cannot float inside an AWS VPC, so an internal NLB is the AWS-native equivalent."],
 "nfs-nlb":["NFS NLB — internal load balancer (we added)","An internal AWS NLB (TCP 2049) in front of the two NFS-Ganesha gateways (ceph03, ceph04). Provides health-checked failover; because NFS is stateful, failover is recover-with-pause (NFSv4 grace reclaim) rather than fully seamless. Deliberately added (same VPC/keepalived reason as the RGW NLB)."],
 "ceph01":["ceph01 — m5.2xlarge (10.20.2.11)","Admin node. Hosts a MON (part of the 3-way quorum), the ACTIVE MGR, the ACTIVE MDS, and OSDs. Disks: 2× gp3 SSD (Tier0) + 1× st1 HDD (Tier1). RBD/CephFS clients and the provisioning API talk here."],
 "ceph02":["ceph02 — m5.2xlarge (10.20.2.12)","MON (quorum), STANDBY MGR, OSDs, and an RGW S3 gateway (behind the RGW NLB). Disks: 2× gp3 SSD + 1× st1 HDD."],
 "ceph03":["ceph03 — m5.2xlarge (10.20.2.13)","MON (quorum), OSDs, an RGW S3 gateway and an NFS-Ganesha gateway (both behind their NLBs). Disks: 2× gp3 SSD + 1× st1 HDD."],
 "ceph04":["ceph04 — m5.2xlarge (10.20.2.14)","OSDs, the STANDBY MDS, and an NFS-Ganesha gateway (behind the NFS NLB). Disks: 2× gp3 SSD + 1× st1 HDD."],
};
const EDGES=[
 ["webapp","rgw-nlb","s3"],["rgw-nlb","ceph02","s3"],["rgw-nlb","ceph03","s3"],
 ["webapp","nfs-nlb","nfs"],["nfs-nlb","ceph03","nfs"],["nfs-nlb","ceph04","nfs"],
 ["webapp","ceph02","bf"],["webapp","ceph01","mgmt"],
];
const stage=document.getElementById("stage"), svg=document.getElementById("wires");
function pt(el){const r=el.getBoundingClientRect(),s=stage.getBoundingClientRect();
 return {cx:r.left-s.left+r.width/2, top:r.top-s.top, bot:r.top-s.top+r.height};}
function draw(){
 svg.innerHTML="";
 for(const [a,b,t] of EDGES){
  const ea=document.getElementById(a), eb=document.getElementById(b); if(!ea||!eb)continue;
  const A=pt(ea),B=pt(eb); const downward=B.top>=A.bot;
  const y1=downward?A.bot:A.top, y2=downward?B.top:B.bot;
  const my=(y1+y2)/2;
  const p=document.createElementNS("http://www.w3.org/2000/svg","path");
  p.setAttribute("d",`M ${A.cx} ${y1} C ${A.cx} ${my}, ${B.cx} ${my}, ${B.cx} ${y2}`);
  p.setAttribute("class","edge edge-"+t);
  svg.appendChild(p);
 }
}
function tog(id,cls){const b=document.getElementById(id);b.addEventListener("click",()=>{b.classList.toggle("on");stage.classList.toggle(cls);});}
tog("t-flows","flows");tog("t-ha","ha");tog("t-tiers","tiers");
let sel=null;
document.querySelectorAll("[data-comp]").forEach(el=>el.addEventListener("click",()=>{
 const k=el.getAttribute("data-comp"); if(!INFO[k])return;
 if(sel)sel.classList.remove("sel"); el.classList.add("sel"); sel=el;
 document.getElementById("panel").innerHTML="<h3>"+INFO[k][0]+"</h3><p>"+INFO[k][1]+"</p>";
}));
window.addEventListener("resize",draw); window.addEventListener("load",draw); setTimeout(draw,120);
</script></body></html>
"""

@app.route("/api-methods")
def api_methods():
    return APIMETHODS_HTML


APIMETHODS_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How it works — Ceph API methods</title>
<style>
  :root{--rh-red:#ee0000;--rh-red-d:#be0000;--bg:#0f0f0f;--panel:#1b1b1b;--panel2:#242424;
        --line:#3c3c3c;--text:#fff;--muted:#a0a0a0;--ctl:#a679f0;--data:#3fb6a8;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:"Red Hat Display","Red Hat Text",-apple-system,Segoe UI,Roboto,Arial,sans-serif}
  header{background:#000;border-bottom:4px solid var(--rh-red);padding:16px 26px;display:flex;align-items:center;gap:14px}
  .logo{width:42px;height:30px;background:var(--rh-red);border-radius:3px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px}
  header h1{font-size:18px;margin:0}.sub{color:var(--muted);font-size:12px;margin-top:2px}
  a.back{color:#ff6a6a;text-decoration:none;font-weight:700;font-size:13px}
  .wrap{max-width:1280px;margin:0 auto;padding:18px 22px 40px}
  .intro{color:#ddd;font-size:13px;line-height:1.6;margin:6px 0 16px;max-width:880px}
  .intro b{color:#fff}
  .bar{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin-bottom:18px}
  .toggles{display:flex;gap:8px;flex-wrap:wrap}
  .tg{background:var(--panel2);border:1px solid var(--line);color:#fff;border-radius:20px;padding:7px 14px;font-size:12px;font-weight:700;cursor:pointer;font-family:inherit;display:flex;align-items:center;gap:7px}
  .tg .d{width:9px;height:9px;border-radius:50%;background:#555}
  .tg.on .d{background:#3fb950}
  .tg.ctl.on{border-color:var(--ctl)}.tg.data.on{border-color:var(--data)}
  .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--muted);margin-left:auto;align-items:center}
  .m{font-weight:800;font-size:10px;padding:2px 7px;border-radius:5px;font-family:ui-monospace,Menlo,monospace}
  .m.get{background:#0d2c12;color:#9be8ab;border:1px solid #1f7a33}
  .m.post{background:#2c0d0d;color:#ff9b9b;border:1px solid #7a1f1f}
  .m.s3{background:#2a210a;color:#f0c069;border:1px solid #7a5a1f}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:18px}
  .card2{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;cursor:pointer;transition:border-color .15s,transform .1s}
  .card2:hover{border-color:#888;transform:translateY(-1px)}
  .card2.sel{border-color:var(--rh-red);box-shadow:0 0 0 1px var(--rh-red) inset}
  .card2 .h{padding:13px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:8px}
  .card2 .h .t{font-weight:700;font-size:14px}
  .card2 .h .tag{font-size:10px;padding:2px 8px;border-radius:10px;background:#2c2c2c;color:#ddd;border:1px solid #3a3a3a;white-space:nowrap}
  .card2 ul{list-style:none;margin:0;padding:8px}
  .card2 li{display:grid;grid-template-columns:auto 1fr auto;gap:5px 9px;align-items:center;padding:8px;border-radius:6px}
  .card2 li:hover{background:var(--panel2)}
  .path{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#fff;word-break:break-all}
  .via{font-size:10px;color:var(--muted);border:1px solid var(--line);border-radius:9px;padding:1px 7px;white-space:nowrap}
  .via.ctl{color:#c9b3f7;border-color:#574a72}.via.data{color:#9fe0d6;border-color:#356158}
  .desc{grid-column:1 / -1;color:var(--muted);font-size:12px;margin:0}
  .stage.no-ctl li[data-lane=ctl]{opacity:.22}
  .stage.no-data li[data-lane=data]{opacity:.22}
  .panel{margin-top:20px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;min-height:84px}
  .panel h3{margin:0 0 8px;font-size:15px;color:#ff8a8a}
  .panel p{margin:0;color:#ddd;font-size:13px;line-height:1.65}
  .panel .hint{color:var(--muted);font-size:12px}
  .foot{color:var(--muted);font-size:11px;text-align:center;margin-top:20px}
  code{background:#000;padding:1px 5px;border-radius:4px;color:#ff9b9b;font-size:.92em}
</style></head><body>
<header><div class="logo">RH</div>
  <div><h1>How it works — Ceph API methods</h1>
  <div class="sub">What the portal actually calls on <code>ceph9</code> under the hood</div></div>
  <a class="back" href="/architecture" style="margin-left:auto">Architecture</a>
  <a class="back" href="/?tab=provision" style="margin-left:18px">&larr; back to portal</a>
</header>
<div class="wrap">
  <p class="intro">Every action in the portal maps to a real API call on the external Ceph cluster. Control-plane
  actions (create/list) go to the <b>Ceph Manager REST API</b> over HTTPS on <code>:8443</code> with a per-session
  bearer token — Ceph's own RBAC decides what you can do. Data-plane actions (objects, mounts) flow through the
  <b>internal NLBs</b> and ODF's CSI drivers. Click a card for the why.</p>

  <div class="bar">
    <div class="toggles">
      <button class="tg ctl on" id="t-ctl"><span class="d"></span>Control plane · mgr REST API</button>
      <button class="tg data on" id="t-data"><span class="d"></span>Data plane · S3 / NFS / RBD</button>
    </div>
    <div class="legend">
      <span><span class="m get">GET</span>&nbsp;read</span>
      <span><span class="m post">POST</span>&nbsp;create</span>
      <span><span class="m s3">S3</span>&nbsp;object op</span>
    </div>
  </div>

  <div class="stage" id="stage">
  <div class="grid">

    <div class="card2" data-comp="connect">
      <div class="h"><span class="t">1 · Connect &amp; authenticate</span><span class="tag">auth</span></div>
      <ul>
        <li data-lane="ctl"><span class="m post">POST</span><span class="path">/api/auth</span><span class="via ctl">mgr API</span>
          <p class="desc">Exchange your Ceph username + password for a short-lived bearer token used by every later call.</p></li>
      </ul>
    </div>

    <div class="card2" data-comp="s3">
      <div class="h"><span class="t">Provision · S3 bucket</span><span class="tag">object</span></div>
      <ul>
        <li data-lane="ctl"><span class="m post">POST</span><span class="path">/api/rgw/user</span><span class="via ctl">mgr API</span>
          <p class="desc">Create an RGW user and return its S3 access / secret keys.</p></li>
        <li data-lane="data"><span class="m s3">S3</span><span class="path">CreateBucket</span><span class="via data">RGW NLB</span>
          <p class="desc">Create the bucket on the RGW gateway; Tier1 pins it to <code>:hdd-placement</code>.</p></li>
      </ul>
    </div>

    <div class="card2" data-comp="nfs">
      <div class="h"><span class="t">Provision · NFS export</span><span class="tag">file</span></div>
      <ul>
        <li data-lane="ctl"><span class="m post">POST</span><span class="path">/api/cephfs/subvolume</span><span class="via ctl">mgr API</span>
          <p class="desc">Tier1 only — carve a CephFS subvolume on the HDD data pool.</p></li>
        <li data-lane="ctl"><span class="m get">GET</span><span class="path">/api/cephfs/subvolume/{fs}/info</span><span class="via ctl">mgr API</span>
          <p class="desc">Resolve the subvolume's on-disk path to export.</p></li>
        <li data-lane="ctl"><span class="m post">POST</span><span class="path">/api/nfs-ganesha/export</span><span class="via ctl">mgr API</span>
          <p class="desc">Publish an NFSv4 export (pseudo <code>/name</code>) on the <code>nfslab</code> cluster.</p></li>
      </ul>
    </div>

    <div class="card2" data-comp="cephfs">
      <div class="h"><span class="t">Provision · CephFS subvolume</span><span class="tag">file</span></div>
      <ul>
        <li data-lane="ctl"><span class="m post">POST</span><span class="path">/api/cephfs/subvolume</span><span class="via ctl">mgr API</span>
          <p class="desc">Create a subvolume; Tier1 sets <code>pool_layout</code> to the HDD data pool.</p></li>
      </ul>
    </div>

    <div class="card2" data-comp="rbd">
      <div class="h"><span class="t">Provision · RBD image</span><span class="tag">block</span></div>
      <ul>
        <li data-lane="ctl"><span class="m post">POST</span><span class="path">/api/block/image</span><span class="via ctl">mgr API</span>
          <p class="desc">Create a block image in pool <code>rbd</code> (Tier0) or <code>rbd-hdd</code> (Tier1).</p></li>
      </ul>
    </div>

    <div class="card2" data-comp="discover">
      <div class="h"><span class="t">Live discovery &amp; details</span><span class="tag">read-only</span></div>
      <ul>
        <li data-lane="ctl"><span class="m get">GET</span><span class="path">/api/rgw/bucket · /api/rgw/user/{uid}</span><span class="via ctl">mgr API</span>
          <p class="desc">List buckets &amp; resolve owners / S3 keys.</p></li>
        <li data-lane="ctl"><span class="m get">GET</span><span class="path">/api/nfs-ganesha/export</span><span class="via ctl">mgr API</span>
          <p class="desc">List NFS exports and their backing paths.</p></li>
        <li data-lane="ctl"><span class="m get">GET</span><span class="path">/api/block/image · /api/cephfs/subvolume/{fs}/info</span><span class="via ctl">mgr API</span>
          <p class="desc">List RBD images &amp; CephFS subvolumes; read each one's tier back live.</p></li>
      </ul>
    </div>

    <div class="card2" data-comp="consume">
      <div class="h"><span class="t">Consume mounted storage</span><span class="tag">data path</span></div>
      <ul>
        <li data-lane="data"><span class="m s3">S3</span><span class="path">ListObjects · PutObject · GetObject</span><span class="via data">RGW NLB</span>
          <p class="desc">Browse / upload / download bucket objects (API-accessed, never mounted).</p></li>
        <li data-lane="data"><span class="m s3">PVC</span><span class="path">Block (RBD) &amp; File (CephFS)</span><span class="via data">ODF CSI</span>
          <p class="desc">Mounted as Kubernetes PVCs that ODF's external CSI drivers bind against ceph9.</p></li>
      </ul>
    </div>

  </div>
  </div>

  <div class="panel" id="panel">
    <h3>Click any card</h3>
    <p class="hint">See the why behind each set of calls. Use the toggles above to highlight control-plane (mgr API) vs data-plane (S3 / NFS / RBD) traffic.</p>
  </div>
  <div class="foot">All calls target the external Ceph cluster (ceph9) · control plane = mgr REST API (:8443) · data plane = internal NLBs + ODF CSI · nothing is cached, everything is re-read live.</div>
</div>
<script>
const INFO={
 "connect":["1 · Connect & authenticate","The portal holds no Ceph credentials. Each session it POSTs your username/password to the mgr API (https://<mon>:8443/api/auth), trying each monitor until one answers, and keeps only the returned bearer token. Every later call carries that token, so Ceph's own RBAC decides what you can see and do — admins see everything, users see only what they created."],
 "s3":["Provision · S3 bucket (object)","Object storage is a two-step: the RGW user (and its S3 keys) is created through the mgr API, then the bucket itself is created with a real S3 call to the RGW gateway through the NLB — which is where the Tier1 'hdd-placement' target gets selected. The portal then reads the bucket's live placement back to confirm its tier."],
 "nfs":["Provision · NFS export (file)","For Tier1 a CephFS subvolume is carved on the HDD data pool first, then its path is resolved; finally an NFSv4 export is published on the 'nfslab' Ganesha cluster. Clients mount it through the internal NFS NLB on :2049, so a gateway failover is transparent to the mount target."],
 "cephfs":["Provision · CephFS subvolume (file)","A CephFS subvolume is a slice of the shared file system. Tier1 sets pool_layout to the HDD data pool so its bytes land on st1 disks. Consume it in-cluster via the cephfs StorageClass (RWX PVC), or re-export it over NFS for external clients."],
 "rbd":["Provision · RBD image (block)","A block image is created in the rbd pool (Tier0) or the rbd-hdd pool (Tier1). It's consumed in-cluster as a block PVC (RWO) through ODF's ceph-rbd StorageClass; a raw 'rbd map' would need a cephx key."],
 "discover":["Live discovery & details (read-only)","Nothing is cached — the resource list and every 'details' view are re-read live from Ceph each time. A user without rights to a resource type simply gets a 403, which the portal treats as 'hide it', so users see only what they own while admins see all. Each item's tier is read back from its actual placement rule / pool, not remembered from creation."],
 "consume":["Consume mounted storage","Two different models side by side: object storage is API-accessed (S3 list/put/get straight to the RGW NLB, never mounted), while block and file are genuine Kubernetes PVCs that ODF's external CSI drivers bind against ceph9 — the app just reads and writes the mountpoint."],
};
const stage=document.getElementById("stage");
function tog(id,cls){const b=document.getElementById(id);b.addEventListener("click",()=>{b.classList.toggle("on");stage.classList.toggle(cls,!b.classList.contains("on"));});}
tog("t-ctl","no-ctl");tog("t-data","no-data");
let sel=null;
document.querySelectorAll("[data-comp]").forEach(el=>el.addEventListener("click",()=>{
 const k=el.getAttribute("data-comp"); if(!INFO[k])return;
 if(sel)sel.classList.remove("sel"); el.classList.add("sel"); sel=el;
 document.getElementById("panel").innerHTML="<h3>"+INFO[k][0]+"</h3><p>"+INFO[k][1]+"</p>";
 document.getElementById("panel").scrollIntoView({behavior:"smooth",block:"nearest"});
}));
</script></body></html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
