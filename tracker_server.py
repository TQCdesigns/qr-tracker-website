from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, redirect, render_template_string, request, url_for

APP_NAME = "QR Studio Pro Tracker"
DB_PATH = Path(os.environ.get("QR_TRACKER_DB", Path(__file__).with_name("qr_scans.db")))

app = Flask(__name__)


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT NOT NULL,
                user_name TEXT NOT NULL DEFAULT 'Unassigned',
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

        # Upgrade older database versions without deleting old scans.
        if not column_exists(con, "scans", "user_name"):
            con.execute("ALTER TABLE scans ADD COLUMN user_name TEXT NOT NULL DEFAULT 'Unassigned'")

        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_user_name ON scans(user_name)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_slug ON scans(slug)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_user_slug ON scans(user_name, slug)")
        con.commit()


def is_safe_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def clean_text(value: str | None, fallback: str = "") -> str:
    return (value or fallback).strip()[:160]


def clean_slug(value: str | None, destination: str) -> str:
    value = clean_text(value)
    if value:
        return value
    parsed = urlparse(destination)
    fallback = parsed.netloc.replace("www.", "").replace(".", "-") or "unknown-qr"
    return fallback[:120]


def get_user_from_request() -> str:
    return clean_text(
        request.args.get("user")
        or request.args.get("user_name")
        or request.args.get("name")
        or request.form.get("user")
        or request.form.get("user_name")
        or request.form.get("name")
    )


def stats_query_parts(user_name: str) -> tuple[str, list[str]]:
    if user_name:
        return "WHERE user_name = ?", [user_name]
    return "", []


@app.get("/")
def home():
    return render_template_string(
        """
        <!doctype html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>QR Studio Pro Tracker</title>
            <style>
                body { font-family: Arial, sans-serif; background:#f5f5f2; color:#242424; padding:40px; }
                .card { background:white; max-width:900px; margin:auto; border:1px solid #ddd6cc; border-radius:24px; padding:32px; box-shadow:0 12px 35px rgba(0,0,0,.08); }
                input { width:100%; box-sizing:border-box; padding:14px; border-radius:14px; border:1px solid #d8d0c3; font-size:16px; margin:8px 0 14px; }
                button, .btn { display:inline-block; background:#8a6f56; color:white; padding:12px 16px; border:0; border-radius:14px; text-decoration:none; font-weight:bold; cursor:pointer; }
                code { background:#f0ece5; padding:4px 8px; border-radius:8px; }
                .muted { color:#6b7280; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>QR Studio Pro Tracker</h1>
                <p>Your user-based tracking website is running.</p>
                <p class="muted">Enter a User / Business name to view only that user's QR stats.</p>
                <form action="/qr-stats" method="get">
                    <label><strong>User / Business name</strong></label>
                    <input name="user" placeholder="Example: Olive & Ivory" required>
                    <button type="submit">Open User Dashboard</button>
                </form>
                <hr>
                <p>Health check: <code>/qr-track-health</code></p>
                <p>Tracking endpoint: <code>/qr-track?url=...&user=...&slug=...</code></p>
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
            "user_tracking": True,
            "app": APP_NAME,
            "required_track_endpoint": "/qr-track",
            "stats_endpoint": "/qr-stats?user=USER_NAME",
        }
    )


@app.get("/qr-track")
def qr_track():
    destination = clean_text(request.args.get("url"))
    if not destination:
        return "Missing final destination URL.", 400

    if not destination.startswith(("http://", "https://")):
        destination = "https://" + destination

    if not is_safe_url(destination):
        return "Invalid destination URL.", 400

    user_name = get_user_from_request()
    if not user_name:
        return "Missing user name. Add &user=YourBusinessName to this QR tracking link.", 400

    slug = clean_slug(request.args.get("slug"), destination)
    campaign = clean_text(request.args.get("campaign"))
    source = clean_text(request.args.get("source"), "qr")
    medium = clean_text(request.args.get("medium"), "qr")
    notes = clean_text(request.args.get("notes"))

    forwarded_for = request.headers.get("X-Forwarded-For", "")
    ip_address = forwarded_for.split(",")[0].strip() or request.remote_addr or ""

    with db() as con:
        con.execute(
            """
            INSERT INTO scans (
                scanned_at, user_name, slug, campaign, source, medium, notes,
                destination_url, ip_address, user_agent, referrer, language
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                user_name,
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
    user_name = get_user_from_request()
    where, params = stats_query_parts(user_name)

    with db() as con:
        total = con.execute(f"SELECT COUNT(*) AS total FROM scans {where}", params).fetchone()["total"]
        by_qr = con.execute(
            f"""
            SELECT
                user_name,
                slug,
                COUNT(*) AS scans,
                MAX(scanned_at) AS last_scan,
                COALESCE(NULLIF(campaign, ''), '-') AS campaign,
                COALESCE(NULLIF(source, ''), '-') AS source,
                COALESCE(NULLIF(medium, ''), '-') AS medium,
                destination_url
            FROM scans
            {where}
            GROUP BY user_name, slug
            ORDER BY scans DESC, last_scan DESC
            """,
            params,
        ).fetchall()
        recent = con.execute(
            f"""
            SELECT scanned_at, user_name, slug, campaign, source, medium, destination_url, ip_address, user_agent
            FROM scans
            {where}
            ORDER BY id DESC
            LIMIT 100
            """,
            params,
        ).fetchall()

    return jsonify(
        {
            "filtered_user": user_name or None,
            "total_scans": total,
            "qr_codes": [dict(row) for row in by_qr],
            "recent_scans": [dict(row) for row in recent],
        }
    )


@app.get("/qr-stats.csv")
def qr_stats_csv():
    user_name = get_user_from_request()
    where, params = stats_query_parts(user_name)
    with db() as con:
        rows = con.execute(
            f"""
            SELECT scanned_at, user_name, slug, campaign, source, medium, notes,
                   destination_url, ip_address, user_agent, referrer, language
            FROM scans
            {where}
            ORDER BY id DESC
            """,
            params,
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "scanned_at", "user_name", "slug", "campaign", "source", "medium", "notes",
        "destination_url", "ip_address", "user_agent", "referrer", "language"
    ])
    for row in rows:
        writer.writerow([row[key] for key in row.keys()])

    filename_user = (user_name or "all-users").replace(" ", "_")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=qr_scans_{filename_user}.csv"},
    )


@app.get("/qr-stats")
def qr_stats():
    user_name = get_user_from_request()

    if not user_name:
        return render_template_string(
            """
            <!doctype html>
            <html>
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <title>Choose User Dashboard</title>
                <style>
                    body { font-family: Arial, sans-serif; background:#f5f5f2; color:#242424; padding:32px; }
                    .card { background:white; max-width:720px; margin:auto; border:1px solid #ddd6cc; border-radius:24px; padding:28px; box-shadow:0 12px 35px rgba(0,0,0,.06); }
                    input { width:100%; box-sizing:border-box; padding:15px; border-radius:14px; border:1px solid #d8d0c3; font-size:16px; margin:10px 0 16px; }
                    button { background:#8a6f56; color:white; padding:13px 18px; border:0; border-radius:14px; font-weight:bold; cursor:pointer; }
                    .muted { color:#6b7280; }
                </style>
            </head>
            <body>
                <div class="card">
                    <h1>Open QR Stats</h1>
                    <p class="muted">Enter the same User / Business name used in QR Studio Pro.</p>
                    <form method="get" action="/qr-stats">
                        <label><strong>User / Business name</strong></label>
                        <input name="user" placeholder="Example: Olive & Ivory" required autofocus>
                        <button type="submit">View My QR Codes</button>
                    </form>
                </div>
            </body>
            </html>
            """
        )

    where, params = stats_query_parts(user_name)
    with db() as con:
        total = con.execute(f"SELECT COUNT(*) AS total FROM scans {where}", params).fetchone()["total"]
        by_qr = con.execute(
            f"""
            SELECT
                slug,
                COUNT(*) AS scans,
                MAX(scanned_at) AS last_scan,
                COALESCE(NULLIF(campaign, ''), '-') AS campaign,
                COALESCE(NULLIF(source, ''), '-') AS source,
                COALESCE(NULLIF(medium, ''), '-') AS medium,
                destination_url
            FROM scans
            {where}
            GROUP BY slug
            ORDER BY scans DESC, last_scan DESC
            """,
            params,
        ).fetchall()
        recent = con.execute(
            f"""
            SELECT scanned_at, slug, campaign, source, medium, destination_url, ip_address, user_agent
            FROM scans
            {where}
            ORDER BY id DESC
            LIMIT 100
            """,
            params,
        ).fetchall()

    csv_url = url_for("qr_stats_csv", user=user_name)
    json_url = url_for("qr_stats_data", user=user_name)

    return render_template_string(
        """
        <!doctype html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>QR Scan Stats - {{ user_name }}</title>
            <style>
                body { font-family: Arial, sans-serif; background:#f5f5f2; color:#242424; padding:32px; }
                .card { background:white; border:1px solid #ddd6cc; border-radius:24px; padding:24px; margin-bottom:22px; box-shadow:0 12px 35px rgba(0,0,0,.06); }
                h1, h2 { margin-top:0; }
                .total { font-size:42px; font-weight:900; color:#8a6f56; }
                .pill { display:inline-block; background:#f0ece5; color:#6b5542; padding:8px 12px; border-radius:999px; font-weight:bold; }
                table { width:100%; border-collapse:collapse; font-size:14px; }
                th, td { padding:12px; border-bottom:1px solid #eee7dc; text-align:left; vertical-align:top; }
                th { color:#6b5542; }
                .btn { display:inline-block; background:#8a6f56; color:white; padding:10px 14px; border-radius:12px; text-decoration:none; font-weight:bold; margin-right:8px; }
                .muted { color:#6b7280; }
                .wide { overflow-x:auto; }
                input { padding:10px; border-radius:12px; border:1px solid #ddd6cc; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>QR Scan Stats</h1>
                <p class="pill">User: {{ user_name }}</p>
                <div class="total">{{ total }}</div>
                <p class="muted">Total scans recorded for this user only.</p>
                <p>
                    <a class="btn" href="{{ csv_url }}">Download CSV</a>
                    <a class="btn" href="{{ json_url }}">View JSON</a>
                    <a class="btn" href="/qr-stats">Switch User</a>
                </p>
            </div>

            <div class="card wide">
                <h2>Individual QR Code Scan Counts</h2>
                <table>
                    <tr><th>QR Slug</th><th>Scans</th><th>Campaign</th><th>Source</th><th>Medium</th><th>Last Scan UTC</th><th>Destination</th></tr>
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
                {% if not by_qr %}<p class="muted">No scans yet for this user name.</p>{% endif %}
            </div>

            <div class="card wide">
                <h2>Recent Scans</h2>
                <table>
                    <tr><th>Time UTC</th><th>QR Slug</th><th>Campaign</th><th>Source</th><th>Medium</th><th>Destination</th><th>IP</th><th>Device</th></tr>
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
        user_name=user_name,
        total=total,
        by_qr=by_qr,
        recent=recent,
        csv_url=csv_url,
        json_url=json_url,
    )


# Required for Railway/Gunicorn.
init_db()

if __name__ == "__main__":
    print("QR tracking server running.")
    print("Health check: http://127.0.0.1:5000/qr-track-health")
    print("Stats page:   http://127.0.0.1:5000/qr-stats")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
