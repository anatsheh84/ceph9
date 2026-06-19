#!/usr/bin/env python3
"""
Ceph External Storage Demo
==========================
A small Flask app that demonstrates an OpenShift cluster (ocp-external) storing
and retrieving files from an EXTERNAL Ceph cluster (ceph9) across all three
storage endpoints:

  * Block  (RBD)    -> a ReadWriteOnce PVC mounted at $BLOCK_DIR
  * File   (CephFS) -> a ReadWriteMany PVC mounted at $FILE_DIR
  * Object (RGW S3) -> an S3 bucket provisioned by an ObjectBucketClaim

Object config (bucket name/host/port + creds) is injected by the OBC via
envFrom (ConfigMap + Secret). Block/File are plain mounted filesystems.
"""
import io
import os

import boto3
from botocore.client import Config
from flask import (Flask, abort, flash, redirect, render_template_string,
                   request, send_file, url_for)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ceph-external-demo")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512 MiB uploads

BLOCK_DIR = os.environ.get("BLOCK_DIR", "/data/block")
FILE_DIR = os.environ.get("FILE_DIR", "/data/file")
for _d in (BLOCK_DIR, FILE_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass

# Object storage (RGW) — injected by the ObjectBucketClaim.
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


def object_ready():
    return all([BUCKET_NAME, BUCKET_HOST, S3_KEY, S3_SECRET])


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=f"http://{BUCKET_HOST}:{BUCKET_PORT}",
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )


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
    except Exception as exc:  # noqa: BLE001 - surface any backend error in UI
        return {"error": str(exc), "items": []}
    return {"error": None, "items": out}


@app.route("/")
def index():
    listings = {ep: list_files(ep) for ep in ENDPOINTS}
    return render_template_string(
        PAGE, endpoints=ENDPOINTS, listings=listings,
        object_ready=object_ready(), bucket=BUCKET_NAME or "(not provisioned)",
        node=os.environ.get("NODE_NAME", ""))


@app.route("/upload", methods=["POST"])
def upload():
    ep = request.form.get("endpoint", "")
    if ep not in ENDPOINTS:
        flash("error", "Unknown storage endpoint.")
        return redirect(url_for("index"))
    f = request.files.get("file")
    if not f or f.filename == "":
        flash("error", "Please choose a file to upload.")
        return redirect(url_for("index"))
    name = secure_filename(f.filename)
    info = ENDPOINTS[ep]
    try:
        if info["kind"] == "fs":
            f.save(os.path.join(info["dir"], name))
        else:
            if not object_ready():
                raise RuntimeError("Object endpoint not configured (OBC not bound).")
            s3_client().upload_fileobj(f, BUCKET_NAME, name)
        flash("ok", f"Uploaded '{name}' to {info['label']} ({info['tech']}) on ceph9.")
    except Exception as exc:  # noqa: BLE001
        flash("error", f"Upload to {info['label']} failed: {exc}")
    return redirect(url_for("index"))


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


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ceph External Storage Demo</title>
<style>
  :root{
    --rh-red:#ee0000; --rh-red-dark:#be0000;
    --bg:#0f0f0f; --panel:#1b1b1b; --panel-2:#242424;
    --line:#3c3c3c; --text:#ffffff; --muted:#a0a0a0;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:"Red Hat Display","Red Hat Text",-apple-system,Segoe UI,Roboto,Arial,sans-serif;}
  header{background:#000;border-bottom:4px solid var(--rh-red);padding:18px 28px;
         display:flex;align-items:center;gap:16px;}
  .logo{width:46px;height:32px;background:var(--rh-red);border-radius:3px;
        display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;
        font-size:13px;letter-spacing:.5px;}
  header h1{font-size:20px;margin:0;font-weight:700}
  header .sub{color:var(--muted);font-size:13px;margin-top:2px}
  .wrap{max-width:1100px;margin:0 auto;padding:28px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:22px;margin-bottom:24px}
  .card h2{margin:0 0 16px;font-size:16px;font-weight:700}
  form.up{display:flex;flex-wrap:wrap;gap:14px;align-items:end}
  .field{display:flex;flex-direction:column;gap:6px}
  label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  select,input[type=file]{background:var(--panel-2);color:var(--text);border:1px solid var(--line);
        border-radius:6px;padding:10px 12px;font-size:14px;min-width:220px}
  input[type=file]::file-selector-button{background:#333;color:#fff;border:0;border-radius:5px;
        padding:7px 12px;margin-right:10px;cursor:pointer}
  button{background:var(--rh-red);color:#fff;border:0;border-radius:6px;padding:11px 26px;
         font-size:14px;font-weight:700;cursor:pointer;transition:background .15s}
  button:hover{background:var(--rh-red-dark)}
  .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
  .ep{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .ep .head{padding:16px 18px;border-bottom:1px solid var(--line);
            display:flex;justify-content:space-between;align-items:center}
  .ep .head .t{font-weight:700;font-size:15px}
  .ep .head .tag{font-size:11px;color:#fff;background:var(--rh-red);padding:2px 8px;border-radius:10px}
  .ep .blurb{color:var(--muted);font-size:12px;padding:10px 18px 0}
  .ep ul{list-style:none;margin:8px 0 0;padding:8px 10px;max-height:320px;overflow:auto}
  .ep li{display:flex;justify-content:space-between;align-items:center;gap:10px;
         padding:9px 10px;border-radius:6px}
  .ep li:hover{background:var(--panel-2)}
  .ep li .nm{font-size:13px;word-break:break-all}
  .ep li .sz{color:var(--muted);font-size:11px;white-space:nowrap}
  .ep a.dl{color:#ff5a5a;text-decoration:none;font-size:12px;font-weight:700;white-space:nowrap}
  .ep a.dl:hover{text-decoration:underline}
  .empty{color:var(--muted);font-size:13px;padding:14px 18px}
  .err{color:#ff8080;font-size:12px;padding:10px 18px}
  .count{color:var(--muted);font-size:12px}
  .flash{padding:12px 16px;border-radius:6px;margin-bottom:18px;font-size:14px}
  .flash.ok{background:#0d2c12;border:1px solid #1f7a33;color:#9be8ab}
  .flash.error{background:#2c0d0d;border:1px solid #7a1f1f;color:#ffb0b0}
  .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px}
  .pill.on{background:#1f7a33;color:#fff}.pill.off{background:#555;color:#ddd}
  footer{color:var(--muted);font-size:12px;text-align:center;padding:20px}
  code{background:#000;padding:1px 6px;border-radius:4px;color:#ff8080}
</style>
</head>
<body>
<header>
  <div class="logo">RH</div>
  <div>
    <h1>Ceph External Storage Demo</h1>
    <div class="sub">OpenShift <code>ocp-external</code> &rarr; external Ceph <code>ceph9</code> &nbsp;•&nbsp; block · file · object</div>
  </div>
</header>

<div class="wrap">
  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat, msg in msgs %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}
  {% endwith %}

  <div class="card">
    <h2>Upload a file to Ceph</h2>
    <form class="up" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
      <div class="field">
        <label for="file">File</label>
        <input type="file" name="file" id="file" required>
      </div>
      <div class="field">
        <label for="endpoint">Storage endpoint</label>
        <select name="endpoint" id="endpoint">
          <option value="block">Block — Ceph RBD (RWO PVC)</option>
          <option value="file">File — CephFS (RWX PVC)</option>
          <option value="object" {{ '' if object_ready else 'disabled' }}>Object — Ceph RGW S3 (bucket)</option>
        </select>
      </div>
      <button type="submit">Upload &uarr;</button>
    </form>
    <div style="margin-top:12px" class="count">
      Object bucket: <code>{{ bucket }}</code>
      <span class="pill {{ 'on' if object_ready else 'off' }}">{{ 'ready' if object_ready else 'pending OBC' }}</span>
    </div>
  </div>

  <div class="grid">
    {% for ep, info in endpoints.items() %}
    <div class="ep">
      <div class="head">
        <span class="t">{{ info.label }}</span>
        <span class="tag">{{ info.tech }}</span>
      </div>
      <div class="blurb">{{ info.blurb }}</div>
      {% set L = listings[ep] %}
      {% if L.error %}
        <div class="err">⚠ {{ L.error }}</div>
      {% elif L['items'] %}
        <ul>
          {% for it in L['items'] %}
          <li>
            <span class="nm">{{ it.name }}</span>
            <span><span class="sz">{{ it.size }}</span>
              &nbsp;<a class="dl" href="{{ url_for('download', ep=ep, name=it.name) }}">download</a></span>
          </li>
          {% endfor %}
        </ul>
        <div class="blurb count" style="padding-bottom:14px">{{ L['items']|length }} file(s)</div>
      {% else %}
        <div class="empty">No files yet.</div>
      {% endif %}
    </div>
    {% endfor %}
  </div>
</div>

<footer>Files written here live on the external Ceph cluster (ceph9){% if node %} • served from node {{ node }}{% endif %}.</footer>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
