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
import re
import time
import uuid
from functools import wraps

import pytesseract
import requests
from PIL import Image
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
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(os.path.dirname(__file__), "uploads"))
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "data", "manifest.json")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)

# Property portfolios — each gets its own installable app / URL.

PORTFOLIOS = {
    "ba-partners": {
        "label": "BA Partners",
        "properties": "Rogell Court, Fulton Place",
        "page": "ba-partners.html",
        "recipient": "Adam",
    },
    "ford-owed": {
        "label": "Ford Owed",
        "properties": "Church Street",
        "page": "ford-owned.html",
        "recipient": "Ford",
    },
    "steve-jeanne": {
        "label": "Steve and Jeanne Owned",
        "properties": "Pierce Street, Broderick",
        "page": "steve-jeanne.html",
        "recipient": "Steve",
    },
     "berenstein-associates": {
        "label": "Berenstein Associates",
        "properties": "Teagarden",
        "page": "berenstein-associates.html",
        "recipient": "Dad",
    },

    
}

# Keyword -> portfolio mapping, used to auto-assign each parsed row.
PORTFOLIO_KEYWORDS = {
    "ba-partners": ["rogell court", "fulton place"],
    "ford-owed": ["church street"],
    "steve-jeanne": ["pierce street", "broderick"],
    "berenstein-associates": ["teagarden"],
}

BALANCES_PATH = os.path.join(os.path.dirname(__file__), "data", "balances.json")


def portfolio_for_text(text):
    low = text.lower()
    for portfolio, keywords in PORTFOLIO_KEYWORDS.items():
        for kw in keywords:
            if kw in low:
                return portfolio
    return None


def parse_amount(raw):
    """
    Robustly parse a dollar figure the way OCR actually renders it. OCR
    sometimes uses a period instead of a comma before the thousands group
    (e.g. "2.795.00" meaning $2,795.00). This treats the LAST separator +
    2-digit group as the decimal part, and strips any other separators
    as thousands grouping, so both "2,795.00" and "2.795.00" parse the
    same way.
    """
    m = re.match(r'^([\d.,]+?)[.,](\d{2})$', raw)
    if not m:
        return None
    integer_part, decimal_part = m.groups()
    integer_part = re.sub(r'[.,]', '', integer_part)
    if not integer_part.isdigit():
        return None
    return float(f"{integer_part}.{decimal_part}")


def clean_lease_label(rest):
    """
    Turn the raw OCR leftover text for one lease (after the property name
    has already been stripped off) into a plain "Unit: Tenant Name" label.
    Prefers "Apartment #N" when present, otherwise falls back to whatever
    bare unit number(s) appear right after the property name.
    """
    unit_label = None
    apt_match = re.search(r'apartment\s*#?\s*(\d+)', rest, re.IGNORECASE)
    if apt_match:
        unit_label = f"Apartment #{apt_match.group(1)}"
        rest = rest[:apt_match.start()] + rest[apt_match.end():]
    else:
        num_match = re.match(r'\s*[-–—.]*\s*(\d+(?:\s*/\s*\d+)*)', rest)
        if num_match and num_match.group(1):
            unit_label = re.sub(r'\s*/\s*', '/', num_match.group(1))
            rest = rest[num_match.end():]

    tenant = rest
    tenant = tenant.replace('$', ' ')
    tenant = re.sub(r'[~\-_—–|()+»«=¢{}"\u201c\u201d\u2018\u2019]', ' ', tenant)

    tenant = re.sub(r'\b\d{3,}\b', ' ', tenant)
    tenant = re.sub(r'\b\d{1,2}\b', ' ', tenant)
    tenant = re.sub(r'\.(?!\w)', ' ', tenant)
    tenant = re.sub(r',{2,}', ',', tenant)
    tenant = re.sub(r'\s*,\s*', ', ', tenant)
    tenant = re.sub(r'\s+', ' ', tenant).strip(' .,')

    if unit_label:
        return f"{unit_label}: {tenant}"
    return tenant


def parse_balances_screenshot(image_path):
    """
    OCR a Buildium 'outstanding lease balances' screenshot and split it into
    per-lease rows. Property names are used as row boundaries (Buildium's
    LEASE column is always leftmost, so a property name reliably marks
    the start of a new row even when OCR reading order gets jumbled
    within a row). Rows where no cleanly-formatted dollar amount was
    found are flagged needsReview so a human catches misreads before
    anything is published.
    """
    img = Image.open(image_path)
    text = pytesseract.image_to_string(img)

    all_keywords = [kw for kws in PORTFOLIO_KEYWORDS.values() for kw in kws]
    all_keywords_sorted = sorted(all_keywords, key=len, reverse=True)
    pattern = re.compile(r'(' + '|'.join(re.escape(kw) for kw in all_keywords_sorted) + r')', re.IGNORECASE)
    starts = list(pattern.finditer(text))

    rows = []
    for i, m in enumerate(starts):
        row_start = m.start()
        row_end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        window = text[row_start:row_end]

        # Cut off at the table's "Total" footer row so it never leaks
        # into the last lease's window.
        total_match = re.search(r'\btotal\b', window, re.IGNORECASE)
        if total_match:
            window = window[:total_match.start()]

        id_match = re.search(r'\b(\d{6,8})\b', window)
        lease_id = id_match.group(1) if id_match else None

        dollar_tokens = re.findall(r'\$([\d.,]+)', window)
        amounts = [parse_amount(tok) for tok in dollar_tokens]
        amounts = [a for a in amounts if a is not None]
        total = max(amounts) if amounts else None

        rest = window[m.end() - row_start:]
        rest = re.sub(r'\$[\d.,]+', '', rest)
        if lease_id:
            rest = rest.replace(lease_id, '')

        label = clean_lease_label(rest)
        portfolio = portfolio_for_text(window)
        rows.append(
            {
                "id": uuid.uuid4().hex,
                "label": label,
                "leaseId": lease_id,
                "total": total,
                "portfolio": portfolio,
                "needsReview": total is None,
            }
        )
    return rows


def load_balances():
    if not os.path.exists(BALANCES_PATH):
        return {"asOf": None, "sourceImage": None, "rows": []}
    with open(BALANCES_PATH) as f:
        return json.load(f)


def save_balances(data):
    os.makedirs(os.path.dirname(BALANCES_PATH), exist_ok=True)
    with open(BALANCES_PATH, "w") as f:
        json.dump(data, f, indent=2)


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

# Simple in-memory cache so the dashboard doesn't hammer the Buildium API
# on every page refresh. Tune TTL with CACHE_SECONDS env var.
_cache = {"data": None, "fetched_at": 0}
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "120"))


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
