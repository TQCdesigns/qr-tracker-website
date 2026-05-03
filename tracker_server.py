from __future__ import annotations

import csv
import json
import os
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from flask import Flask, request, redirect, jsonify, render_template_string, Response

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("QR_TRACKER_DATA_DIR", "tracker_data"))
DATA_DIR.mkdir(exist_ok=True)

SCAN_FILE = DATA_DIR / "scans.jsonl"


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


def load_scans() -> list[dict]:
    if not SCAN_FILE.exists():
        return []

    scans = []
    with SCAN_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                scans.append(json.loads(line))
            except Exception:
                pass
    return scans


def save_scan(scan: dict) -> None:
    with SCAN_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(scan, ensure_ascii=False) + "\n")


def filtered_scans(user: str, business: str) -> list[dict]:
    user = clean(user).lower()
    business = clean(business).lower()

    scans = load_scans()

    if user:
        scans = [s for s in scans if clean(s.get("user")).lower() == user]

    if business:
        scans = [s for s in scans if clean(s.get("business")).lower() == business]

    return scans


def count_by(scans: list[dict], key: str) -> list[tuple[str, int]]:
    counter = Counter(clean(scan.get(key), "uncategorised") for scan in scans)
    return counter.most_common()


def count_by_day(scans: list[dict]) -> dict[str, int]:
    result = defaultdict(int)
    for scan in scans:
        day = clean(scan.get("timestamp"))[:10] or "unknown"
        result[day] += 1
    return dict(sorted(result.items()))


def json_for_js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


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
        "version": "3.0",
        "features": [
            "user filtering",
            "business filtering",
            "campaign tracking",
            "source tracking",
            "medium tracking",
            "slug tracking",
            "notes",
            "dashboard",
            "analytics charts",
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

    scan = {
        "id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "user": clean(request.args.get("user"), "unknown"),
        "business": clean(request.args.get("business"), "unknown"),
        "slug": clean(request.args.get("slug"), "default"),
        "campaign": clean(request.args.get("campaign"), "uncategorised"),
        "source": clean(request.args.get("source"), "qr"),
        "medium": clean(request.args.get("medium"), "qr"),
        "notes": clean(request.args.get("notes")),
        "destination": destination,
        "ip": get_ip(),
        "device": device_type(user_agent),
        "browser": browser_name(user_agent),
        "user_agent": user_agent,
        "referer": request.headers.get("Referer", ""),
    }

    save_scan(scan)

    return redirect(destination, code=302)


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

    return jsonify({
        "total_scans": len(scans),
        "by_day": count_by_day(scans),
        "by_campaign": dict(count_by(scans, "campaign")),
        "by_source": dict(count_by(scans, "source")),
        "by_medium": dict(count_by(scans, "medium")),
        "by_slug": dict(count_by(scans, "slug")),
        "by_device": dict(count_by(scans, "device")),
        "by_browser": dict(count_by(scans, "browser")),
        "by_business": dict(count_by(scans, "business")),
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
        "slug",
        "notes",
        "destination",
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
    body {
        margin: 0;
        font-family: Segoe UI, Arial, sans-serif;
        background:
            radial-gradient(circle at 10% 10%, rgba(34,211,238,.24), transparent 28%),
            radial-gradient(circle at 90% 15%, rgba(168,85,247,.20), transparent 28%),
            linear-gradient(135deg, #f8fbff, #eff6ff, #f5f3ff);
        color: #0f172a;
    }

    .wrap {
        max-width: 1240px;
        margin: 0 auto;
        padding: 32px;
    }

    .hero, .card {
        background: rgba(255,255,255,.84);
        border: 1px solid rgba(255,255,255,.9);
        border-radius: 30px;
        box-shadow: 0 24px 70px rgba(59,130,246,.13);
        padding: 24px;
        margin-bottom: 20px;
        backdrop-filter: blur(18px);
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
        font-size: 36px;
        letter-spacing: -.8px;
    }

    h2 {
        margin-top: 0;
    }

    .muted {
        color: #64748b;
        font-weight: 650;
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
        background: linear-gradient(135deg, #22d3ee, #8b5cf6);
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
        background: rgba(255,255,255,.8);
        color: #2563eb;
        text-decoration: none;
        font-weight: 900;
        border: 1px solid #dbeafe;
    }

    .stats {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 16px;
        margin-bottom: 20px;
    }

    .stat {
        background: rgba(255,255,255,.9);
        border-radius: 24px;
        padding: 20px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 18px 45px rgba(34,211,238,.10);
    }

    .stat strong {
        display: block;
        font-size: 34px;
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
        color: #0891b2;
        border-radius: 999px;
        padding: 6px 10px;
        font-weight: 850;
        font-size: 12px;
    }

    canvas {
        width: 100% !important;
        max-height: 360px;
    }

    @media (max-width: 900px) {
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


@app.get("/dashboard")
def dashboard():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))

    scans = filtered_scans(user, business)
    total = len(scans)

    campaign_rows = count_by(scans, "campaign")
    source_rows = count_by(scans, "source")
    slug_rows = count_by(scans, "slug")
    device_rows = count_by(scans, "device")

    best_campaign = campaign_rows[0][0] if campaign_rows else "None yet"
    best_source = source_rows[0][0] if source_rows else "None yet"
    best_qr = slug_rows[0][0] if slug_rows else "None yet"

    return render_template_string("""
<!doctype html>
<html>
<head>
    <title>QR Tracker Dashboard</title>
    """ + BASE_CSS + """
</head>
<body>
<div class="wrap">
    <div class="hero">
        <div class="top">
            <div>
                <h1>QR Tracker Dashboard</h1>
                <p class="muted">Enter the same name and business used in QR Studio Pro to view only your QR scans.</p>
            </div>
        </div>

        """ + filter_form(user, business, "/dashboard") + """

        <div class="nav">
            <a href="/dashboard?user={{ user }}&business={{ business }}">Dashboard</a>
            <a href="/analytics?user={{ user }}&business={{ business }}">Analytics Charts</a>
            <a href="/api/summary?user={{ user }}&business={{ business }}">API Summary</a>
        </div>
    </div>

    <div class="stats">
        <div class="stat"><span class="muted">Total scans</span><strong>{{ total }}</strong></div>
        <div class="stat"><span class="muted">Campaigns</span><strong>{{ campaign_count }}</strong></div>
        <div class="stat"><span class="muted">Sources</span><strong>{{ source_count }}</strong></div>
        <div class="stat"><span class="muted">QR codes</span><strong>{{ slug_count }}</strong></div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>Best Campaign</h2>
            <p><span class="pill">{{ best_campaign }}</span></p>
            <p class="muted">Great for grouping by city, event, promo, flyer drop or location.</p>
        </div>

        <div class="card">
            <h2>Best Source</h2>
            <p><span class="pill">{{ best_source }}</span></p>
            <p class="muted">Source tells you where scans came from, like website-url, poster, flyer or social.</p>
        </div>

        <div class="card">
            <h2>Top QR</h2>
            <p><span class="pill">{{ best_qr }}</span></p>
            <p class="muted">Slug helps identify the exact QR code.</p>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>Campaigns</h2>
            {% for name, count in campaign_rows %}
                <p><span class="pill">{{ name }}</span> — {{ count }}</p>
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
            <h2>Devices</h2>
            {% for name, count in device_rows %}
                <p><span class="pill">{{ name }}</span> — {{ count }}</p>
            {% else %}
                <p class="muted">No device data yet.</p>
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
                <th>Device</th>
                <th>Browser</th>
                <th>Slug</th>
                <th>Destination</th>
            </tr>
            {% for scan in scans[-50:][::-1] %}
            <tr>
                <td>{{ scan.timestamp }}</td>
                <td>{{ scan.campaign }}</td>
                <td>{{ scan.source }}</td>
                <td>{{ scan.device }}</td>
                <td>{{ scan.browser }}</td>
                <td>{{ scan.slug }}</td>
                <td>{{ scan.destination }}</td>
            </tr>
            {% else %}
            <tr><td colspan="7" class="muted">No scans yet.</td></tr>
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
        campaign_rows=campaign_rows,
        source_rows=source_rows,
        slug_rows=slug_rows,
        device_rows=device_rows,
        campaign_count=len(campaign_rows),
        source_count=len(source_rows),
        slug_count=len(slug_rows),
        best_campaign=best_campaign,
        best_source=best_source,
        best_qr=best_qr,
    )


@app.get("/analytics")
def analytics():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))

    scans = filtered_scans(user, business)

    by_day = count_by_day(scans)
    by_campaign = dict(count_by(scans, "campaign"))
    by_source = dict(count_by(scans, "source"))
    by_slug = dict(count_by(scans, "slug"))
    by_device = dict(count_by(scans, "device"))
    by_browser = dict(count_by(scans, "browser"))

    return render_template_string("""
<!doctype html>
<html>
<head>
    <title>QR Tracker Analytics</title>
    """ + BASE_CSS + """
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
<div class="wrap">
    <div class="hero">
        <h1>Advanced Analytics</h1>
        <p class="muted">Charts for scans over time, campaigns, sources, QR slugs, devices and browsers.</p>

        """ + filter_form(user, business, "/analytics") + """

        <div class="nav">
            <a href="/dashboard?user={{ user }}&business={{ business }}">Dashboard</a>
            <a href="/analytics?user={{ user }}&business={{ business }}">Analytics Charts</a>
            <a href="/api/summary?user={{ user }}&business={{ business }}">API Summary</a>
        </div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Scans Over Time</h2>
            <canvas id="timeChart"></canvas>
        </div>

        <div class="card">
            <h2>Campaign Performance</h2>
            <canvas id="campaignChart"></canvas>
        </div>

        <div class="card">
            <h2>Source Breakdown</h2>
            <canvas id="sourceChart"></canvas>
        </div>

        <div class="card">
            <h2>Top QR Codes</h2>
            <canvas id="slugChart"></canvas>
        </div>

        <div class="card">
            <h2>Device Types</h2>
            <canvas id="deviceChart"></canvas>
        </div>

        <div class="card">
            <h2>Browsers</h2>
            <canvas id="browserChart"></canvas>
        </div>
    </div>
</div>

<script>
const byDay = {{ by_day | safe }};
const byCampaign = {{ by_campaign | safe }};
const bySource = {{ by_source | safe }};
const bySlug = {{ by_slug | safe }};
const byDevice = {{ by_device | safe }};
const byBrowser = {{ by_browser | safe }};

function makeChart(id, type, title, obj) {
    const labels = Object.keys(obj);
    const values = Object.values(obj);

    new Chart(document.getElementById(id), {
        type: type,
        data: {
            labels: labels,
            datasets: [{
                label: title,
                data: values,
                borderWidth: 2,
                tension: 0.35
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
makeChart("campaignChart", "bar", "Campaigns", byCampaign);
makeChart("sourceChart", "doughnut", "Sources", bySource);
makeChart("slugChart", "bar", "QR Codes", bySlug);
makeChart("deviceChart", "pie", "Devices", byDevice);
makeChart("browserChart", "doughnut", "Browsers", byBrowser);
</script>
</body>
</html>
    """,
        user=user,
        business=business,
        by_day=json_for_js(by_day),
        by_campaign=json_for_js(by_campaign),
        by_source=json_for_js(by_source),
        by_slug=json_for_js(by_slug),
        by_device=json_for_js(by_device),
        by_browser=json_for_js(by_browser),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
