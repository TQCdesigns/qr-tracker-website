from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template_string, request, Response

APP_NAME = "QR Studio Pro Tracker"

# Railway can reset local files on redeploy, but this works for your starter version.
# Later, we can upgrade this to PostgreSQL for permanent storage.
DB_PATH = Path(os.environ.get("QR_TRACKER_DB", Path(__file__).with_name("qr_scans.db")))

app = Flask(__name__)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT NOT NULL,
                slug TEXT NOT NULL,
                campaign TEXT,
                source TEXT,
                medium TEXT,
                notes TEXT,
                destination_url TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                referrer TEXT,
                language TEXT
            )
            """
        )
        con.commit()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def is_safe_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def clean_slug(value: str, destination: str) -> str:
    value = (value or "").strip()
    if value:
        return value[:120]

    parsed = urlparse(destination)
    fallback = parsed.netloc.replace("www.", "").replace(".", "-") or "unknown-qr"
    return fallback[:120]


@app.get("/")
def home():
    return render_template_string(
        """
        <!doctype html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>QR Studio Pro Tracker</title>
            <style>
                body { font-family: Arial, sans-serif; background:#f5f5f2; color:#242424; padding:40px; }
                .card { background:white; max-width:850px; margin:auto; border:1px solid #ddd6cc; border-radius:24px; padding:32px; box-shadow:0 12px 35px rgba(0,0,0,.08); }
                code { background:#f0ece5; padding:4px 8px; border-radius:8px; }
                a { color:#8a6f56; font-weight:bold; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>QR Studio Pro Tracker</h1>
                <p>Your tracking website is running.</p>
                <p>Health check: <code>/qr-track-health</code></p>
                <p>Tracking endpoint: <code>/qr-track</code></p>
                <p>Stats dashboard: <a href="/qr-stats">/qr-stats</a></p>
            </div>
        </body>
        </html>
        """
    )


@app.get("/qr-track-health")
def qr_track_health():
    return jsonify(
        {
            "status": "ok",
            "tracking": True,
            "app": APP_NAME,
            "required_track_endpoint": "/qr-track",
            "stats_endpoint": "/qr-stats",
        }
    )


@app.get("/qr-track")
def qr_track():
    destination = (request.args.get("url") or "").strip()

    if not destination:
        return "Missing final destination URL.", 400

    if not destination.startswith(("http://", "https://")):
        destination = "https://" + destination

    if not is_safe_url(destination):
        return "Invalid destination URL.", 400

    slug = clean_slug(request.args.get("slug"), destination)
    campaign = (request.args.get("campaign") or "").strip()
    source = (request.args.get("source") or "qr").strip()
    medium = (request.args.get("medium") or "qr").strip()
    notes = (request.args.get("notes") or "").strip()

    forwarded_for = request.headers.get("X-Forwarded-For", "")
    ip_address = forwarded_for.split(",")[0].strip() or request.remote_addr or ""

    with db() as con:
        con.execute(
            """
            INSERT INTO scans (
                scanned_at, slug, campaign, source, medium, notes,
                destination_url, ip_address, user_agent, referrer, language
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                slug,
                campaign,
                source,
                medium,
                notes,
                destination,
                ip_address,
                request.headers.get("User-Agent", ""),
                request.headers.get("Referer", ""),
                request.headers.get("Accept-Language", ""),
            ),
        )
        con.commit()

    return redirect(destination, code=302)


@app.get("/qr-stats-data")
def qr_stats_data():
    with db() as con:
        total = con.execute("SELECT COUNT(*) AS total FROM scans").fetchone()["total"]

        by_qr = con.execute(
            """
            SELECT
                slug,
                COUNT(*) AS scans,
                MAX(scanned_at) AS last_scan,
                COALESCE(NULLIF(campaign, ''), '-') AS campaign,
                COALESCE(NULLIF(source, ''), '-') AS source,
                COALESCE(NULLIF(medium, ''), '-') AS medium,
                destination_url
            FROM scans
            GROUP BY slug
            ORDER BY scans DESC, last_scan DESC
            """
        ).fetchall()

        recent = con.execute(
            """
            SELECT scanned_at, slug, campaign, source, medium, destination_url, ip_address, user_agent
            FROM scans
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()

    return jsonify(
        {
            "total_scans": total,
            "qr_codes": [dict(row) for row in by_qr],
            "recent_scans": [dict(row) for row in recent],
        }
    )


@app.get("/qr-stats.csv")
def qr_stats_csv():
    with db() as con:
        rows = con.execute(
            """
            SELECT scanned_at, slug, campaign, source, medium, notes,
                   destination_url, ip_address, user_agent, referrer, language
            FROM scans
            ORDER BY id DESC
            """
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "scanned_at", "slug", "campaign", "source", "medium", "notes",
        "destination_url", "ip_address", "user_agent", "referrer", "language"
    ])

    for row in rows:
        writer.writerow([row[key] for key in row.keys()])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=qr_scans.csv"},
    )


@app.get("/qr-stats")
def qr_stats():
    with db() as con:
        total = con.execute("SELECT COUNT(*) AS total FROM scans").fetchone()["total"]

        by_qr = con.execute(
            """
            SELECT
                slug,
                COUNT(*) AS scans,
                MAX(scanned_at) AS last_scan,
                COALESCE(NULLIF(campaign, ''), '-') AS campaign,
                COALESCE(NULLIF(source, ''), '-') AS source,
                COALESCE(NULLIF(medium, ''), '-') AS medium,
                destination_url
            FROM scans
            GROUP BY slug
            ORDER BY scans DESC, last_scan DESC
            """
        ).fetchall()

        recent = con.execute(
            """
            SELECT scanned_at, slug, campaign, source, medium, destination_url, ip_address, user_agent
            FROM scans
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()

    return render_template_string(
        """
        <!doctype html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>QR Scan Stats</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    background: #f5f5f2;
                    color: #242424;
                    padding: 32px;
                }
                .card {
                    background: white;
                    border: 1px solid #ddd6cc;
                    border-radius: 24px;
                    padding: 24px;
                    margin-bottom: 22px;
                    box-shadow: 0 12px 35px rgba(0,0,0,.06);
                }
                h1, h2 { margin-top: 0; }
                .total {
                    font-size: 42px;
                    font-weight: 900;
                    color: #8a6f56;
                }
                table {
                    width: 100%;
                    border-collapse: collapse;
                    font-size: 14px;
                }
                th, td {
                    padding: 12px;
                    border-bottom: 1px solid #eee7dc;
                    text-align: left;
                    vertical-align: top;
                }
                th { color: #6b5542; }
                code {
                    background: #f0ece5;
                    padding: 3px 6px;
                    border-radius: 8px;
                }
                .btn {
                    display:inline-block;
                    background:#8a6f56;
                    color:white;
                    padding:10px 14px;
                    border-radius:12px;
                    text-decoration:none;
                    font-weight:bold;
                }
                .muted { color:#6b7280; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>QR Scan Stats</h1>
                <div class="total">{{ total }}</div>
                <p class="muted">Total scans recorded across all QR codes.</p>
                <p>
                    <a class="btn" href="/qr-stats.csv">Download CSV</a>
                    <a class="btn" href="/qr-stats-data">View JSON</a>
                </p>
            </div>

            <div class="card">
                <h2>Individual QR Code Scan Counts</h2>
                <table>
                    <tr>
                        <th>QR Slug</th>
                        <th>Scans</th>
                        <th>Campaign</th>
                        <th>Source</th>
                        <th>Medium</th>
                        <th>Last Scan UTC</th>
                        <th>Destination</th>
                    </tr>
                    {% for row in by_qr %}
                    <tr>
                        <td><strong>{{ row["slug"] }}</strong></td>
                        <td><strong>{{ row["scans"] }}</strong></td>
                        <td>{{ row["campaign"] }}</td>
                        <td>{{ row["source"] }}</td>
                        <td>{{ row["medium"] }}</td>
                        <td>{{ row["last_scan"] }}</td>
                        <td>{{ row["destination_url"] }}</td>
                    </tr>
                    {% endfor %}
                </table>
            </div>

            <div class="card">
                <h2>Recent Scans</h2>
                <table>
                    <tr>
                        <th>Time UTC</th>
                        <th>QR Slug</th>
                        <th>Campaign</th>
                        <th>Source</th>
                        <th>Medium</th>
                        <th>Destination</th>
                        <th>IP</th>
                        <th>Device</th>
                    </tr>
                    {% for row in recent %}
                    <tr>
                        <td>{{ row["scanned_at"] }}</td>
                        <td>{{ row["slug"] }}</td>
                        <td>{{ row["campaign"] }}</td>
                        <td>{{ row["source"] }}</td>
                        <td>{{ row["medium"] }}</td>
                        <td>{{ row["destination_url"] }}</td>
                        <td>{{ row["ip_address"] }}</td>
                        <td>{{ row["user_agent"] }}</td>
                    </tr>
                    {% endfor %}
                </table>
            </div>
        </body>
        </html>
        """,
        total=total,
        by_qr=by_qr,
        recent=recent,
    )


# Important for Railway/Gunicorn.
# This makes sure the database/table exists before scans happen.
init_db()


if __name__ == "__main__":
    print("QR tracking server running.")
    print("Health check: http://127.0.0.1:5000/qr-track-health")
    print("Stats page:   http://127.0.0.1:5000/qr-stats")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
