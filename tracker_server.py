from __future__ import annotations

import html
import json
import os
import uuid
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, quote_plus

from flask import Flask, request, redirect, jsonify, render_template_string, Response, send_file

app = Flask(__name__)

APP_DIR = Path(__file__).resolve().parent
LOGO_FILE = APP_DIR / "QRLogo.png"

DATA_DIR = Path(os.environ.get("QR_TRACKER_DATA_DIR", "tracker_data"))
DATA_DIR.mkdir(exist_ok=True)

SCAN_FILE = DATA_DIR / "scans.jsonl"
CONVERSION_FILE = DATA_DIR / "conversions.jsonl"
QR_FILE = DATA_DIR / "qrs.jsonl"
GEO_CACHE_FILE = DATA_DIR / "geo_cache.json"


# -----------------------------
# Helpers
# -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(value: str | None, fallback: str = "") -> str:
    return (value or fallback or "").strip()


def safe(value: str | None) -> str:
    return html.escape(clean(value), quote=True)


def get_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def device_type(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if any(x in ua for x in ["iphone", "android", "mobile"]):
        return "Mobile"
    if any(x in ua for x in ["ipad", "tablet"]):
        return "Tablet"
    return "Desktop"


def browser_name(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "edg" in ua:
        return "Edge"
    if "chrome" in ua and "chromium" not in ua:
        return "Chrome"
    if "safari" in ua and "chrome" not in ua:
        return "Safari"
    if "firefox" in ua:
        return "Firefox"
    return "Other"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def append_jsonl(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def rewrite_jsonl(path: Path, rows: list[dict]) -> None:
    """Rewrite a JSONL file safely after deleting QR records."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp_path.replace(path)


def load_scans() -> list[dict]:
    return load_jsonl(SCAN_FILE)


def load_conversions() -> list[dict]:
    return load_jsonl(CONVERSION_FILE)


def save_scan(scan: dict) -> None:
    append_jsonl(SCAN_FILE, scan)


def save_conversion(conversion: dict) -> None:
    append_jsonl(CONVERSION_FILE, conversion)


def load_registered_qrs() -> list[dict]:
    return load_jsonl(QR_FILE)


def save_registered_qrs(rows: list[dict]) -> None:
    rewrite_jsonl(QR_FILE, rows)


def qr_identity_key(row: dict) -> tuple[str, str, str, str, str]:
    return (
        clean(row.get("token")).lower(),
        clean(row.get("user")).lower(),
        clean(row.get("business")).lower(),
        clean(row.get("campaign"), "default").lower(),
        clean(row.get("slug"), "default").lower(),
    )


def upsert_registered_qr(qr: dict) -> dict:
    rows = load_registered_qrs()
    qr = dict(qr)
    qr.setdefault("registered_at", now_iso())
    qr["updated_at"] = now_iso()

    qr_id = clean(qr.get("qr_id") or qr.get("id"))
    new_key = qr_identity_key(qr)
    merged: list[dict] = []
    replaced = False

    for row in rows:
        same_id = qr_id and clean(row.get("qr_id") or row.get("id")) == qr_id
        same_identity = qr_identity_key(row) == new_key
        if same_id or same_identity:
            existing = dict(row)
            existing.update({k: v for k, v in qr.items() if v not in (None, "")})
            if "registered_at" not in existing:
                existing["registered_at"] = clean(row.get("registered_at"), now_iso())
            existing["updated_at"] = qr["updated_at"]
            merged.append(existing)
            qr = existing
            replaced = True
        else:
            merged.append(row)

    if not replaced:
        merged.append(qr)

    save_registered_qrs(merged)
    return qr


def filtered_registered_qrs(user: str, business: str, campaign: str = "", slug: str = "", token: str = "") -> list[dict]:
    return filtered_rows(load_registered_qrs(), user, business, campaign, slug, token)


def registered_qr_count_by(user: str, business: str, token: str, key: str, campaign: str = "") -> list[tuple[str, int]]:
    return count_by(filtered_registered_qrs(user, business, campaign, token=token), key, "default")


def load_geo_cache() -> dict:
    if not GEO_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(GEO_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_geo_cache(cache: dict) -> None:
    try:
        GEO_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


def lookup_geo(ip: str) -> dict:
    if not ip or ip.startswith(("127.", "10.", "192.168.", "172.")) or ip == "::1":
        return {"country": "Local", "region": "Local", "city": "Local", "lat": None, "lon": None}

    cache = load_geo_cache()
    if ip in cache:
        return cache[ip]

    geo = {"country": "Unknown", "region": "Unknown", "city": "Unknown", "lat": None, "lon": None}
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,lat,lon"
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if payload.get("status") == "success":
            geo = {
                "country": clean(payload.get("country"), "Unknown"),
                "region": clean(payload.get("regionName"), "Unknown"),
                "city": clean(payload.get("city"), "Unknown"),
                "lat": payload.get("lat"),
                "lon": payload.get("lon"),
            }
    except Exception:
        pass

    cache[ip] = geo
    save_geo_cache(cache)
    return geo


def selected_campaign_from_request() -> str:
    value = clean(request.args.get("campaign"))
    if value.lower() in {"all", "all campaigns"}:
        return ""
    return value


def selected_slug_from_request() -> str:
    value = clean(request.args.get("slug"))
    if value.lower() in {"all", "all qr codes", "all qrs"}:
        return ""
    return value


def selected_token_from_request() -> str:
    return clean(request.args.get("token"))


def request_identity() -> tuple[str, str, str]:
    """Read tracker identity from query string or form data."""
    user = clean(request.values.get("user"))
    business = clean(request.values.get("business"))
    token = clean(request.values.get("token"))
    return user, business, token


def has_required_identity(user: str = "", business: str = "", token: str = "") -> bool:
    return bool(clean(user) and clean(business) and clean(token))


def identity_query(user: str, business: str, token: str, campaign: str = "", slug: str = "") -> str:
    parts = [
        f"user={quote_plus(user)}",
        f"business={quote_plus(business)}",
        f"token={quote_plus(token)}",
    ]
    if campaign:
        parts.append(f"campaign={quote_plus(campaign)}")
    if slug:
        parts.append(f"slug={quote_plus(slug)}")
    return "&".join(parts)


def identity_gate(active: str = "dashboard"):
    user, business, token = request_identity()
    if has_required_identity(user, business, token):
        return None
    return render_home_page(
        message="Enter your name, business name and private token to open the tracker dashboard.",
        active=active,
    )


def filtered_rows(rows: list[dict], user: str, business: str, campaign: str = "", slug: str = "", token: str = "") -> list[dict]:
    user_l = clean(user).lower()
    business_l = clean(business).lower()
    campaign_l = clean(campaign).lower()
    slug_l = clean(slug).lower()
    token_l = clean(token).lower()

    # The secure creator token keeps each creator's tracker data separated.
    # Dashboards/APIs should not expose rows unless a matching token is supplied.
    if not token_l:
        return []

    rows = [r for r in rows if clean(r.get("token")).lower() == token_l]
    if user_l:
        rows = [r for r in rows if clean(r.get("user")).lower() == user_l]
    if business_l:
        rows = [r for r in rows if clean(r.get("business")).lower() == business_l]
    if campaign_l:
        rows = [r for r in rows if clean(r.get("campaign"), "default").lower() == campaign_l]
    if slug_l:
        rows = [r for r in rows if clean(r.get("slug"), "default").lower() == slug_l]
    return rows


def filtered_scans(user: str, business: str, campaign: str = "", slug: str = "", token: str = "") -> list[dict]:
    return filtered_rows(load_scans(), user, business, campaign, slug, token)


def filtered_conversions(user: str, business: str, campaign: str = "", slug: str = "", token: str = "") -> list[dict]:
    return filtered_rows(load_conversions(), user, business, campaign, slug, token)


def count_by(rows: list[dict], key: str, fallback: str = "uncategorised") -> list[tuple[str, int]]:
    counter = Counter(clean(row.get(key), fallback) for row in rows)
    return counter.most_common()


def count_by_day(rows: list[dict]) -> dict[str, int]:
    result = defaultdict(int)
    for row in rows:
        day = clean(row.get("timestamp"))[:10] or "unknown"
        result[day] += 1
    return dict(sorted(result.items()))


def conversion_rate(scan_count: int, conversion_count: int) -> float:
    if scan_count <= 0:
        return 0.0
    return round((conversion_count / scan_count) * 100, 2)


def campaign_scores(scans: list[dict]) -> dict[str, float]:
    rows = count_by(scans, "campaign", "default")
    total = sum(v for _, v in rows) or 1
    return {k: round((v / total) * 100, 2) for k, v in rows}


def json_for_js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def available_campaigns(user: str = "", business: str = "", token: str = "") -> list[str]:
    scan_rows = filtered_rows(load_scans(), user, business, token=token)
    qr_rows = filtered_registered_qrs(user, business, token=token)
    names = sorted({
        clean(r.get("campaign"), "default")
        for r in scan_rows + qr_rows
        if clean(r.get("campaign"), "default")
    })
    if "default" not in names:
        names.insert(0, "default")
    return names


def available_slugs(user: str = "", business: str = "", campaign: str = "", token: str = "") -> list[str]:
    scan_rows = filtered_rows(load_scans(), user, business, campaign, token=token)
    qr_rows = filtered_registered_qrs(user, business, campaign, token=token)
    names = sorted({
        clean(r.get("slug"), "default")
        for r in scan_rows + qr_rows
        if clean(r.get("slug"), "default")
    })
    if "default" not in names:
        names.insert(0, "default")
    return names


def qr_summary_rows(user: str = "", business: str = "", campaign: str = "", token: str = "") -> list[dict]:
    registered = filtered_registered_qrs(user, business, campaign, token=token)
    scans = filtered_scans(user, business, campaign, token=token)
    conversions = filtered_conversions(user, business, campaign, token=token)

    conv_by_key = Counter(
        (clean(c.get("campaign"), "default"), clean(c.get("slug"), "default"))
        for c in conversions
    )

    grouped: dict[tuple[str, str], dict] = {}

    for qr in registered:
        qr_campaign = clean(qr.get("campaign"), "default")
        slug = clean(qr.get("slug"), "default")
        key = (qr_campaign, slug)
        grouped[key] = {
            "id": clean(qr.get("qr_id") or qr.get("id")),
            "qr_id": clean(qr.get("qr_id") or qr.get("id")),
            "type": clean(qr.get("type"), "Saved QR"),
            "slug": slug,
            "campaign": qr_campaign,
            "source": clean(qr.get("source"), "qr"),
            "medium": clean(qr.get("medium"), "qr"),
            "destination": clean(qr.get("destination")),
            "tracked_url": clean(qr.get("tracked_url")),
            "created": clean(qr.get("created") or qr.get("registered_at")),
            "first_seen": clean(qr.get("first_seen") or qr.get("created") or qr.get("registered_at")),
            "last_seen": clean(qr.get("last_seen") or qr.get("updated_at") or qr.get("created")),
            "scans": 0,
            "conversions": 0,
            "conversion_rate": 0.0,
            "top_city": "Unknown",
            "top_device": "Unknown",
            "registered": True,
            "cities": Counter(),
            "devices": Counter(),
        }

    for scan in scans:
        qr_campaign = clean(scan.get("campaign"), "default")
        slug = clean(scan.get("slug"), "default")
        key = (qr_campaign, slug)
        if key not in grouped:
            grouped[key] = {
                "id": "",
                "qr_id": "",
                "type": "Tracked QR",
                "slug": slug,
                "campaign": qr_campaign,
                "source": clean(scan.get("source"), "qr"),
                "medium": clean(scan.get("medium"), "qr"),
                "destination": clean(scan.get("destination")),
                "tracked_url": "",
                "created": clean(scan.get("timestamp")),
                "first_seen": clean(scan.get("timestamp")),
                "last_seen": clean(scan.get("timestamp")),
                "scans": 0,
                "conversions": 0,
                "conversion_rate": 0.0,
                "top_city": "Unknown",
                "top_device": "Unknown",
                "registered": False,
                "cities": Counter(),
                "devices": Counter(),
            }

        row = grouped[key]
        row["scans"] += 1
        row["source"] = row.get("source") or clean(scan.get("source"), "qr")
        row["medium"] = row.get("medium") or clean(scan.get("medium"), "qr")
        if not row.get("destination"):
            row["destination"] = clean(scan.get("destination"))
        timestamp = clean(scan.get("timestamp"))
        if timestamp:
            row["last_seen"] = max(clean(row.get("last_seen")), timestamp) if clean(row.get("last_seen")) else timestamp
            row["first_seen"] = min(clean(row.get("first_seen")), timestamp) if clean(row.get("first_seen")) else timestamp
        row["cities"][clean(scan.get("city"), "Unknown")] += 1
        row["devices"][clean(scan.get("device"), "Unknown")] += 1

    for key, row in grouped.items():
        row["conversions"] = conv_by_key.get(key, 0)
        row["conversion_rate"] = conversion_rate(row["scans"], row["conversions"])
        row["top_city"] = row["cities"].most_common(1)[0][0] if row["cities"] else "Unknown"
        row["top_device"] = row["devices"].most_common(1)[0][0] if row["devices"] else "Unknown"
        row.pop("cities", None)
        row.pop("devices", None)

    return sorted(
        grouped.values(),
        key=lambda x: (x.get("scans", 0), clean(x.get("last_seen") or x.get("created"))),
        reverse=True,
    )

def heatmap_points(scans: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str], dict] = {}
    for scan in scans:
        city = clean(scan.get("city"), "Unknown")
        country = clean(scan.get("country"), "Unknown")
        region = clean(scan.get("region"), "Unknown")
        key = (city, region, country)
        if key not in grouped:
            grouped[key] = {
                "city": city,
                "region": region,
                "country": country,
                "count": 0,
                "lat": scan.get("lat"),
                "lon": scan.get("lon"),
            }
        grouped[key]["count"] += 1
    return sorted(grouped.values(), key=lambda x: x["count"], reverse=True)


# -----------------------------
# Branding assets
# -----------------------------
@app.get("/QRLogo.png")
def qr_mode_logo():
    """Serve the QR Mode logo from the same folder as tracker.py."""
    if LOGO_FILE.exists():
        return send_file(LOGO_FILE)
    return Response(status=404)


def brand_block(section: str = "") -> str:
    section_html = f'<div class="brand-subtitle">{safe(section)}</div>' if section else ""
    return f"""
    <div class="brandbar">
        <div class="brand-logo">
            <img src="/QRLogo.png" alt="QR Mode logo" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
            <span>QM</span>
        </div>
        <div>
            <div class="brand-name"><span class="gradient-text">QR</span> Mode</div>
            {section_html}
        </div>
    </div>
    """


# -----------------------------
# Tracking routes
# -----------------------------
@app.get("/")
def home():
    return render_home_page()


def render_home_page(message: str = "", active: str = "home"):
    user = clean(request.values.get("user"))
    business = clean(request.values.get("business"))
    token = clean(request.values.get("token"))

    return render_template_string("""
<!doctype html>
<html>
<head>
    <title>QR Mode Tracker</title>
    {{ css | safe }}
</head>
<body>
<div class="setup-page">
    <div class="setup-shell">
        <div class="setup-logo">
            <img src="/QRLogo.png" alt="QR Mode logo" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
            <span>QM</span>
        </div>
        <p class="setup-eyebrow"><span class="gradient-text">QR</span> MODE TRACKER</p>
        <h1>Open your private QR dashboard</h1>
        <p class="setup-subtitle">Enter the same name, business name and private token from QR Mode. These details keep each creator's scans, campaigns, QR codes and exports separated.</p>

        {% if message %}
            <div class="setup-message">{{ message }}</div>
        {% endif %}

        <form class="setup-card" method="get" action="/dashboard">
            <label>Name *</label>
            <input name="user" placeholder="Your name" value="{{ user }}" required>

            <label>Business name *</label>
            <input name="business" placeholder="Business name" value="{{ business }}" required>

            <label>Private creator token *</label>
            <input name="token" placeholder="Paste your QR Mode token" value="{{ token }}" required>

            <button type="submit">Open Dashboard →</button>

            <div class="setup-pills">
                <span>Private stats</span>
                <span>Campaign reports</span>
                <span>QR library</span>
            </div>
        </form>

        <div class="setup-help-grid">
            <div>
                <strong>Where do I find the token?</strong>
                <p>Open QR Mode, go to the first setup page, then generate or export your private creator token.</p>
            </div>
            <div>
                <strong>Why is it required?</strong>
                <p>The secure creator token helps the tracker show only the QR codes and scans for that creator.</p>
            </div>
            <div>
                <strong>What can I view?</strong>
                <p>Dashboard, saved QR codes, analytics charts, campaign analytics, heatmap data and CSV exports.</p>
            </div>
        </div>
    </div>
</div>
</body>
</html>
    """, css=BASE_CSS, user=safe(user), business=safe(business), token=safe(token), message=message)


@app.get("/qr-track-health")
def qr_track_health():
    return jsonify({
        "status": "ok",
        "tracking": True,
        "version": "4.6-qr-mode-branding",
        "features": [
            "user filtering",
            "business filtering",
            "used campaign dropdown",
            "QR code library page",
            "slug / per-QR filtering",
            "campaign analytics dashboard",
            "campaign charts",
            "scan heatmap",
            "source tracking",
            "medium tracking",
            "slug tracking",
            "A/B variant tracking",
            "conversion tracking",
            "geo tracking",
            "device detection",
            "browser detection",
            "csv export",
            "creator token filtering",
            "private QR library sync",
        ],
    })


@app.get("/qr-track")
def qr_track():
    destination = clean(request.args.get("url"))
    user, business, token = request_identity()

    if not destination:
        return render_home_page("This tracked QR is missing a destination URL."), 400

    if not has_required_identity(user, business, token):
        return render_home_page("This tracked QR is missing its name, business name or private token."), 400

    destination = unquote(destination)
    if not destination.startswith(("http://", "https://")):
        destination = "https://" + destination

    user_agent = request.headers.get("User-Agent", "")
    ip = get_ip()
    geo = lookup_geo(ip)

    scan = {
        "id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "user": user,
        "business": business,
        "token": token,
        "slug": clean(request.args.get("slug"), "default"),
        "campaign": clean(request.args.get("campaign"), "default"),
        "source": clean(request.args.get("source"), "qr"),
        "medium": clean(request.args.get("medium"), "qr"),
        "variant": clean(request.args.get("variant"), "A"),
        "notes": clean(request.args.get("notes")),
        "destination": destination,
        "ip": ip,
        "country": geo.get("country", "Unknown"),
        "region": geo.get("region", "Unknown"),
        "city": geo.get("city", "Unknown"),
        "lat": geo.get("lat"),
        "lon": geo.get("lon"),
        "device": device_type(user_agent),
        "browser": browser_name(user_agent),
        "user_agent": user_agent,
        "referer": request.headers.get("Referer", ""),
        "converted": False,
    }

    save_scan(scan)
    return redirect(destination, code=302)


@app.get("/conversion")
def conversion():
    user, business, token = request_identity()
    if not has_required_identity(user, business, token):
        return jsonify({"status": "error", "message": "Missing name, business name or private token."}), 400

    payload = {
        "id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "user": user,
        "business": business,
        "token": token,
        "slug": clean(request.args.get("slug"), "default"),
        "campaign": clean(request.args.get("campaign"), "default"),
        "source": clean(request.args.get("source"), "qr"),
        "medium": clean(request.args.get("medium"), "qr"),
        "variant": clean(request.args.get("variant"), "A"),
        "value": clean(request.args.get("value"), "1"),
        "event": clean(request.args.get("event"), "conversion"),
        "ip": get_ip(),
        "user_agent": request.headers.get("User-Agent", ""),
    }
    save_conversion(payload)
    return jsonify({"status": "ok", "conversion": True, "id": payload["id"]})


# -----------------------------
# API routes
# -----------------------------
@app.get("/api/scans")
def api_scans():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    token = selected_token_from_request()
    if not has_required_identity(user, business, token):
        return jsonify({"count": 0, "scans": [], "status": "missing_identity"}), 401
    scans = filtered_scans(user, business, campaign, slug, token)
    return jsonify({"count": len(scans), "scans": scans})


@app.get("/api/campaigns")
def api_campaigns():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    token = selected_token_from_request()
    if not has_required_identity(user, business, token):
        return jsonify({"campaigns": [], "count": 0, "status": "missing_identity"}), 401
    campaigns = available_campaigns(user, business, token)
    return jsonify({"campaigns": campaigns, "count": len(campaigns)})


@app.post("/api/qrs/register")
def api_register_qr():
    payload = request.get_json(silent=True) or {}
    user = clean(payload.get("user") or request.values.get("user"))
    business = clean(payload.get("business") or request.values.get("business"))
    token = clean(payload.get("token") or request.values.get("token"))

    if not has_required_identity(user, business, token):
        return jsonify({
            "status": "error",
            "registered": False,
            "message": "Missing name, business name or secure creator token."
        }), 400

    incoming_qr_id = clean(payload.get("qr_id") or payload.get("id") or str(uuid.uuid4()))

    qr = {
        "id": incoming_qr_id,
        "qr_id": incoming_qr_id,
        "type": clean(payload.get("type"), "Saved QR"),
        "campaign": clean(payload.get("campaign"), "default"),
        "slug": clean(payload.get("slug"), "default"),
        "destination": clean(payload.get("destination")),
        "tracked_url": clean(payload.get("tracked_url")),
        "created": clean(payload.get("created"), now_iso()),
        "tracker_base": clean(payload.get("tracker_base")),
        "user": user,
        "business": business,
        "token": token,
        "source": clean(payload.get("source"), "qr"),
        "medium": clean(payload.get("medium"), "qr"),
    }

    saved = upsert_registered_qr(qr)
    return jsonify({
        "status": "ok",
        "registered": True,
        "qr": saved,
    })


@app.get("/api/qrs")
def api_qrs():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    token = selected_token_from_request()
    if not has_required_identity(user, business, token):
        return jsonify({"count": 0, "qrs": [], "status": "missing_identity"}), 401
    rows = qr_summary_rows(user, business, campaign, token)
    return jsonify({"count": len(rows), "qrs": rows})


@app.route("/api/qrs/delete", methods=["GET", "POST"])
def api_delete_qr():
    user, business, token = request_identity()
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    qr_id = clean(request.values.get("qr_id"))

    if not has_required_identity(user, business, token):
        return jsonify({
            "status": "error",
            "deleted": False,
            "message": "Missing name, business name or secure creator token."
        }), 400

    if not qr_id and (not campaign or not slug):
        return jsonify({
            "status": "error",
            "deleted": False,
            "message": "QR ID or campaign and slug are required to delete a QR code from the tracker."
        }), 400

    def identity_matches(row: dict) -> bool:
        if clean(row.get("token")).lower() != token.lower():
            return False
        if clean(row.get("user")).lower() != user.lower():
            return False
        if clean(row.get("business")).lower() != business.lower():
            return False
        if qr_id and clean(row.get("qr_id") or row.get("id")) == qr_id:
            return True
        return (
            clean(row.get("campaign"), "default").lower() == campaign.lower()
            and clean(row.get("slug"), "default").lower() == slug.lower()
        )

    scans_before = load_scans()
    conversions_before = load_conversions()
    qrs_before = load_registered_qrs()

    scans_after = [row for row in scans_before if not identity_matches(row)]
    conversions_after = [row for row in conversions_before if not identity_matches(row)]
    qrs_after = [row for row in qrs_before if not identity_matches(row)]

    rewrite_jsonl(SCAN_FILE, scans_after)
    rewrite_jsonl(CONVERSION_FILE, conversions_after)
    rewrite_jsonl(QR_FILE, qrs_after)

    deleted_scans = len(scans_before) - len(scans_after)
    deleted_conversions = len(conversions_before) - len(conversions_after)
    deleted_qrs = len(qrs_before) - len(qrs_after)

    return jsonify({
        "status": "ok",
        "deleted": True,
        "campaign": campaign,
        "slug": slug,
        "qr_id": qr_id,
        "deleted_saved_qrs": deleted_qrs,
        "deleted_scans": deleted_scans,
        "deleted_conversions": deleted_conversions,
    })

@app.get("/api/summary")
def api_summary():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    token = selected_token_from_request()
    if not has_required_identity(user, business, token):
        return jsonify({"status": "missing_identity", "message": "Name, business name and secure creator token are required."}), 401
    scans = filtered_scans(user, business, campaign, slug, token)
    conversions = filtered_conversions(user, business, campaign, slug, token)

    by_day = count_by_day(scans)
    by_campaign = dict(count_by(scans, "campaign", "default"))
    by_source = dict(count_by(scans, "source", "qr"))
    by_medium = dict(count_by(scans, "medium", "qr"))
    by_slug = dict(count_by(scans, "slug", "default"))
    by_device = dict(count_by(scans, "device", "Unknown"))
    by_browser = dict(count_by(scans, "browser", "Unknown"))
    by_country = dict(count_by(scans, "country", "Unknown"))
    by_city = dict(count_by(scans, "city", "Unknown"))
    by_variant = dict(count_by(scans, "variant", "A"))

    return jsonify({
        "total_scans": len(scans),
        "total_conversions": len(conversions),
        "conversion_rate": conversion_rate(len(scans), len(conversions)),
        "selected_campaign": campaign or "All Campaigns",
        "selected_slug": slug or "All QR Codes",
        "available_campaigns": available_campaigns(user, business, selected_token_from_request()),
        "available_qr_codes": available_slugs(user, business, campaign, selected_token_from_request()),
        "by_day": by_day,
        "by_campaign": by_campaign,
        "by_source": by_source,
        "by_medium": by_medium,
        "by_slug": by_slug,
        "by_device": by_device,
        "by_browser": by_browser,
        "by_country": by_country,
        "by_city": by_city,
        "by_variant": by_variant,
        "heatmap_points": heatmap_points(scans),
        "qr_codes": qr_summary_rows(user, business, campaign, selected_token_from_request()),
        "campaign_performance": campaign_scores(filtered_scans(user, business, token=token)),
        "insights": {
            "best_campaign": max(by_campaign, key=by_campaign.get) if by_campaign else None,
            "best_source": max(by_source, key=by_source.get) if by_source else None,
            "best_device": max(by_device, key=by_device.get) if by_device else None,
            "best_day": max(by_day, key=by_day.get) if by_day else None,
            "best_city": max(by_city, key=by_city.get) if by_city else None,
        },
    })


@app.get("/export.csv")
def export_csv():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    token = selected_token_from_request()
    if not has_required_identity(user, business, token):
        return render_home_page("Enter your name, business name and secure creator token before exporting CSV data."), 401
    scans = filtered_scans(user, business, campaign, slug, token)

    fields = [
        "timestamp", "user", "business", "campaign", "source", "medium", "variant",
        "slug", "notes", "destination", "country", "region", "city", "lat", "lon",
        "device", "browser", "ip", "user_agent",
    ]

    def generate():
        yield ",".join(fields) + "\n"
        for scan in scans:
            row = []
            for field in fields:
                value = str(scan.get(field, "")).replace('"', '""')
                row.append(f'"{value}"')
            yield ",".join(row) + "\n"

    suffix = ""
    if campaign:
        suffix += f"_{campaign}"
    if slug:
        suffix += f"_{slug}"
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=qr_scans{suffix}.csv"},
    )


# -----------------------------
# UI helpers
# -----------------------------
BASE_CSS = """
<style>
    :root {
        --ink: #0f172a;
        --muted: #64748b;
        --blue: #2563eb;
        --cyan: #22d3ee;
        --purple: #8b5cf6;
        --teal: #0891b2;
        --card: rgba(255,255,255,.84);
    }

    .gradient-text {
        background: linear-gradient(135deg, var(--cyan), var(--blue), var(--purple));
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }
    .brandbar {
        display: flex;
        align-items: center;
        gap: 14px;
        margin-bottom: 14px;
    }
    .brand-logo {
        width: 58px;
        height: 58px;
        display: flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 58px;
    }
    .brand-logo img {
        width: 54px;
        height: 54px;
        object-fit: contain;
        display: block;
    }
    .brand-logo span {
        width: 54px;
        height: 54px;
        display: none;
        align-items: center;
        justify-content: center;
        border-radius: 18px;
        color: #fff;
        font-size: 19px;
        font-weight: 950;
        background: linear-gradient(135deg, var(--cyan), var(--blue), var(--purple));
    }
    .brand-name {
        font-size: 30px;
        line-height: 1;
        letter-spacing: -0.7px;
        font-weight: 950;
        color: var(--ink);
    }
    .brand-subtitle {
        margin-top: 5px;
        color: var(--muted);
        font-size: 13px;
        font-weight: 850;
        letter-spacing: .04em;
        text-transform: uppercase;
    }
    body {
        margin: 0;
        font-family: Segoe UI, Arial, sans-serif;
        background:
            radial-gradient(circle at 8% 8%, rgba(34,211,238,.26), transparent 30%),
            radial-gradient(circle at 88% 12%, rgba(168,85,247,.22), transparent 28%),
            radial-gradient(circle at 58% 100%, rgba(20,184,166,.18), transparent 35%),
            linear-gradient(135deg, #f8fbff, #eff6ff, #f5f3ff);
        color: var(--ink);
    }
    .wrap { max-width: 1320px; margin: 0 auto; padding: 32px; }
    .hero, .card, .stat, .qr-card {
        background: var(--card);
        border: 1px solid rgba(255,255,255,.92);
        box-shadow: 0 24px 70px rgba(59,130,246,.13);
        backdrop-filter: blur(18px);
    }
    .hero { border-radius: 32px; padding: 26px; margin-bottom: 20px; }
    .card { border-radius: 28px; padding: 22px; margin-bottom: 20px; }
    .top { display:flex; align-items:center; justify-content:space-between; gap:14px; flex-wrap:wrap; }
    h1 { margin:0; font-size:38px; letter-spacing:-.9px; }
    h2 { margin-top:0; font-size:20px; }
    .muted { color:var(--muted); font-weight:650; line-height:1.45; }
    form { display:flex; gap:12px; flex-wrap:wrap; margin-top:18px; align-items:center; }
    input, select {
        padding: 14px 16px;
        border-radius: 16px;
        border: 1px solid #cbd5e1;
        font-weight: 750;
        min-width: 220px;
        background: rgba(255,255,255,.94);
        color: var(--ink);
    }
    button, a.btn {
        padding:14px 18px; border-radius:16px; border:none;
        background:linear-gradient(135deg,var(--cyan),var(--purple));
        color:white; font-weight:950; text-decoration:none; cursor:pointer;
    }
    .nav { display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; }
    .nav a {
        padding:10px 14px; border-radius:999px; background:rgba(255,255,255,.82);
        color:var(--blue); text-decoration:none; font-weight:900; border:1px solid #dbeafe;
    }
    .nav a.active { color:white; background:linear-gradient(135deg,var(--cyan),var(--purple)); border:none; }
    .stats { display:grid; grid-template-columns:repeat(5,1fr); gap:16px; margin-bottom:20px; }
    .stat { border-radius:24px; padding:20px; }
    .stat strong { display:block; font-size:32px; margin-top:4px; }
    .grid { display:grid; grid-template-columns:repeat(3,1fr); gap:18px; }
    .grid2 { display:grid; grid-template-columns:repeat(2,1fr); gap:18px; }
    table { width:100%; border-collapse:collapse; background:white; border-radius:18px; overflow:hidden; }
    th, td { padding:12px; border-bottom:1px solid #e2e8f0; text-align:left; font-size:14px; }
    th { background:#f8fafc; color:#334155; }
    .pill { display:inline-block; background:#ecfeff; color:var(--teal); border-radius:999px; padding:6px 10px; font-weight:850; font-size:12px; }
    .score-bar { background:#e2e8f0; height:10px; border-radius:999px; overflow:hidden; margin-top:8px; }
    .score-fill { height:100%; border-radius:999px; background:linear-gradient(135deg,var(--cyan),var(--purple)); }
    canvas { width:100% !important; max-height:360px; }
    .heatmap-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; }
    .heat-cell { border-radius:22px; padding:18px; color:#0f172a; background:linear-gradient(135deg, rgba(34,211,238,.18), rgba(139,92,246,.18)); border:1px solid rgba(255,255,255,.95); }
    .heat-cell strong { display:block; font-size:26px; }
    .map-frame { min-height:420px; border-radius:28px; overflow:hidden; border:1px solid #dbeafe; background:#fff; }
    .map-frame iframe { width:100%; height:420px; border:0; }
    .qr-card-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:18px; }
    .qr-card { border-radius:28px; padding:22px; }
    .qr-card-top { display:flex; justify-content:space-between; gap:14px; align-items:flex-start; }
    .qr-card-title { font-size:22px; font-weight:950; color:var(--ink); margin:0 0 6px 0; }
    .qr-card-sub { color:var(--muted); font-weight:700; overflow-wrap:anywhere; }
    .qr-actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; }
    .qr-actions a { padding:10px 13px; border-radius:999px; text-decoration:none; font-weight:900; background:#ecfeff; color:var(--teal); border:1px solid #bae6fd; }
    @media (max-width:1000px) { .stats,.grid,.grid2,.heatmap-grid,.qr-card-grid { grid-template-columns:1fr; } }

    .setup-page {
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 28px;
        box-sizing: border-box;
    }
    .setup-shell {
        width: min(920px, 100%);
        background: rgba(255,255,255,.80);
        border: 1px solid rgba(255,255,255,.94);
        border-radius: 36px;
        box-shadow: 0 32px 100px rgba(59,130,246,.18), 0 12px 46px rgba(139,92,246,.12);
        backdrop-filter: blur(22px);
        padding: 38px;
        text-align: center;
    }
    .setup-logo {
        width: 86px;
        height: 86px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        margin-bottom: 14px;
    }
    .setup-logo img {
        width: 82px;
        height: 82px;
        object-fit: contain;
        display: block;
    }
    .setup-logo span {
        width: 70px;
        height: 70px;
        display: none;
        align-items: center;
        justify-content: center;
        border-radius: 24px;
        color: white;
        font-size: 25px;
        font-weight: 950;
        background: linear-gradient(135deg, var(--cyan), var(--blue), var(--purple));
        box-shadow: 0 18px 42px rgba(34,211,238,.30);
    }
    .setup-eyebrow {
        margin: 0 0 8px;
        color: var(--teal);
        font-size: 12px;
        letter-spacing: .22em;
        font-weight: 950;
    }
    .setup-subtitle {
        max-width: 690px;
        margin: 12px auto 0;
        color: var(--muted);
        font-weight: 700;
        line-height: 1.55;
    }
    .setup-message {
        margin: 18px auto 0;
        max-width: 680px;
        border-radius: 18px;
        padding: 12px 16px;
        color: #075985;
        background: rgba(236,254,255,.92);
        border: 1px solid #bae6fd;
        font-weight: 850;
    }
    .setup-card {
        margin: 22px auto 0;
        max-width: 620px;
        display: grid;
        grid-template-columns: 1fr;
        gap: 9px;
        padding: 24px;
        border-radius: 28px;
        background: rgba(255,255,255,.86);
        border: 1px solid rgba(226,232,240,.95);
        box-shadow: 0 18px 50px rgba(15,23,42,.08);
        text-align: left;
    }
    .setup-card label {
        font-size: 13px;
        font-weight: 950;
        color: #334155;
        margin-top: 5px;
    }
    .setup-card input { width: 100%; min-width: 0; box-sizing: border-box; }
    .setup-card button { margin-top: 10px; width: 100%; }
    .setup-pills {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        justify-content: center;
        margin-top: 4px;
    }
    .setup-pills span {
        border-radius: 999px;
        padding: 7px 11px;
        background: #f0fdfa;
        color: #0f766e;
        font-size: 12px;
        font-weight: 900;
        border: 1px solid #ccfbf1;
    }
    .setup-help-grid {
        margin-top: 18px;
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 12px;
        text-align: left;
    }
    .setup-help-grid div {
        border-radius: 22px;
        padding: 16px;
        background: rgba(255,255,255,.62);
        border: 1px solid rgba(226,232,240,.82);
    }
    .setup-help-grid strong { color: var(--ink); }
    .setup-help-grid p { margin: 7px 0 0; color: var(--muted); font-weight: 700; line-height: 1.45; }
    @media (max-width:800px) { .setup-help-grid { grid-template-columns: 1fr; } .setup-shell { padding: 24px; } }

</style>
"""


def campaign_dropdown(user: str, business: str, selected: str = "", token: str = "") -> str:
    campaigns = available_campaigns(user, business, token)
    options = ['<option value="">All Campaigns</option>']
    for campaign in campaigns:
        selected_attr = "selected" if campaign == selected else ""
        options.append(f'<option value="{safe(campaign)}" {selected_attr}>{safe(campaign)}</option>')
    return "\n".join(options)


def slug_dropdown(user: str, business: str, campaign: str = "", selected: str = "", token: str = "") -> str:
    slugs = available_slugs(user, business, campaign, token)
    options = ['<option value="">All QR Codes</option>']
    for slug in slugs:
        selected_attr = "selected" if slug == selected else ""
        options.append(f'<option value="{safe(slug)}" {selected_attr}>{safe(slug)}</option>')
    return "\n".join(options)


def filter_form(user: str, business: str, action: str, campaign: str = "", slug: str = "", token: str = "") -> str:
    export_url = f"/export.csv?user={quote_plus(user)}&business={quote_plus(business)}&token={quote_plus(token)}&campaign={quote_plus(campaign)}&slug={quote_plus(slug)}"
    return f"""
    <form method="get" action="{action}">
        <input name="user" placeholder="Name" value="{safe(user)}">
        <input name="business" placeholder="Business name" value="{safe(business)}">
        <input name="token" placeholder="Private token" value="{safe(token)}">
        <select name="campaign">{campaign_dropdown(user, business, campaign, token)}</select>
        <select name="slug">{slug_dropdown(user, business, campaign, slug, token)}</select>
        <button type="submit">View Stats</button>
        <a class="btn" href="{export_url}">Export CSV</a>
    </form>
    """


def nav_links(user: str, business: str, campaign: str, slug: str, active: str, token: str = "") -> str:
    q = f"user={quote_plus(user)}&business={quote_plus(business)}&token={quote_plus(token)}&campaign={quote_plus(campaign)}&slug={quote_plus(slug)}"
    return f"""
    <div class="nav">
        <a class="{'active' if active == 'dashboard' else ''}" href="/dashboard?{q}">Dashboard</a>
        <a class="{'active' if active == 'qrs' else ''}" href="/qrs?{q}">Saved QR Codes</a>
        <a class="{'active' if active == 'analytics' else ''}" href="/analytics?{q}">Analytics Charts</a>
        <a class="{'active' if active == 'campaigns' else ''}" href="/campaigns?{q}">Campaign Analytics</a>
        <a class="{'active' if active == 'heatmap' else ''}" href="/heatmap?{q}">Scan Heatmap</a>
        <a href="/api/summary?{q}">API Summary</a>
    </div>
    """


def stats_block(total: int, conversions_count: int, conv_rate: float, campaign_count: int, slug_count: int) -> str:
    return f"""
    <div class="stats">
        <div class="stat"><span class="muted">Total scans</span><strong>{total}</strong></div>
        <div class="stat"><span class="muted">Conversions</span><strong>{conversions_count}</strong></div>
        <div class="stat"><span class="muted">Conv. rate</span><strong>{conv_rate}%</strong></div>
        <div class="stat"><span class="muted">Campaigns</span><strong>{campaign_count}</strong></div>
        <div class="stat"><span class="muted">QR codes</span><strong>{slug_count}</strong></div>
    </div>
    """


# -----------------------------
# Dashboard routes
# -----------------------------
@app.get("/dashboard")
def dashboard():
    gated = identity_gate("dashboard")
    if gated is not None:
        return gated
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    token = selected_token_from_request()

    scans = filtered_scans(user, business, campaign, slug, token)
    conversions = filtered_conversions(user, business, campaign, slug, token)
    all_scans_for_campaigns = filtered_scans(user, business, token=token)

    campaign_rows = count_by(all_scans_for_campaigns, "campaign", "default")
    source_rows = count_by(scans, "source", "qr")
    slug_rows = count_by(scans, "slug", "default")
    city_rows = count_by(scans, "city", "Unknown")
    variant_rows = count_by(scans, "variant", "A")
    scores = campaign_scores(all_scans_for_campaigns)

    total = len(scans)
    best_campaign = campaign_rows[0][0] if campaign_rows else "None yet"
    best_source = source_rows[0][0] if source_rows else "None yet"
    best_qr = slug_rows[0][0] if slug_rows else "None yet"
    best_city = city_rows[0][0] if city_rows else "None yet"
    selected_label = f"{campaign or 'All Campaigns'} · {slug or 'All QR Codes'}"

    return render_template_string("""
<!doctype html>
<html>
<head>
    <title>QR Mode Tracker Dashboard</title>
    <meta http-equiv="refresh" content="30">
    {{ css | safe }}
</head>
<body>
<div class="wrap">
    <div class="hero">
        <div class="top"><div>{{ brand | safe }}<p class="muted">Live QR scans, campaign performance, A/B variants, devices and locations.</p></div><span class="pill">{{ selected_label }}</span></div>
        {{ form | safe }}
        {{ nav | safe }}
    </div>
    {{ stats | safe }}
    <div class="grid">
        <div class="card"><h2>Top Insights</h2><p><strong>Best Campaign:</strong> <span class="pill">{{ best_campaign }}</span></p><p><strong>Best Source:</strong> <span class="pill">{{ best_source }}</span></p><p><strong>Top QR:</strong> <span class="pill">{{ best_qr }}</span></p><p><strong>Best City:</strong> <span class="pill">{{ best_city }}</span></p></div>
        <div class="card"><h2>Growth Tip</h2><p class="muted">Use the campaign and QR dropdowns to compare city, event, flyer-drop, poster and individual QR performance.</p></div>
        <div class="card"><h2>A/B Testing</h2>{% for name, count in variant_rows %}<p><span class="pill">Variant {{ name }}</span> — {{ count }}</p>{% else %}<p class="muted">No variant data yet.</p>{% endfor %}</div>
    </div>
    <div class="grid">
        <div class="card"><h2>Campaign Scores</h2>{% for name, score in scores.items() %}<p><span class="pill">{{ name }}</span> — {{ score }}%</p><div class="score-bar"><div class="score-fill" style="width: {{ score }}%;"></div></div>{% else %}<p class="muted">No campaign data yet.</p>{% endfor %}</div>
        <div class="card"><h2>Sources</h2>{% for name, count in source_rows %}<p><span class="pill">{{ name }}</span> — {{ count }}</p>{% else %}<p class="muted">No source data yet.</p>{% endfor %}</div>
        <div class="card"><h2>Locations</h2>{% for name, count in city_rows %}<p><span class="pill">{{ name }}</span> — {{ count }}</p>{% else %}<p class="muted">No location data yet.</p>{% endfor %}</div>
    </div>
    <div class="card"><h2>Recent scans</h2><table><tr><th>Time</th><th>Campaign</th><th>Source</th><th>Variant</th><th>City</th><th>Device</th><th>Browser</th><th>Slug</th><th>Destination</th></tr>{% for scan in scans[-60:][::-1] %}<tr><td>{{ scan.timestamp }}</td><td>{{ scan.campaign }}</td><td>{{ scan.source }}</td><td>{{ scan.variant }}</td><td>{{ scan.city }}</td><td>{{ scan.device }}</td><td>{{ scan.browser }}</td><td>{{ scan.slug }}</td><td>{{ scan.destination }}</td></tr>{% else %}<tr><td colspan="9" class="muted">No scans yet.</td></tr>{% endfor %}</table></div>
</div>
</body>
</html>
    """, css=BASE_CSS, brand=brand_block("Tracker Dashboard"), form=filter_form(user, business, "/dashboard", campaign, slug, token), nav=nav_links(user, business, campaign, slug, "dashboard", token), stats=stats_block(total, len(conversions), conversion_rate(total, len(conversions)), len(available_campaigns(user, business, token)), len(available_slugs(user, business, campaign, token))), selected_label=selected_label, scans=scans, variant_rows=variant_rows, source_rows=source_rows, city_rows=city_rows, scores=scores, best_campaign=best_campaign, best_source=best_source, best_qr=best_qr, best_city=best_city)


@app.get("/qrs")
def qrs_page():
    gated = identity_gate("qrs")
    if gated is not None:
        return gated
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    token = selected_token_from_request()
    qrs = qr_summary_rows(user, business, campaign, token)
    if slug:
        qrs = [q for q in qrs if clean(q.get("slug"), "default").lower() == slug.lower()]
    scans = filtered_scans(user, business, campaign, slug, token)
    conversions = filtered_conversions(user, business, campaign, slug, token)
    return render_template_string("""
<!doctype html>
<html>
<head><title>Saved QR Codes</title>{{ css | safe }}</head>
<body>
<div class="wrap">
    <div class="hero">{{ brand | safe }}<p class="muted">A clean view of saved and tracked QR codes. Saved QR codes can appear here before their first scan.</p>{{ form | safe }}{{ nav | safe }}</div>
    {{ stats | safe }}
    <div class="qr-card-grid">
    {% for qr in qrs %}
        <div class="qr-card">
            <div class="qr-card-top"><div><p class="qr-card-title">{{ qr.slug }}</p><p><span class="pill">{{ qr.campaign }}</span> <span class="pill">{{ qr.source }}</span> <span class="pill">{{ qr.medium }}</span></p></div><div><strong>{{ qr.scans }}</strong><br><span class="muted">scans</span></div></div>
            <p class="qr-card-sub">{{ qr.destination }}</p>
            <p class="muted">Conversions: <strong>{{ qr.conversions }}</strong> · Rate: <strong>{{ qr.conversion_rate }}%</strong> · Top city: <strong>{{ qr.top_city }}</strong> · Device: <strong>{{ qr.top_device }}</strong></p>
            <div class="qr-actions">
                <a href="/dashboard?user={{ user_q }}&business={{ business_q }}&token={{ token_q }}&campaign={{ qr.campaign_q }}&slug={{ qr.slug_q }}">Dashboard</a>
                <a href="/analytics?user={{ user_q }}&business={{ business_q }}&token={{ token_q }}&campaign={{ qr.campaign_q }}&slug={{ qr.slug_q }}">Analytics</a>
                <a href="/campaigns?user={{ user_q }}&business={{ business_q }}&token={{ token_q }}&campaign={{ qr.campaign_q }}&slug={{ qr.slug_q }}">Campaign</a>
                <a href="/heatmap?user={{ user_q }}&business={{ business_q }}&token={{ token_q }}&campaign={{ qr.campaign_q }}&slug={{ qr.slug_q }}">Heatmap</a>
            </div>
        </div>
    {% else %}
        <div class="card"><p class="muted">No saved QR codes or scans have been registered yet. Save a QR from QR Mode or scan a tracked QR to show it here.</p></div>
    {% endfor %}
    </div>
</div>
</body>
</html>
    """, css=BASE_CSS, brand=brand_block("Saved QR Codes"), form=filter_form(user, business, "/qrs", campaign, slug, token), nav=nav_links(user, business, campaign, slug, "qrs", token), stats=stats_block(len(scans), len(conversions), conversion_rate(len(scans), len(conversions)), len(available_campaigns(user, business, token)), len(qrs)), qrs=[{**q, "slug_q": quote_plus(q["slug"]), "campaign_q": quote_plus(q["campaign"])} for q in qrs], user_q=quote_plus(user), business_q=quote_plus(business), token_q=quote_plus(token))


@app.get("/analytics")
def analytics():
    gated = identity_gate("analytics")
    if gated is not None:
        return gated
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    token = selected_token_from_request()
    scans = filtered_scans(user, business, campaign, slug, token)
    conversions = filtered_conversions(user, business, campaign, slug, token)

    return render_template_string("""
<!doctype html>
<html>
<head><title>QR Mode Tracker Analytics</title><meta http-equiv="refresh" content="45">{{ css | safe }}<script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head>
<body><div class="wrap"><div class="hero">{{ brand | safe }}<p class="muted">Live charts for scans, campaigns, QR codes, A/B variants, conversions, locations, devices and browsers.</p>{{ form | safe }}{{ nav | safe }}</div>
<div class="grid2"><div class="card"><h2>Scans Over Time</h2><canvas id="timeChart"></canvas></div><div class="card"><h2>Conversions Over Time</h2><canvas id="conversionChart"></canvas></div><div class="card"><h2>Campaign Performance</h2><canvas id="campaignChart"></canvas></div><div class="card"><h2>QR Code Performance</h2><canvas id="slugChart"></canvas></div><div class="card"><h2>A/B Variants</h2><canvas id="variantChart"></canvas></div><div class="card"><h2>Source Breakdown</h2><canvas id="sourceChart"></canvas></div><div class="card"><h2>Top Cities</h2><canvas id="cityChart"></canvas></div><div class="card"><h2>Countries</h2><canvas id="countryChart"></canvas></div><div class="card"><h2>Device Types</h2><canvas id="deviceChart"></canvas></div><div class="card"><h2>Browsers</h2><canvas id="browserChart"></canvas></div></div></div>
<script>
const byDay={{ by_day|safe }}, byConversionDay={{ by_conversion_day|safe }}, byCampaign={{ by_campaign|safe }}, bySource={{ by_source|safe }}, bySlug={{ by_slug|safe }}, byDevice={{ by_device|safe }}, byBrowser={{ by_browser|safe }}, byCity={{ by_city|safe }}, byCountry={{ by_country|safe }}, byVariant={{ by_variant|safe }};
function sortedData(obj, limit=12){return Object.entries(obj).sort((a,b)=>b[1]-a[1]).slice(0,limit)}
function makeChart(id,type,title,obj,limit=12){let rows=type==='line'?Object.entries(obj):sortedData(obj,limit);new Chart(document.getElementById(id),{type,data:{labels:rows.map(x=>x[0]),datasets:[{label:title,data:rows.map(x=>x[1]),borderWidth:2,tension:.35,fill:type==='line'}]},options:{responsive:true,plugins:{legend:{display:type!=='bar'&&type!=='line'}},scales:type==='pie'||type==='doughnut'?{}:{y:{beginAtZero:true,ticks:{precision:0}}}}})}
makeChart('timeChart','line','Scans',byDay);makeChart('conversionChart','line','Conversions',byConversionDay);makeChart('campaignChart','bar','Campaigns',byCampaign);makeChart('slugChart','bar','QR Codes',bySlug);makeChart('variantChart','bar','Variants',byVariant);makeChart('sourceChart','doughnut','Sources',bySource);makeChart('cityChart','bar','Cities',byCity);makeChart('countryChart','doughnut','Countries',byCountry);makeChart('deviceChart','pie','Devices',byDevice);makeChart('browserChart','doughnut','Browsers',byBrowser);
</script></body></html>
    """, css=BASE_CSS, brand=brand_block("Advanced Analytics"), form=filter_form(user, business, "/analytics", campaign, slug, selected_token_from_request()), nav=nav_links(user, business, campaign, slug, "analytics", selected_token_from_request()), by_day=json_for_js(count_by_day(scans)), by_conversion_day=json_for_js(count_by_day(conversions)), by_campaign=json_for_js(dict(count_by(filtered_scans(user, business, token=selected_token_from_request()), "campaign", "default"))), by_source=json_for_js(dict(count_by(scans, "source", "qr"))), by_slug=json_for_js(dict(count_by(scans, "slug", "default"))), by_device=json_for_js(dict(count_by(scans, "device", "Unknown"))), by_browser=json_for_js(dict(count_by(scans, "browser", "Unknown"))), by_city=json_for_js(dict(count_by(scans, "city", "Unknown"))), by_country=json_for_js(dict(count_by(scans, "country", "Unknown"))), by_variant=json_for_js(dict(count_by(scans, "variant", "A"))))


@app.get("/campaigns")
def campaigns_page():
    gated = identity_gate("campaigns")
    if gated is not None:
        return gated
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    token = selected_token_from_request()
    all_scans = filtered_scans(user, business, token=token)
    scans = filtered_scans(user, business, campaign, slug, token)
    conversions = filtered_conversions(user, business, campaign, slug, token)
    campaign_names = available_campaigns(user, business, token)
    rows = [(name, len(filtered_scans(user, business, name, token=token))) for name in campaign_names]
    campaign_table = []
    for name, count in rows:
        campaign_scans = filtered_scans(user, business, name, token=token)
        conv_count = len(filtered_conversions(user, business, name, token=token))
        campaign_table.append({"name": name, "count": len(campaign_scans), "qrs": len(available_slugs(user, business, name, token)), "conversions": conv_count, "rate": conversion_rate(len(campaign_scans), conv_count)})

    return render_template_string("""
<!doctype html>
<html><head><title>Campaign Analytics</title>{{ css | safe }}<script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head><body><div class="wrap"><div class="hero">{{ brand | safe }}<p class="muted">Compare campaign groups like Melbourne, Sydney, flyer drops, events and poster runs.</p>{{ form | safe }}{{ nav | safe }}</div>{{ stats | safe }}<div class="grid2"><div class="card"><h2>Campaign Scan Share</h2><canvas id="campaignShare"></canvas></div><div class="card"><h2>Selected Campaign Over Time</h2><canvas id="campaignTime"></canvas></div></div><div class="card"><h2>Campaign Table</h2><table><tr><th>Campaign</th><th>Scans</th><th>QR Codes</th><th>Conversions</th><th>Conversion Rate</th><th>Open</th></tr>{% for row in campaign_table %}<tr><td>{{ row.name }}</td><td>{{ row.count }}</td><td>{{ row.qrs }}</td><td>{{ row.conversions }}</td><td>{{ row.rate }}%</td><td><a class="pill" href="/campaigns?user={{ user_q }}&business={{ business_q }}&token={{ token_q }}&campaign={{ row.name_q }}">View</a></td></tr>{% else %}<tr><td colspan="6" class="muted">No campaigns yet.</td></tr>{% endfor %}</table></div></div><script>const allCampaigns={{ by_campaign|safe }}, selectedTime={{ by_day|safe }};function sortedData(obj,limit=16){return Object.entries(obj).sort((a,b)=>b[1]-a[1]).slice(0,limit)}function chart(id,type,label,obj){const rows=type==='line'?Object.entries(obj):sortedData(obj);new Chart(document.getElementById(id),{type,data:{labels:rows.map(x=>x[0]),datasets:[{label,data:rows.map(x=>x[1]),borderWidth:2,tension:.35,fill:type==='line'}]},options:{scales:type==='doughnut'?{}:{y:{beginAtZero:true,ticks:{precision:0}}}}})}chart('campaignShare','doughnut','Campaigns',allCampaigns);chart('campaignTime','line','Scans',selectedTime);</script></body></html>
    """, css=BASE_CSS, brand=brand_block("Campaign Analytics"), form=filter_form(user, business, "/campaigns", campaign, slug, selected_token_from_request()), nav=nav_links(user, business, campaign, slug, "campaigns", selected_token_from_request()), stats=stats_block(len(scans), len(conversions), conversion_rate(len(scans), len(conversions)), len(rows), len(available_slugs(user, business, campaign, selected_token_from_request()))), campaign_table=[{**r, "name_q": quote_plus(r["name"])} for r in campaign_table], user_q=quote_plus(user), business_q=quote_plus(business), token_q=quote_plus(selected_token_from_request()), by_campaign=json_for_js(dict(rows)), by_day=json_for_js(count_by_day(scans)))


@app.get("/heatmap")
def heatmap_page():
    gated = identity_gate("heatmap")
    if gated is not None:
        return gated
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    token = selected_token_from_request()
    scans = filtered_scans(user, business, campaign, slug, token)
    conversions = filtered_conversions(user, business, campaign, slug, token)
    points = heatmap_points(scans)
    top = points[:12]
    map_query = "Australia"
    if top:
        first = top[0]
        map_query = ", ".join([x for x in [first.get("city"), first.get("region"), first.get("country")] if x and x != "Unknown"])

    return render_template_string("""
<!doctype html>
<html><head><title>Scan Heatmap</title>{{ css | safe }}<script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head><body><div class="wrap"><div class="hero">{{ brand | safe }}<p class="muted">Location-based scan grouping by city, region and country. IP locations are approximate.</p>{{ form | safe }}{{ nav | safe }}</div>{{ stats | safe }}<div class="grid2"><div class="card"><h2>City Heat Chart</h2><canvas id="cityHeat"></canvas></div><div class="card"><h2>Map Preview</h2><div class="map-frame"><iframe loading="lazy" src="https://maps.google.com/maps?q={{ map_query }}&output=embed"></iframe></div></div></div><div class="card"><h2>Top Location Heat Cells</h2><div class="heatmap-grid">{% for p in top %}<div class="heat-cell"><strong>{{ p.count }}</strong><span class="muted">{{ p.city }}{% if p.region %}, {{ p.region }}{% endif %}<br>{{ p.country }}</span></div>{% else %}<p class="muted">No location data yet.</p>{% endfor %}</div></div></div><script>const points={{ points|safe }};const cityCounts={};points.forEach(p=>{cityCounts[`${p.city}, ${p.country}`]=p.count});const rows=Object.entries(cityCounts).sort((a,b)=>b[1]-a[1]).slice(0,16);new Chart(document.getElementById('cityHeat'),{type:'bar',data:{labels:rows.map(x=>x[0]),datasets:[{label:'Scans',data:rows.map(x=>x[1]),borderWidth:2}]},options:{indexAxis:'y',scales:{x:{beginAtZero:true,ticks:{precision:0}}}}});</script></body></html>
    """, css=BASE_CSS, brand=brand_block("Scan Heatmap"), form=filter_form(user, business, "/heatmap", campaign, slug, token), nav=nav_links(user, business, campaign, slug, "heatmap", token), stats=stats_block(len(scans), len(conversions), conversion_rate(len(scans), len(conversions)), len(available_campaigns(user, business, token)), len(available_slugs(user, business, campaign, token))), top=top, points=json_for_js(points), map_query=quote_plus(map_query))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

