from __future__ import annotations

import csv
import json
import os
import uuid
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from flask import Flask, request, redirect, jsonify, render_template_string, Response

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("QR_TRACKER_DATA_DIR", "tracker_data"))
DATA_DIR.mkdir(exist_ok=True)

SCAN_FILE = DATA_DIR / "scans.jsonl"
CONVERSION_FILE = DATA_DIR / "conversions.jsonl"
GEO_CACHE_FILE = DATA_DIR / "geo_cache.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(value: str | None, fallback: str = "") -> str:
    return (value or fallback or "").strip()


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
    if "chrome" in ua:
        return "Chrome"
    if "safari" in ua and "chrome" not in ua:
        return "Safari"
    if "firefox" in ua:
        return "Firefox"
    return "Other"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    rows = []
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
        return {"country": "Local", "region": "Local", "city": "Local"}

    cache = load_geo_cache()
    if ip in cache:
        return cache[ip]

    geo = {"country": "Unknown", "region": "Unknown", "city": "Unknown"}

    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city"
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if payload.get("status") == "success":
            geo = {
                "country": clean(payload.get("country"), "Unknown"),
                "region": clean(payload.get("regionName"), "Unknown"),
                "city": clean(payload.get("city"), "Unknown"),
            }
    except Exception:
        pass

    cache[ip] = geo
    save_geo_cache(cache)
    return geo


def filtered_scans(user: str, business: str) -> list[dict]:
    user = clean(user).lower()
    business = clean(business).lower()
    scans = load_scans()

    if user:
        scans = [s for s in scans if clean(s.get("user")).lower() == user]
    if business:
        scans = [s for s in scans if clean(s.get("business")).lower() == business]

    return scans


def filtered_conversions(user: str, business: str) -> list[dict]:
    user = clean(user).lower()
    business = clean(business).lower()
    conversions = load_conversions()

    if user:
        conversions = [c for c in conversions if clean(c.get("user")).lower() == user]
    if business:
        conversions = [c for c in conversions if clean(c.get("business")).lower() == business]

    return conversions


def count_by(rows: list[dict], key: str, fallback: str = "uncategorised") -> list[tuple[str, int]]:
    counter = Counter(clean(row.get(key), fallback) for row in rows)
    return counter.most_common()


def count_by_day(rows: list[dict]) -> dict[str, int]:
    result = defaultdict(int)
    for row in rows:
        day = clean(row.get("timestamp"))[:10] or "unknown"
        result[day] += 1
    return dict(sorted(result.items()))


def campaign_scores(scans: list[dict]) -> dict[str, float]:
    rows = count_by(scans, "campaign", "default")
    total = sum(v for _, v in rows) or 1
    return {k: round((v / total) * 100, 2) for k, v in rows}


def json_for_js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def conversion_rate(scan_count: int, conversion_count: int) -> float:
    if scan_count <= 0:
        return 0.0
    return round((conversion_count / scan_count) * 100, 2)


@app.get("/")
def home():
    return """
    <h1>QR Tracker</h1>
    <p>Tracker is running.</p>
    <p><a href="/qr-track-health">Health Check</a></p>
    <p><a href="/dashboard">Dashboard</a></p>
    <p><a href="/analytics">Analytics</a></p>
    """


@app.get("/qr-track-health")
def qr_track_health():
    return jsonify({
        "status": "ok",
        "tracking": True,
        "version": "4.0",
        "features": [
            "user filtering",
            "business filtering",
            "campaign tracking",
            "source tracking",
            "medium tracking",
            "slug tracking",
            "A/B variant tracking",
            "conversion tracking",
            "geo tracking",
            "device detection",
            "browser detection",
            "live dashboard refresh",
            "analytics charts",
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

    scan_id = str(uuid.uuid4())

    scan = {
        "id": scan_id,
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


@app.get("/api/scans")
def api_scans():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    scans = filtered_scans(user, business)

    return jsonify({
        "count": len(scans),
        "scans": scans,
    })


@app.get("/api/summary")
def api_summary():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))

    scans = filtered_scans(user, business)
    conversions = filtered_conversions(user, business)

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

    best_campaign = max(by_campaign, key=by_campaign.get) if by_campaign else None
    best_source = max(by_source, key=by_source.get) if by_source else None
    best_device = max(by_device, key=by_device.get) if by_device else None
    best_day = max(by_day, key=by_day.get) if by_day else None
    best_city = max(by_city, key=by_city.get) if by_city else None

    return jsonify({
        "total_scans": len(scans),
        "total_conversions": len(conversions),
        "conversion_rate": conversion_rate(len(scans), len(conversions)),
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
        "campaign_performance": campaign_scores(scans),
        "insights": {
            "best_campaign": best_campaign,
            "best_source": best_source,
            "best_device": best_device,
            "best_day": best_day,
            "best_city": best_city,
        },
    })


@app.get("/export.csv")
def export_csv():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))
    scans = filtered_scans(user, business)

    fields = [
        "timestamp",
        "user",
        "business",
        "campaign",
        "source",
        "medium",
        "variant",
        "slug",
        "notes",
        "destination",
        "country",
        "region",
        "city",
        "device",
        "browser",
        "ip",
        "user_agent",
    ]

    def generate():
        yield ",".join(fields) + "\n"
        for scan in scans:
            row = []
            for field in fields:
                value = str(scan.get(field, "")).replace('"', '""')
                row.append(f'"{value}"')
            yield ",".join(row) + "\n"

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=qr_scans.csv"},
    )


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

    .wrap {
        max-width: 1280px;
        margin: 0 auto;
        padding: 32px;
    }

    .hero, .card, .stat {
        background: var(--card);
        border: 1px solid rgba(255,255,255,.92);
        box-shadow: 0 24px 70px rgba(59,130,246,.13);
        backdrop-filter: blur(18px);
    }

    .hero {
        border-radius: 32px;
        padding: 26px;
        margin-bottom: 20px;
    }

    .card {
        border-radius: 28px;
        padding: 22px;
        margin-bottom: 20px;
    }

    .top {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 14px;
        flex-wrap: wrap;
    }

    h1 {
        margin: 0;
        font-size: 38px;
        letter-spacing: -.9px;
    }

    h2 {
        margin-top: 0;
        font-size: 20px;
    }

    .muted {
        color: var(--muted);
        font-weight: 650;
        line-height: 1.45;
    }

    form {
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        margin-top: 18px;
    }

    input {
        padding: 14px 16px;
        border-radius: 16px;
        border: 1px solid #cbd5e1;
        font-weight: 750;
        min-width: 220px;
        background: rgba(255,255,255,.94);
    }

    button, a.btn {
        padding: 14px 18px;
        border-radius: 16px;
        border: none;
        background: linear-gradient(135deg, var(--cyan), var(--purple));
        color: white;
        font-weight: 950;
        text-decoration: none;
        cursor: pointer;
    }

    .nav {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin-top: 16px;
    }

    .nav a {
        padding: 10px 14px;
        border-radius: 999px;
        background: rgba(255,255,255,.82);
        color: var(--blue);
        text-decoration: none;
        font-weight: 900;
        border: 1px solid #dbeafe;
    }

    .nav a.active {
        color: white;
        background: linear-gradient(135deg, var(--cyan), var(--purple));
        border: none;
    }

    .stats {
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: 16px;
        margin-bottom: 20px;
    }

    .stat {
        border-radius: 24px;
        padding: 20px;
    }

    .stat strong {
        display: block;
        font-size: 32px;
        margin-top: 4px;
    }

    .grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 18px;
    }

    .grid2 {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 18px;
    }

    table {
        width: 100%;
        border-collapse: collapse;
        background: white;
        border-radius: 18px;
        overflow: hidden;
    }

    th, td {
        padding: 12px;
        border-bottom: 1px solid #e2e8f0;
        text-align: left;
        font-size: 14px;
    }

    th {
        background: #f8fafc;
        color: #334155;
    }

    .pill {
        display: inline-block;
        background: #ecfeff;
        color: var(--teal);
        border-radius: 999px;
        padding: 6px 10px;
        font-weight: 850;
        font-size: 12px;
    }

    .score-bar {
        background: #e2e8f0;
        height: 10px;
        border-radius: 999px;
        overflow: hidden;
        margin-top: 8px;
    }

    .score-fill {
        height: 100%;
        border-radius: 999px;
        background: linear-gradient(135deg, var(--cyan), var(--purple));
    }

    canvas {
        width: 100% !important;
        max-height: 360px;
    }

    @media (max-width: 1000px) {
        .stats, .grid, .grid2 {
            grid-template-columns: 1fr;
        }
    }
</style>
"""


def filter_form(user: str, business: str, action: str) -> str:
    return f"""
    <form method="get" action="{action}">
        <input name="user" placeholder="Name" value="{user}">
        <input name="business" placeholder="Business name" value="{business}">
        <button type="submit">View Stats</button>
        <a class="btn" href="/export.csv?user={user}&business={business}">Export CSV</a>
    </form>
    """


def nav_links(user: str, business: str, active: str) -> str:
    dash_active = "active" if active == "dashboard" else ""
    analytics_active = "active" if active == "analytics" else ""
    return f"""
    <div class="nav">
        <a class="{dash_active}" href="/dashboard?user={user}&business={business}">Dashboard</a>
        <a class="{analytics_active}" href="/analytics?user={user}&business={business}">Analytics Charts</a>
        <a href="/api/summary?user={user}&business={business}">API Summary</a>
    </div>
    """


@app.get("/dashboard")
def dashboard():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))

    scans = filtered_scans(user, business)
    conversions = filtered_conversions(user, business)
    total = len(scans)

    campaign_rows = count_by(scans, "campaign", "default")
    source_rows = count_by(scans, "source", "qr")
    slug_rows = count_by(scans, "slug", "default")
    device_rows = count_by(scans, "device", "Unknown")
    city_rows = count_by(scans, "city", "Unknown")
    variant_rows = count_by(scans, "variant", "A")
    scores = campaign_scores(scans)

    best_campaign = campaign_rows[0][0] if campaign_rows else "None yet"
    best_source = source_rows[0][0] if source_rows else "None yet"
    best_qr = slug_rows[0][0] if slug_rows else "None yet"
    best_city = city_rows[0][0] if city_rows else "None yet"

    return render_template_string("""
<!doctype html>
<html>
<head>
    <title>QR Tracker Dashboard</title>
    <meta http-equiv="refresh" content="30">
    """ + BASE_CSS + """
</head>
<body>
<div class="wrap">
    <div class="hero">
        <div class="top">
            <div>
                <h1>QR Tracker Dashboard</h1>
                <p class="muted">Live QR scans, campaign performance, A/B variants, devices and locations.</p>
            </div>
        </div>

        """ + filter_form(user, business, "/dashboard") + nav_links(user, business, "dashboard") + """
    </div>

    <div class="stats">
        <div class="stat"><span class="muted">Total scans</span><strong>{{ total }}</strong></div>
        <div class="stat"><span class="muted">Conversions</span><strong>{{ conversions_count }}</strong></div>
        <div class="stat"><span class="muted">Conv. rate</span><strong>{{ conv_rate }}%</strong></div>
        <div class="stat"><span class="muted">Campaigns</span><strong>{{ campaign_count }}</strong></div>
        <div class="stat"><span class="muted">QR codes</span><strong>{{ slug_count }}</strong></div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>Top Insights</h2>
            <p><strong>Best Campaign:</strong> <span class="pill">{{ best_campaign }}</span></p>
            <p><strong>Best Source:</strong> <span class="pill">{{ best_source }}</span></p>
            <p><strong>Top QR:</strong> <span class="pill">{{ best_qr }}</span></p>
            <p><strong>Best City:</strong> <span class="pill">{{ best_city }}</span></p>
        </div>

        <div class="card">
            <h2>Growth Tip</h2>
            <p class="muted">Duplicate your best campaign and test a new city, poster, frame, QR colour, or call-to-action.</p>
        </div>

        <div class="card">
            <h2>A/B Testing</h2>
            {% for name, count in variant_rows %}
                <p><span class="pill">Variant {{ name }}</span> — {{ count }}</p>
            {% else %}
                <p class="muted">No variant data yet.</p>
            {% endfor %}
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>Campaign Scores</h2>
            {% for name, score in scores.items() %}
                <p><span class="pill">{{ name }}</span> — {{ score }}%</p>
                <div class="score-bar"><div class="score-fill" style="width: {{ score }}%;"></div></div>
            {% else %}
                <p class="muted">No campaign data yet.</p>
            {% endfor %}
        </div>

        <div class="card">
            <h2>Sources</h2>
            {% for name, count in source_rows %}
                <p><span class="pill">{{ name }}</span> — {{ count }}</p>
            {% else %}
                <p class="muted">No source data yet.</p>
            {% endfor %}
        </div>

        <div class="card">
            <h2>Locations</h2>
            {% for name, count in city_rows %}
                <p><span class="pill">{{ name }}</span> — {{ count }}</p>
            {% else %}
                <p class="muted">No location data yet.</p>
            {% endfor %}
        </div>
    </div>

    <div class="card">
        <h2>Recent scans</h2>
        <table>
            <tr>
                <th>Time</th>
                <th>Campaign</th>
                <th>Source</th>
                <th>Variant</th>
                <th>City</th>
                <th>Device</th>
                <th>Browser</th>
                <th>Slug</th>
                <th>Destination</th>
            </tr>
            {% for scan in scans[-60:][::-1] %}
            <tr>
                <td>{{ scan.timestamp }}</td>
                <td>{{ scan.campaign }}</td>
                <td>{{ scan.source }}</td>
                <td>{{ scan.variant }}</td>
                <td>{{ scan.city }}</td>
                <td>{{ scan.device }}</td>
                <td>{{ scan.browser }}</td>
                <td>{{ scan.slug }}</td>
                <td>{{ scan.destination }}</td>
            </tr>
            {% else %}
            <tr><td colspan="9" class="muted">No scans yet.</td></tr>
            {% endfor %}
        </table>
    </div>
</div>
</body>
</html>
    """,
        user=user,
        business=business,
        scans=scans,
        total=total,
        conversions_count=len(conversions),
        conv_rate=conversion_rate(total, len(conversions)),
        campaign_rows=campaign_rows,
        source_rows=source_rows,
        slug_rows=slug_rows,
        device_rows=device_rows,
        city_rows=city_rows,
        variant_rows=variant_rows,
        scores=scores,
        campaign_count=len(campaign_rows),
        source_count=len(source_rows),
        slug_count=len(slug_rows),
        best_campaign=best_campaign,
        best_source=best_source,
        best_qr=best_qr,
        best_city=best_city,
    )


@app.get("/analytics")
def analytics():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))

    scans = filtered_scans(user, business)
    conversions = filtered_conversions(user, business)

    by_day = count_by_day(scans)
    by_campaign = dict(count_by(scans, "campaign", "default"))
    by_source = dict(count_by(scans, "source", "qr"))
    by_slug = dict(count_by(scans, "slug", "default"))
    by_device = dict(count_by(scans, "device", "Unknown"))
    by_browser = dict(count_by(scans, "browser", "Unknown"))
    by_city = dict(count_by(scans, "city", "Unknown"))
    by_country = dict(count_by(scans, "country", "Unknown"))
    by_variant = dict(count_by(scans, "variant", "A"))
    by_conversion_day = count_by_day(conversions)

    return render_template_string("""
<!doctype html>
<html>
<head>
    <title>QR Tracker Analytics</title>
    <meta http-equiv="refresh" content="45">
    """ + BASE_CSS + """
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
<div class="wrap">
    <div class="hero">
        <h1>Advanced Analytics</h1>
        <p class="muted">Live charts for scans, campaigns, A/B variants, conversions, locations, devices and browsers.</p>

        """ + filter_form(user, business, "/analytics") + nav_links(user, business, "analytics") + """
    </div>

    <div class="grid2">
        <div class="card"><h2>Scans Over Time</h2><canvas id="timeChart"></canvas></div>
        <div class="card"><h2>Conversions Over Time</h2><canvas id="conversionChart"></canvas></div>
        <div class="card"><h2>Campaign Performance</h2><canvas id="campaignChart"></canvas></div>
        <div class="card"><h2>A/B Variants</h2><canvas id="variantChart"></canvas></div>
        <div class="card"><h2>Source Breakdown</h2><canvas id="sourceChart"></canvas></div>
        <div class="card"><h2>Top QR Codes</h2><canvas id="slugChart"></canvas></div>
        <div class="card"><h2>Top Cities</h2><canvas id="cityChart"></canvas></div>
        <div class="card"><h2>Countries</h2><canvas id="countryChart"></canvas></div>
        <div class="card"><h2>Device Types</h2><canvas id="deviceChart"></canvas></div>
        <div class="card"><h2>Browsers</h2><canvas id="browserChart"></canvas></div>
    </div>
</div>

<script>
const byDay = {{ by_day | safe }};
const byConversionDay = {{ by_conversion_day | safe }};
const byCampaign = {{ by_campaign | safe }};
const bySource = {{ by_source | safe }};
const bySlug = {{ by_slug | safe }};
const byDevice = {{ by_device | safe }};
const byBrowser = {{ by_browser | safe }};
const byCity = {{ by_city | safe }};
const byCountry = {{ by_country | safe }};
const byVariant = {{ by_variant | safe }};

function sortedData(obj, limit = 12) {
    return Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, limit);
}

function makeChart(id, type, title, obj, limit = 12) {
    let rows = type === "line"
        ? Object.entries(obj)
        : sortedData(obj, limit);

    const labels = rows.map(x => x[0]);
    const values = rows.map(x => x[1]);

    new Chart(document.getElementById(id), {
        type: type,
        data: {
            labels: labels,
            datasets: [{
                label: title,
                data: values,
                borderWidth: 2,
                tension: 0.35,
                fill: type === "line"
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: { display: type !== "bar" && type !== "line" }
            },
            scales: type === "pie" || type === "doughnut" ? {} : {
                y: { beginAtZero: true, ticks: { precision: 0 } }
            }
        }
    });
}

makeChart("timeChart", "line", "Scans", byDay);
makeChart("conversionChart", "line", "Conversions", byConversionDay);
makeChart("campaignChart", "bar", "Campaigns", byCampaign);
makeChart("variantChart", "bar", "Variants", byVariant);
makeChart("sourceChart", "doughnut", "Sources", bySource);
makeChart("slugChart", "bar", "QR Codes", bySlug);
makeChart("cityChart", "bar", "Cities", byCity);
makeChart("countryChart", "doughnut", "Countries", byCountry);
makeChart("deviceChart", "pie", "Devices", byDevice);
makeChart("browserChart", "doughnut", "Browsers", byBrowser);
</script>
</body>
</html>
    """,
        user=user,
        business=business,
        by_day=json_for_js(by_day),
        by_conversion_day=json_for_js(by_conversion_day),
        by_campaign=json_for_js(by_campaign),
        by_source=json_for_js(by_source),
        by_slug=json_for_js(by_slug),
        by_device=json_for_js(by_device),
        by_browser=json_for_js(by_browser),
        by_city=json_for_js(by_city),
        by_country=json_for_js(by_country),
        by_variant=json_for_js(by_variant),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
