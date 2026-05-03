from __future__ import annotations

import csv
import json
import os
import uuid
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


@app.get("/")
def home():
    return """
    <h1>QR Tracker</h1>
    <p>Tracker is running.</p>
    <p>Health: <a href="/qr-track-health">/qr-track-health</a></p>
    <p>Dashboard: <a href="/dashboard">/dashboard</a></p>
    """


@app.get("/qr-track-health")
def qr_track_health():
    return jsonify({
        "status": "ok",
        "tracking": True,
        "version": "2.0",
        "features": [
            "user filtering",
            "business filtering",
            "campaign tracking",
            "source tracking",
            "medium tracking",
            "slug tracking",
            "notes",
            "dashboard",
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
        "user_agent": request.headers.get("User-Agent", ""),
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

    def group_by(key: str):
        result = {}
        for scan in scans:
            value = clean(scan.get(key), "uncategorised")
            result[value] = result.get(value, 0) + 1
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))

    return jsonify({
        "total_scans": len(scans),
        "by_campaign": group_by("campaign"),
        "by_source": group_by("source"),
        "by_medium": group_by("medium"),
        "by_slug": group_by("slug"),
        "by_business": group_by("business"),
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


@app.get("/dashboard")
def dashboard():
    user = clean(request.args.get("user"))
    business = clean(request.args.get("business"))

    scans = filtered_scans(user, business)

    total = len(scans)

    def count_by(key):
        result = {}
        for scan in scans:
            value = clean(scan.get(key), "uncategorised")
            result[value] = result.get(value, 0) + 1
        return sorted(result.items(), key=lambda x: x[1], reverse=True)

    campaign_rows = count_by("campaign")
    source_rows = count_by("source")
    slug_rows = count_by("slug")

    return render_template_string("""
<!doctype html>
<html>
<head>
    <title>QR Tracker Dashboard</title>
    <style>
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: linear-gradient(135deg, #f8fbff, #eff6ff, #f5f3ff);
            color: #0f172a;
        }
        .wrap {
            max-width: 1180px;
            margin: 0 auto;
            padding: 32px;
        }
        .hero, .card {
            background: rgba(255,255,255,.82);
            border: 1px solid rgba(255,255,255,.9);
            border-radius: 28px;
            box-shadow: 0 24px 70px rgba(59,130,246,.13);
            padding: 24px;
            margin-bottom: 20px;
        }
        h1 {
            margin: 0;
            font-size: 34px;
        }
        .muted {
            color: #64748b;
            font-weight: 600;
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
            font-weight: 700;
            min-width: 220px;
        }
        button, a.btn {
            padding: 14px 18px;
            border-radius: 16px;
            border: none;
            background: linear-gradient(135deg, #22d3ee, #8b5cf6);
            color: white;
            font-weight: 900;
            text-decoration: none;
            cursor: pointer;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
        }
        .stat {
            background: white;
            border-radius: 22px;
            padding: 20px;
            border: 1px solid #e2e8f0;
        }
        .stat strong {
            display: block;
            font-size: 32px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
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
        }
        .pill {
            display: inline-block;
            background: #ecfeff;
            color: #0891b2;
            border-radius: 999px;
            padding: 6px 10px;
            font-weight: 800;
            font-size: 12px;
        }
    </style>
</head>
<body>
<div class="wrap">
    <div class="hero">
        <h1>QR Tracker Dashboard</h1>
        <p class="muted">Enter the same name and business used in QR Studio Pro to view only your QR scans.</p>

        <form method="get" action="/dashboard">
            <input name="user" placeholder="Name" value="{{ user }}">
            <input name="business" placeholder="Business name" value="{{ business }}">
            <button type="submit">View Stats</button>
            <a class="btn" href="/export.csv?user={{ user }}&business={{ business }}">Export CSV</a>
        </form>
    </div>

    <div class="stats">
        <div class="stat"><span class="muted">Total scans</span><strong>{{ total }}</strong></div>
        <div class="stat"><span class="muted">Campaigns</span><strong>{{ campaign_count }}</strong></div>
        <div class="stat"><span class="muted">Sources</span><strong>{{ source_count }}</strong></div>
        <div class="stat"><span class="muted">QR slugs</span><strong>{{ slug_count }}</strong></div>
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
            <h2>QR Slugs</h2>
            {% for name, count in slug_rows %}
                <p><span class="pill">{{ name }}</span> — {{ count }}</p>
            {% else %}
                <p class="muted">No slug data yet.</p>
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
                <th>Medium</th>
                <th>Slug</th>
                <th>Destination</th>
            </tr>
            {% for scan in scans[-50:][::-1] %}
            <tr>
                <td>{{ scan.timestamp }}</td>
                <td>{{ scan.campaign }}</td>
                <td>{{ scan.source }}</td>
                <td>{{ scan.medium }}</td>
                <td>{{ scan.slug }}</td>
                <td>{{ scan.destination }}</td>
            </tr>
            {% else %}
            <tr><td colspan="6" class="muted">No scans yet.</td></tr>
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
        campaign_count=len(campaign_rows),
        source_count=len(source_rows),
        slug_count=len(slug_rows),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
