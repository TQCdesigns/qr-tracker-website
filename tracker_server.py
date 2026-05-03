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

from flask import Flask, request, redirect, jsonify, render_template_string, Response

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("QR_TRACKER_DATA_DIR", "tracker_data"))
DATA_DIR.mkdir(exist_ok=True)

SCAN_FILE = DATA_DIR / "scans.jsonl"
CONVERSION_FILE = DATA_DIR / "conversions.jsonl"
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


def load_scans() -> list[dict]:
    return load_jsonl(SCAN_FILE)


def load_conversions() -> list[dict]:
    return load_jsonl(CONVERSION_FILE)


def save_scan(scan: dict) -> None:
    append_jsonl(SCAN_FILE, scan)


def save_conversion(conversion: dict) -> None:
    append_jsonl(CONVERSION_FILE, conversion)


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


def filtered_rows(rows: list[dict], user: str, business: str, campaign: str = "", slug: str = "") -> list[dict]:
    user_l = clean(user).lower()
    business_l = clean(business).lower()
    campaign_l = clean(campaign).lower()
    slug_l = clean(slug).lower()

    if user_l:
        rows = [r for r in rows if clean(r.get("user")).lower() == user_l]
    if business_l:
        rows = [r for r in rows if clean(r.get("business")).lower() == business_l]
    if campaign_l:
        rows = [r for r in rows if clean(r.get("campaign"), "default").lower() == campaign_l]
    if slug_l:
        rows = [r for r in rows if clean(r.get("slug"), "default").lower() == slug_l]
    return rows


def filtered_scans(user: str, business: str, campaign: str = "", slug: str = "") -> list[dict]:
    return filtered_rows(load_scans(), user, business, campaign, slug)


def filtered_conversions(user: str, business: str, campaign: str = "", slug: str = "") -> list[dict]:
    return filtered_rows(load_conversions(), user, business, campaign, slug)


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


def available_campaigns(user: str = "", business: str = "") -> list[str]:
    rows = filtered_rows(load_scans(), user, business)
    names = sorted({clean(r.get("campaign"), "default") for r in rows if clean(r.get("campaign"), "default")})
    if "default" not in names:
        names.insert(0, "default")
    return names


def available_slugs(user: str = "", business: str = "", campaign: str = "") -> list[str]:
    rows = filtered_rows(load_scans(), user, business, campaign)
    names = sorted({clean(r.get("slug"), "default") for r in rows if clean(r.get("slug"), "default")})
    if "default" not in names:
        names.insert(0, "default")
    return names


def qr_summary_rows(user: str = "", business: str = "", campaign: str = "") -> list[dict]:
    scans = filtered_scans(user, business, campaign)
    conversions = filtered_conversions(user, business, campaign)
    conv_by_slug = Counter(clean(c.get("slug"), "default") for c in conversions)
    grouped: dict[str, dict] = {}
    for scan in scans:
        slug = clean(scan.get("slug"), "default")
        if slug not in grouped:
            grouped[slug] = {
                "slug": slug,
                "campaign": clean(scan.get("campaign"), "default"),
                "source": clean(scan.get("source"), "qr"),
                "medium": clean(scan.get("medium"), "qr"),
                "destination": clean(scan.get("destination")),
                "first_seen": clean(scan.get("timestamp")),
                "last_seen": clean(scan.get("timestamp")),
                "scans": 0,
                "conversions": 0,
                "cities": Counter(),
                "devices": Counter(),
            }
        row = grouped[slug]
        row["scans"] += 1
        timestamp = clean(scan.get("timestamp"))
        if timestamp:
            row["last_seen"] = max(row["last_seen"], timestamp)
            row["first_seen"] = min(row["first_seen"], timestamp)
        row["cities"][clean(scan.get("city"), "Unknown")] += 1
        row["devices"][clean(scan.get("device"), "Unknown")] += 1

    for slug, row in grouped.items():
        row["conversions"] = conv_by_slug.get(slug, 0)
        row["conversion_rate"] = conversion_rate(row["scans"], row["conversions"])
        row["top_city"] = row["cities"].most_common(1)[0][0] if row["cities"] else "Unknown"
        row["top_device"] = row["devices"].most_common(1)[0][0] if row["devices"] else "Unknown"
        row.pop("cities", None)
        row.pop("devices", None)
    return sorted(grouped.values(), key=lambda x: x["scans"], reverse=True)


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
# Tracking routes
# -----------------------------
@app.get("/")
def home():
    return """
    <h1>QR Tracker</h1>
    <p>Tracker is running.</p>
    <p><a href="/qr-track-health">Health Check</a></p>
    <p><a href="/dashboard">Dashboard</a></p>
    <p><a href="/qrs">QR Code Library</a></p>
    <p><a href="/analytics">Analytics</a></p>
    <p><a href="/campaigns">Campaign Analytics</a></p>
    <p><a href="/heatmap">Location Heatmap</a></p>
    """


@app.get("/qr-track-health")
def qr_track_health():
    return jsonify({
        "status": "ok",
        "tracking": True,
        "version": "4.3",
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
        ],
    })


@app.get("/qr-track")
def qr_track():
    destination = clean(request.args.get("url"))
    if not destination:
        return "Missing destination URL.", 400

    destination = unquote(destination)
    if not destination.startswith(("http://", "https://")):
        destination = "https://" + destination

    user_agent = request.headers.get("User-Agent", "")
    ip = get_ip()
    geo = lookup_geo(ip)

    scan = {
        "id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "user": clean(request.args.get("user"), "unknown"),
        "business": clean(request.args.get("business"), "unknown"),
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
    payload = {
        "id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "user": clean(request.args.get("user"), "unknown"),
        "business": clean(request.args.get("business"), "unknown"),
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
    scans = filtered_scans(user, business, campaign, slug)
    return jsonify({"count": len(scans), "scans": scans})


@app.get("/api/campaigns")
def api_campaigns():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaigns = available_campaigns(user, business)
    return jsonify({"campaigns": campaigns, "count": len(campaigns)})


@app.get("/api/qrs")
def api_qrs():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    rows = qr_summary_rows(user, business, campaign)
    return jsonify({"count": len(rows), "qrs": rows})


@app.get("/api/summary")
def api_summary():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    scans = filtered_scans(user, business, campaign, slug)
    conversions = filtered_conversions(user, business, campaign, slug)

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
        "available_campaigns": available_campaigns(user, business),
        "available_qr_codes": available_slugs(user, business, campaign),
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
        "qr_codes": qr_summary_rows(user, business, campaign),
        "campaign_performance": campaign_scores(filtered_scans(user, business)),
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
    scans = filtered_scans(user, business, campaign, slug)

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
</style>
"""


def campaign_dropdown(user: str, business: str, selected: str = "") -> str:
    campaigns = available_campaigns(user, business)
    options = ['<option value="">All Campaigns</option>']
    for campaign in campaigns:
        selected_attr = "selected" if campaign == selected else ""
        options.append(f'<option value="{safe(campaign)}" {selected_attr}>{safe(campaign)}</option>')
    return "\n".join(options)


def slug_dropdown(user: str, business: str, campaign: str = "", selected: str = "") -> str:
    slugs = available_slugs(user, business, campaign)
    options = ['<option value="">All QR Codes</option>']
    for slug in slugs:
        selected_attr = "selected" if slug == selected else ""
        options.append(f'<option value="{safe(slug)}" {selected_attr}>{safe(slug)}</option>')
    return "\n".join(options)


def filter_form(user: str, business: str, action: str, campaign: str = "", slug: str = "") -> str:
    export_url = f"/export.csv?user={quote_plus(user)}&business={quote_plus(business)}&campaign={quote_plus(campaign)}&slug={quote_plus(slug)}"
    return f"""
    <form method="get" action="{action}">
        <input name="user" placeholder="Name" value="{safe(user)}">
        <input name="business" placeholder="Business name" value="{safe(business)}">
        <select name="campaign">{campaign_dropdown(user, business, campaign)}</select>
        <select name="slug">{slug_dropdown(user, business, campaign, slug)}</select>
        <button type="submit">View Stats</button>
        <a class="btn" href="{export_url}">Export CSV</a>
    </form>
    """


def nav_links(user: str, business: str, campaign: str, slug: str, active: str) -> str:
    q = f"user={quote_plus(user)}&business={quote_plus(business)}&campaign={quote_plus(campaign)}&slug={quote_plus(slug)}"
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
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()

    scans = filtered_scans(user, business, campaign, slug)
    conversions = filtered_conversions(user, business, campaign, slug)
    all_scans_for_campaigns = filtered_scans(user, business)

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
    <title>QR Tracker Dashboard</title>
    <meta http-equiv="refresh" content="30">
    {{ css | safe }}
</head>
<body>
<div class="wrap">
    <div class="hero">
        <div class="top"><div><h1>QR Tracker Dashboard</h1><p class="muted">Live QR scans, campaign performance, A/B variants, devices and locations.</p></div><span class="pill">{{ selected_label }}</span></div>
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
    """, css=BASE_CSS, form=filter_form(user, business, "/dashboard", campaign, slug), nav=nav_links(user, business, campaign, slug, "dashboard"), stats=stats_block(total, len(conversions), conversion_rate(total, len(conversions)), len(campaign_rows), len(slug_rows)), selected_label=selected_label, scans=scans, variant_rows=variant_rows, source_rows=source_rows, city_rows=city_rows, scores=scores, best_campaign=best_campaign, best_source=best_source, best_qr=best_qr, best_city=best_city)


@app.get("/qrs")
def qrs_page():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    qrs = qr_summary_rows(user, business, campaign)
    if slug:
        qrs = [q for q in qrs if clean(q.get("slug"), "default").lower() == slug.lower()]
    scans = filtered_scans(user, business, campaign, slug)
    conversions = filtered_conversions(user, business, campaign, slug)
    return render_template_string("""
<!doctype html>
<html>
<head><title>Saved QR Codes</title>{{ css | safe }}</head>
<body>
<div class="wrap">
    <div class="hero"><h1>Saved QR Codes</h1><p class="muted">A clean view of every QR code currently being tracked. Open dashboard, analytics, campaign view or heatmap for each QR.</p>{{ form | safe }}{{ nav | safe }}</div>
    {{ stats | safe }}
    <div class="qr-card-grid">
    {% for qr in qrs %}
        <div class="qr-card">
            <div class="qr-card-top"><div><p class="qr-card-title">{{ qr.slug }}</p><p><span class="pill">{{ qr.campaign }}</span> <span class="pill">{{ qr.source }}</span> <span class="pill">{{ qr.medium }}</span></p></div><div><strong>{{ qr.scans }}</strong><br><span class="muted">scans</span></div></div>
            <p class="qr-card-sub">{{ qr.destination }}</p>
            <p class="muted">Conversions: <strong>{{ qr.conversions }}</strong> · Rate: <strong>{{ qr.conversion_rate }}%</strong> · Top city: <strong>{{ qr.top_city }}</strong> · Device: <strong>{{ qr.top_device }}</strong></p>
            <div class="qr-actions">
                <a href="/dashboard?user={{ user_q }}&business={{ business_q }}&campaign={{ qr.campaign_q }}&slug={{ qr.slug_q }}">Dashboard</a>
                <a href="/analytics?user={{ user_q }}&business={{ business_q }}&campaign={{ qr.campaign_q }}&slug={{ qr.slug_q }}">Analytics</a>
                <a href="/campaigns?user={{ user_q }}&business={{ business_q }}&campaign={{ qr.campaign_q }}&slug={{ qr.slug_q }}">Campaign</a>
                <a href="/heatmap?user={{ user_q }}&business={{ business_q }}&campaign={{ qr.campaign_q }}&slug={{ qr.slug_q }}">Heatmap</a>
            </div>
        </div>
    {% else %}
        <div class="card"><p class="muted">No QR scans have been recorded yet. Scan one of your saved QR codes and it will appear here.</p></div>
    {% endfor %}
    </div>
</div>
</body>
</html>
    """, css=BASE_CSS, form=filter_form(user, business, "/qrs", campaign, slug), nav=nav_links(user, business, campaign, slug, "qrs"), stats=stats_block(len(scans), len(conversions), conversion_rate(len(scans), len(conversions)), len(count_by(filtered_scans(user, business), "campaign", "default")), len(qrs)), qrs=[{**q, "slug_q": quote_plus(q["slug"]), "campaign_q": quote_plus(q["campaign"])} for q in qrs], user_q=quote_plus(user), business_q=quote_plus(business))


@app.get("/analytics")
def analytics():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    scans = filtered_scans(user, business, campaign, slug)
    conversions = filtered_conversions(user, business, campaign, slug)

    return render_template_string("""
<!doctype html>
<html>
<head><title>QR Tracker Analytics</title><meta http-equiv="refresh" content="45">{{ css | safe }}<script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head>
<body><div class="wrap"><div class="hero"><h1>Advanced Analytics</h1><p class="muted">Live charts for scans, campaigns, QR codes, A/B variants, conversions, locations, devices and browsers.</p>{{ form | safe }}{{ nav | safe }}</div>
<div class="grid2"><div class="card"><h2>Scans Over Time</h2><canvas id="timeChart"></canvas></div><div class="card"><h2>Conversions Over Time</h2><canvas id="conversionChart"></canvas></div><div class="card"><h2>Campaign Performance</h2><canvas id="campaignChart"></canvas></div><div class="card"><h2>QR Code Performance</h2><canvas id="slugChart"></canvas></div><div class="card"><h2>A/B Variants</h2><canvas id="variantChart"></canvas></div><div class="card"><h2>Source Breakdown</h2><canvas id="sourceChart"></canvas></div><div class="card"><h2>Top Cities</h2><canvas id="cityChart"></canvas></div><div class="card"><h2>Countries</h2><canvas id="countryChart"></canvas></div><div class="card"><h2>Device Types</h2><canvas id="deviceChart"></canvas></div><div class="card"><h2>Browsers</h2><canvas id="browserChart"></canvas></div></div></div>
<script>
const byDay={{ by_day|safe }}, byConversionDay={{ by_conversion_day|safe }}, byCampaign={{ by_campaign|safe }}, bySource={{ by_source|safe }}, bySlug={{ by_slug|safe }}, byDevice={{ by_device|safe }}, byBrowser={{ by_browser|safe }}, byCity={{ by_city|safe }}, byCountry={{ by_country|safe }}, byVariant={{ by_variant|safe }};
function sortedData(obj, limit=12){return Object.entries(obj).sort((a,b)=>b[1]-a[1]).slice(0,limit)}
function makeChart(id,type,title,obj,limit=12){let rows=type==='line'?Object.entries(obj):sortedData(obj,limit);new Chart(document.getElementById(id),{type,data:{labels:rows.map(x=>x[0]),datasets:[{label:title,data:rows.map(x=>x[1]),borderWidth:2,tension:.35,fill:type==='line'}]},options:{responsive:true,plugins:{legend:{display:type!=='bar'&&type!=='line'}},scales:type==='pie'||type==='doughnut'?{}:{y:{beginAtZero:true,ticks:{precision:0}}}}})}
makeChart('timeChart','line','Scans',byDay);makeChart('conversionChart','line','Conversions',byConversionDay);makeChart('campaignChart','bar','Campaigns',byCampaign);makeChart('slugChart','bar','QR Codes',bySlug);makeChart('variantChart','bar','Variants',byVariant);makeChart('sourceChart','doughnut','Sources',bySource);makeChart('cityChart','bar','Cities',byCity);makeChart('countryChart','doughnut','Countries',byCountry);makeChart('deviceChart','pie','Devices',byDevice);makeChart('browserChart','doughnut','Browsers',byBrowser);
</script></body></html>
    """, css=BASE_CSS, form=filter_form(user, business, "/analytics", campaign, slug), nav=nav_links(user, business, campaign, slug, "analytics"), by_day=json_for_js(count_by_day(scans)), by_conversion_day=json_for_js(count_by_day(conversions)), by_campaign=json_for_js(dict(count_by(filtered_scans(user, business), "campaign", "default"))), by_source=json_for_js(dict(count_by(scans, "source", "qr"))), by_slug=json_for_js(dict(count_by(scans, "slug", "default"))), by_device=json_for_js(dict(count_by(scans, "device", "Unknown"))), by_browser=json_for_js(dict(count_by(scans, "browser", "Unknown"))), by_city=json_for_js(dict(count_by(scans, "city", "Unknown"))), by_country=json_for_js(dict(count_by(scans, "country", "Unknown"))), by_variant=json_for_js(dict(count_by(scans, "variant", "A"))))


@app.get("/campaigns")
def campaigns_page():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    all_scans = filtered_scans(user, business)
    scans = filtered_scans(user, business, campaign, slug)
    conversions = filtered_conversions(user, business, campaign, slug)
    rows = count_by(all_scans, "campaign", "default")
    campaign_table = []
    for name, count in rows:
        campaign_scans = filtered_scans(user, business, name)
        conv_count = len(filtered_conversions(user, business, name))
        campaign_table.append({"name": name, "count": len(campaign_scans), "qrs": len(count_by(campaign_scans, "slug", "default")), "conversions": conv_count, "rate": conversion_rate(len(campaign_scans), conv_count)})

    return render_template_string("""
<!doctype html>
<html><head><title>Campaign Analytics</title>{{ css | safe }}<script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head><body><div class="wrap"><div class="hero"><h1>Campaign Analytics</h1><p class="muted">Compare campaign groups like Melbourne, Sydney, flyer drops, events and poster runs.</p>{{ form | safe }}{{ nav | safe }}</div>{{ stats | safe }}<div class="grid2"><div class="card"><h2>Campaign Scan Share</h2><canvas id="campaignShare"></canvas></div><div class="card"><h2>Selected Campaign Over Time</h2><canvas id="campaignTime"></canvas></div></div><div class="card"><h2>Campaign Table</h2><table><tr><th>Campaign</th><th>Scans</th><th>QR Codes</th><th>Conversions</th><th>Conversion Rate</th><th>Open</th></tr>{% for row in campaign_table %}<tr><td>{{ row.name }}</td><td>{{ row.count }}</td><td>{{ row.qrs }}</td><td>{{ row.conversions }}</td><td>{{ row.rate }}%</td><td><a class="pill" href="/campaigns?user={{ user_q }}&business={{ business_q }}&campaign={{ row.name_q }}">View</a></td></tr>{% else %}<tr><td colspan="6" class="muted">No campaigns yet.</td></tr>{% endfor %}</table></div></div><script>const allCampaigns={{ by_campaign|safe }}, selectedTime={{ by_day|safe }};function sortedData(obj,limit=16){return Object.entries(obj).sort((a,b)=>b[1]-a[1]).slice(0,limit)}function chart(id,type,label,obj){const rows=type==='line'?Object.entries(obj):sortedData(obj);new Chart(document.getElementById(id),{type,data:{labels:rows.map(x=>x[0]),datasets:[{label,data:rows.map(x=>x[1]),borderWidth:2,tension:.35,fill:type==='line'}]},options:{scales:type==='doughnut'?{}:{y:{beginAtZero:true,ticks:{precision:0}}}}})}chart('campaignShare','doughnut','Campaigns',allCampaigns);chart('campaignTime','line','Scans',selectedTime);</script></body></html>
    """, css=BASE_CSS, form=filter_form(user, business, "/campaigns", campaign, slug), nav=nav_links(user, business, campaign, slug, "campaigns"), stats=stats_block(len(scans), len(conversions), conversion_rate(len(scans), len(conversions)), len(rows), len(count_by(scans, "slug", "default"))), campaign_table=[{**r, "name_q": quote_plus(r["name"])} for r in campaign_table], user_q=quote_plus(user), business_q=quote_plus(business), by_campaign=json_for_js(dict(rows)), by_day=json_for_js(count_by_day(scans)))


@app.get("/heatmap")
def heatmap_page():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    campaign = selected_campaign_from_request()
    slug = selected_slug_from_request()
    scans = filtered_scans(user, business, campaign, slug)
    conversions = filtered_conversions(user, business, campaign, slug)
    points = heatmap_points(scans)
    top = points[:12]
    map_query = "Australia"
    if top:
        first = top[0]
        map_query = ", ".join([x for x in [first.get("city"), first.get("region"), first.get("country")] if x and x != "Unknown"])

    return render_template_string("""
<!doctype html>
<html><head><title>Scan Heatmap</title>{{ css | safe }}<script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head><body><div class="wrap"><div class="hero"><h1>Scan Heatmap</h1><p class="muted">Location-based scan grouping by city, region and country. IP locations are approximate.</p>{{ form | safe }}{{ nav | safe }}</div>{{ stats | safe }}<div class="grid2"><div class="card"><h2>City Heat Chart</h2><canvas id="cityHeat"></canvas></div><div class="card"><h2>Map Preview</h2><div class="map-frame"><iframe loading="lazy" src="https://maps.google.com/maps?q={{ map_query }}&output=embed"></iframe></div></div></div><div class="card"><h2>Top Location Heat Cells</h2><div class="heatmap-grid">{% for p in top %}<div class="heat-cell"><strong>{{ p.count }}</strong><span class="muted">{{ p.city }}{% if p.region %}, {{ p.region }}{% endif %}<br>{{ p.country }}</span></div>{% else %}<p class="muted">No location data yet.</p>{% endfor %}</div></div></div><script>const points={{ points|safe }};const cityCounts={};points.forEach(p=>{cityCounts[`${p.city}, ${p.country}`]=p.count});const rows=Object.entries(cityCounts).sort((a,b)=>b[1]-a[1]).slice(0,16);new Chart(document.getElementById('cityHeat'),{type:'bar',data:{labels:rows.map(x=>x[0]),datasets:[{label:'Scans',data:rows.map(x=>x[1]),borderWidth:2}]},options:{indexAxis:'y',scales:{x:{beginAtZero:true,ticks:{precision:0}}}}});</script></body></html>
    """, css=BASE_CSS, form=filter_form(user, business, "/heatmap", campaign, slug), nav=nav_links(user, business, campaign, slug, "heatmap"), stats=stats_block(len(scans), len(conversions), conversion_rate(len(scans), len(conversions)), len(count_by(filtered_scans(user, business), "campaign", "default")), len(count_by(scans, "slug", "default"))), top=top, points=json_for_js(points), map_query=quote_plus(map_query))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
