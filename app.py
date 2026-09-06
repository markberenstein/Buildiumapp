"""
Outstanding Balances Dashboard — Buildium API

A small Flask app that:
  1. Calls the Buildium API server-side (so your Client ID/Secret never
     reach the browser).
  2. Merges lease outstanding-balance data with rental/unit/property
     names so the dashboard is readable, not just IDs.
  3. Serves a single-page dashboard (static/index.html) that renders it.

Buildium API docs: https://developer.buildium.com/
Auth: every request needs headers
  x-buildium-client-id: <your client id>
  x-buildium-client-secret: <your client secret>
"""

import json
import os
import time
import uuid
from functools import wraps

import requests
from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

BUILDIUM_BASE_URL = "https://api.buildium.com/v1"
CLIENT_ID = os.environ.get("BUILDIUM_CLIENT_ID")
CLIENT_SECRET = os.environ.get("BUILDIUM_CLIENT_SECRET")

# --- Screenshot / testing mode -------------------------------------------
# While you're testing, you can skip the live Buildium hookup entirely:
# log into /admin, upload screenshots of the balances, and every visitor
# to "/" sees those images instead of the live table. The moment the
# manifest is empty again, "/" automatically falls back to the live
# Buildium dashboard below.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.pat
# Property portfolios — each gets its own installable app / URL.
PORTFOLIOS = {
    "ba-partners": {
        "label": "BA Partners",
        "properties": "Rogell Court, Fulton Place",
        "page": "ba-partners.html",
    },
    "ford-managed": {
        "label": "Ford Managed",
        "properties": "Church Street, Pierce Street, Broderick",
        "page": "ford-managed.html",
    },
    "berenstein": {
        "label": "Berenstein Associates",
        "properties": "Teagarden",
        "page": "berenstein.html",
    },
}


def load_manifest():
    if not os.path.exists(MANIFEST_PATH):
        return []
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def save_manifest(entries):
    with open(MANIFEST_PATH, "w") as f:
        json.dump(entries, f, indent=2)


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"error": "Not logged in"}), 401
        return fn(*args, **kwargs)
    return wrapper


def buildium_headers():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "Missing BUILDIUM_CLIENT_ID / BUILDIUM_CLIENT_SECRET environment variables."
        )
    return {
        "x-buildium-client-id": CLIENT_ID,
        "x-buildium-client-secret": CLIENT_SECRET,
        "Accept": "application/json",
    }


def buildium_get(path, params=None):
    resp = requests.get(
        f"{BUILDIUM_BASE_URL}{path}",
        headers=buildium_headers(),
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_all_pages(path, params=None, page_size=200, hard_limit=5000):
    """Buildium paginates with offset/limit. Walk pages until exhausted."""
    params = dict(params or {})
    params["limit"] = page_size
    offset = 0
    out = []
    while True:
        params["offset"] = offset
        batch = buildium_get(path, params)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size or len(out) >= hard_limit:
            break
        offset += page_size
    return out


def require_cache_or_fetch(force=False):
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["fetched_at"]) < CACHE_SECONDS:
        return _cache["data"]

    balances = fetch_all_pages("/leases/outstandingbalances")

    rentals = fetch_all_pages("/rentals")
    rentals_by_id = {r["Id"]: r for r in rentals}

    units_by_id = {}
    for r in rentals:
        for u in r.get("Units", []) or []:
            units_by_id[u["Id"]] = u

    leases = fetch_all_pages("/leases")
    leases_by_id = {l["Id"]: l for l in leases}

    rows = []
    for b in balances:
        lease_id = b.get("LeaseId")
        property_id = b.get("PropertyId")
        unit_id = b.get("UnitId")

        lease = leases_by_id.get(lease_id, {})
        prop = rentals_by_id.get(property_id, {})
        unit = units_by_id.get(unit_id, {})

        tenants = lease.get("CurrentTenants") or lease.get("Tenants") or []
        tenant_names = ", ".join(
            f"{t.get('FirstName', '').strip()} {t.get('LastName', '').strip()}".strip()
            for t in tenants
        ) or "—"

        rows.append(
            {
                "leaseId": lease_id,
                "property": prop.get("Name", f"Property {property_id}"),
                "unit": unit.get("UnitNumber", "—"),
                "tenant": tenant_names,
                "bucket_0_30": b.get("Balance0to30Days", 0) or 0,
                "bucket_31_60": b.get("Balance31to60Days", 0) or 0,
                "bucket_61_90": b.get("Balance61to90Days", 0) or 0,
                "bucket_90_plus": b.get("BalanceOver90Days", 0) or 0,
                "total": b.get("TotalBalance", 0) or 0,
                "noticeGiven": bool(lease.get("IsNoticeGiven")) if lease else False,
                "evictionPending": bool(b.get("EvictionPendingDate")),
            }
        )

    rows.sort(key=lambda r: r["total"], reverse=True)

    result = {
        "generatedAt": int(now),
        "rows": rows,
        "totals": {
            "total": sum(r["total"] for r in rows),
            "bucket_0_30": sum(r["bucket_0_30"] for r in rows),
            "bucket_31_60": sum(r["bucket_31_60"] for r in rows),
            "bucket_61_90": sum(r["bucket_61_90"] for r in rows),
            "bucket_90_plus": sum(r["bucket_90_plus"] for r in rows),
        },
    }

    _cache["data"] = result
    _cache["fetched_at"] = now
    return result


_cache = {"data": None, "fetched_at": 0}
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "120"))


@app.route("/api/balances")
def api_balances():
    force = request.args.get("refresh") == "1"
    try:
        data = require_cache_or_fetch(force=force)
        return jsonify(data)
    except requests.HTTPError as e:
        return jsonify({"error": f"Buildium API error: {e}"}), 502
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    if load_manifest():
        return send_from_directory(app.static_folder, "gallery.html")
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/screenshots")
def api_screenshots():
    entries = sorted(load_manifest(), key=lambda e: e["uploadedAt"], reverse=True)
    return jsonify({"entries": entries})


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    safe = secure_filename(filename)
    return send_from_directory(UPLOAD_FOLDER, safe)


@app.route("/admin")
def admin_page():
    return send_from_directory(app.static_folder, "admin.html")


@app.route("/admin/status")
def admin_status():
    return jsonify({"loggedIn": bool(session.get("is_admin"))})


@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return jsonify({"error": "Set ADMIN_PASSWORD on the server first."}), 500
    body = request.get_json(silent=True) or {}
    if body.get("password") == ADMIN_PASSWORD:
        session["is_admin"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Wrong password"}), 401


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/admin/upload", methods=["POST"])
@require_admin
def admin_upload():
    file = request.files.get("file")
    caption = (request.form.get("caption") or "").strip()
    if not file or file.filename == "":
        return jsonify({"error": "No file provided"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: .{ext}"}), 400

    filename = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(UPLOAD_FOLDER, filename))

    entries = load_manifest()
    entries.append(
        {
            "id": uuid.uuid4().hex,
            "filename": filename,
            "caption": caption,
            "uploadedAt": int(time.time()),
        }
    )
    save_manifest(entries)
    return jsonify({"ok": True, "entries": entries})


@app.route("/admin/delete/<entry_id>", methods=["POST"])
@require_admin
def admin_delete(entry_id):
    entries = load_manifest()
    keep, remove = [], []
    for e in entries:
        (remove if e["id"] == entry_id else keep).append(e)
    for e in remove:
        path = os.path.join(UPLOAD_FOLDER, e["filename"])
        if os.path.exists(path):
            os.remove(path)
    save_manifest(keep)
    return jsonify({"ok": True, "entries": keep})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG") == "1")
