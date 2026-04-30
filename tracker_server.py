from __future__ import annotations

"""
QR Studio Pro - simple tracking website/server.

What it does:
- /qr-track-health
  Lets the desktop app confirm that tracking is installed.

- /qr-track?url=FINAL_URL&slug=poster-01&campaign=Canberra&source=poster&medium=qr
  Records the scan, then redirects the scanner to FINAL_URL.

- /qr-stats
  Shows a very simple scan dashboard.

Run locally:
    py -m venv .venv
    .venv\Scripts\Activate
    pip install flask
    py tracker_server.py

Deploy this file on the website/server you want to use as the tracking website.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template_string, request

APP_NAME = "QR Studio Pro Tracker"
DB_PATH = Path(__file__).with_name("qr_scans.db")

app = Flask(__name__)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT NOT NULL,
                slug TEXT,
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


def is_safe_url(value: str) -> bool:
    """Only allow normal web redirects."""
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


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
    slug = (request.args.get("slug") or "").strip()
    campaign = (request.args.get("campaign") or "").strip()
    source = (request.args.get("source") or "").strip()
    medium = (request.args.get("medium") or "").strip()
    notes = (request.args.get("notes") or "").strip()

    if not destination:
        return (
            "QR tracking is installed, but this QR is missing the final destination URL.",
            400,
        )

    if not is_safe_url(destination):
        return (
            "QR tracking blocked this redirect because the destination URL is invalid.",
            400,
        )

    forwarded_for = request.headers.get("X-Forwarded-For", "")
    ip_address = forwarded_for.split(",")[0].strip() or request.remote_addr or ""

    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO scans (
                scanned_at, slug, campaign, source, medium, notes, destination_url,
                ip_address, user_agent, referrer, language
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
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


@app.get("/qr-stats")
def qr_stats():
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        total = con.execute("SELECT COUNT(*) AS total FROM scans").fetchone()["total"]
        by_slug = con.execute(
            """
            SELECT COALESCE(NULLIF(slug, ''), '(no slug)') AS slug, COUNT(*) AS scans
            FROM scans
            GROUP BY COALESCE(NULLIF(slug, ''), '(no slug)')
            ORDER BY scans DESC
            LIMIT 50
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

    template = """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>QR Scan Stats</title>
        <style>
            body { font-family: Arial, sans-serif; background:#f5f5f2; color:#242424; padding:32px; }
            .card { background:white; border:1px solid #ddd6cc; border-radius:22px; padding:24px; margin-bottom:22px; box-shadow:0 12px 35px rgba(0,0,0,.06); }
            h1 { margin-top:0; }
            table { width:100%; border-collapse:collapse; font-size:14px; }
            th, td { padding:12px; border-bottom:1px solid #eee7dc; text-align:left; vertical-align:top; }
            th { color:#6b5542; }
            code { background:#f0ece5; padding:3px 6px; border-radius:8px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>QR Scan Stats</h1>
            <p>Total scans recorded: <strong>{{ total }}</strong></p>
            <p>Health check: <code>/qr-track-health</code> · Tracking endpoint: <code>/qr-track</code></p>
        </div>

        <div class="card">
            <h2>Top QR Slugs</h2>
            <table>
                <tr><th>Slug</th><th>Scans</th></tr>
                {% for row in by_slug %}
                <tr><td>{{ row["slug"] }}</td><td>{{ row["scans"] }}</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>Recent Scans</h2>
            <table>
                <tr>
                    <th>Time UTC</th><th>Slug</th><th>Campaign</th><th>Source</th>
                    <th>Medium</th><th>Destination</th><th>IP</th><th>Device/User Agent</th>
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
    """
    return render_template_string(template, total=total, by_slug=by_slug, recent=recent)


if __name__ == "__main__":
    init_db()
    print("QR tracking server running.")
    print("Health check: http://127.0.0.1:5000/qr-track-health")
    print("Stats page:   http://127.0.0.1:5000/qr-stats")
    app.run(host="0.0.0.0", port=5000, debug=True)
