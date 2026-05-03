from __future__ import annotations

import csv
import io
import os
import re
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


def clean_text(value: str | None, fallback: str = "", limit: int = 160) -> str:
    return (value or fallback).strip()[:limit]


def normalise_identity(value: str) -> str:
    value = clean_text(value, limit=180).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def make_user_key(user_name: str, business_name: str) -> str:
    return f"{normalise_identity(business_name)}--{normalise_identity(user_name)}"


def get_identity_from_request() -> tuple[str, str, str]:
    user_name = clean_text(
        request.args.get("user")
        or request.args.get("user_name")
        or request.args.get("name")
        or request.form.get("user")
        or request.form.get("user_name")
        or request.form.get("name")
    )
    business_name = clean_text(
        request.args.get("business")
        or request.args.get("business_name")
        or request.form.get("business")
        or request.form.get("business_name")
    )
    supplied_key = clean_text(request.args.get("user_key") or request.form.get("user_key"), limit=220)
    user_key = supplied_key or (make_user_key(user_name, business_name) if user_name and business_name else "")
    return user_name, business_name, user_key


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT NOT NULL,
                user_name TEXT NOT NULL DEFAULT 'Unassigned',
                business_name TEXT NOT NULL DEFAULT 'Unassigned',
                user_key TEXT NOT NULL DEFAULT 'unassigned--unassigned',
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

        if not column_exists(con, "scans", "user_name"):
            con.execute("ALTER TABLE scans ADD COLUMN user_name TEXT NOT NULL DEFAULT 'Unassigned'")
        if not column_exists(con, "scans", "business_name"):
            con.execute("ALTER TABLE scans ADD COLUMN business_name TEXT NOT NULL DEFAULT 'Unassigned'")
        if not column_exists(con, "scans", "user_key"):
            con.execute("ALTER TABLE scans ADD COLUMN user_key TEXT NOT NULL DEFAULT 'unassigned--unassigned'")

        rows = con.execute(
            "SELECT id, user_name, business_name, user_key FROM scans WHERE user_key = '' OR user_key = 'unassigned--unassigned'"
        ).fetchall()
        for row in rows:
            fixed_key = make_user_key(row[1] or "Unassigned", row[2] or "Unassigned")
            con.execute("UPDATE scans SET user_key = ? WHERE id = ?", (fixed_key, row[0]))

        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_user_key ON scans(user_key)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_user_business ON scans(user_name, business_name)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_slug ON scans(slug)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_scans_key_slug ON scans(user_key, slug)")
        con.commit()


def is_safe_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def clean_slug(value: str | None, destination: str) -> str:
    value = clean_text(value, limit=120)
    if value:
        return value
    parsed = urlparse(destination)
    fallback = parsed.netloc.replace("www.", "").replace(".", "-") or "unknown-qr"
    return fallback[:120]


def where_for_identity(user_key: str) -> tuple[str, list[str]]:
    if not user_key:
        return "WHERE 1 = 0", []
    return "WHERE user_key = ?", [user_key]


BASE_CSS = """
:root {
  --bg: #f7f4ee;
  --panel: rgba(255,255,255,.86);
  --ink: #1f2937;
  --muted: #6b7280;
  --line: #e7ded0;
  --sand: #b08968;
  --sage: #7d8b6f;
  --teal: #0ea5a5;
  --blue: #2563eb;
  --purple: #7c3aed;
  --shadow: 0 24px 80px rgba(64, 50, 33, .14);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background:
    radial-gradient(circle at top left, rgba(14,165,165,.18), transparent 34rem),
    radial-gradient(circle at bottom right, rgba(176,137,104,.22), transparent 34rem),
    linear-gradient(135deg, #fbfaf7 0%, var(--bg) 46%, #eee6da 100%);
  color: var(--ink);
  min-height: 100vh;
  padding: 28px;
}
a { color: inherit; }
.shell { max-width: 1180px; margin: 0 auto; }
.hero, .card {
  background: var(--panel);
  border: 1px solid rgba(231,222,208,.9);
  border-radius: 30px;
  box-shadow: var(--shadow);
  backdrop-filter: blur(16px);
}
.hero { padding: 32px; display: grid; grid-template-columns: 1.25fr .75fr; gap: 24px; align-items: center; }
.logo { width: 58px; height: 58px; border-radius: 20px; display: grid; place-items: center; color: white; font-weight: 900; font-size: 30px; background: linear-gradient(135deg, var(--teal), var(--blue), var(--purple)); box-shadow: 0 16px 40px rgba(37,99,235,.28); }
.eyebrow { display:inline-flex; align-items:center; gap:8px; padding: 8px 12px; border-radius: 999px; background: #fff7ed; border:1px solid #eadac8; color:#8a5a35; font-size:12px; font-weight:900; letter-spacing:.12em; text-transform:uppercase; }
h1 { margin: 14px 0 8px; font-size: clamp(34px, 5vw, 58px); line-height: .96; letter-spacing: -0.05em; }
p { color: var(--muted); line-height: 1.65; }
.form-card { padding: 24px; background: white; border-radius: 26px; border:1px solid var(--line); }
label { display:block; font-size:13px; font-weight:900; color:#374151; margin: 12px 0 7px; }
input { width:100%; border:1px solid #ded4c7; background:#fffdfa; color:var(--ink); border-radius:16px; padding:15px 16px; font-size:15px; outline:none; }
input:focus { border-color: var(--teal); box-shadow: 0 0 0 4px rgba(14,165,165,.12); }
.btn, button { border:0; border-radius:16px; padding:14px 18px; font-weight:900; cursor:pointer; text-decoration:none; display:inline-flex; justify-content:center; align-items:center; gap:8px; }
.btn-primary, button { width:100%; color:#fff; background: linear-gradient(135deg, var(--teal), var(--blue)); box-shadow: 0 14px 32px rgba(37,99,235,.22); }
.btn-soft { background:#fff; color:#334155; border:1px solid var(--line); box-shadow:none; width:auto; }
.grid { display:grid; grid-template-columns: repeat(4, 1fr); gap:16px; margin:22px 0; }
.metric { padding:22px; border-radius:24px; background:white; border:1px solid var(--line); box-shadow: 0 16px 45px rgba(64,50,33,.08); }
.metric span { display:block; color:var(--muted); font-weight:800; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
.metric strong { display:block; font-size:34px; margin-top:6px; letter-spacing:-.04em; }
.toolbar { display:flex; flex-wrap:wrap; gap:10px; justify-content:space-between; align-items:center; margin:24px 0 14px; }
.card { padding:24px; margin-top:18px; }
table { width:100%; border-collapse:separate; border-spacing:0 10px; }
th { text-align:left; color:#6b7280; font-size:12px; letter-spacing:.08em; text-transform:uppercase; padding:0 12px; }
td { background:white; border-top:1px solid var(--line); border-bottom:1px solid var(--line); padding:14px 12px; vertical-align:top; }
td:first-child { border-left:1px solid var(--line); border-radius:16px 0 0 16px; font-weight:900; }
td:last-child { border-right:1px solid var(--line); border-radius:0 16px 16px 0; }
.badge { display:inline-flex; padding:6px 10px; border-radius:999px; background:#eef6f6; color:#0f766e; font-weight:900; font-size:12px; }
.muted { color: var(--muted); }
.small { font-size:13px; }
.url { max-width: 360px; overflow-wrap:anywhere; color:#475569; }
.empty { text-align:center; padding:36px; border:1px dashed #d7c9b8; border-radius:24px; background:rgba(255,255,255,.62); }
@media (max-width: 900px) { body { padding:16px; } .hero { grid-template-columns:1fr; } .grid { grid-template-columns: repeat(2, 1fr); } table { font-size:13px; } }
@media (max-width: 620px) { .grid { grid-template-columns: 1fr; } .toolbar { display:block; } .btn-soft { width:100%; margin-top:8px; } }
"""


def render_page(content: str, title: str = APP_NAME, **context):
    return render_template_string(
        """
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{{ title }}</title>
          <style>{{ css|safe }}</style>
        </head>
        <body>
          <main class="shell">{{ content|safe }}</main>
        </body>
        </html>
        """,
        title=title,
        css=BASE_CSS,
        content=render_template_string(content, **context),
    )


@app.get("/")
def home():
    return render_page(
        """
        <section class="hero">
          <div>
            <div class="logo">×</div>
            <div class="eyebrow">QR Studio Pro Tracker</div>
            <h1>Open your private QR dashboard.</h1>
            <p>Enter the exact name and business name used on the first screen of QR Studio Pro. This keeps each person's QR scans separated without needing a full password system.</p>
          </div>
          <form class="form-card" action="/qr-stats" method="get">
            <label>Your name *</label>
            <input name="user" placeholder="Example: Brenn" required autofocus>
            <label>Business name *</label>
            <input name="business" placeholder="Example: Olive & Ivory" required>
            <p class="small muted">Tip: both fields must match the QR Studio Pro profile that generated the tracked QR code.</p>
            <button type="submit">Open My Dashboard →</button>
          </form>
        </section>
        <section class="card">
          <span class="badge">Live endpoints</span>
          <p><strong>Health:</strong> /qr-track-health</p>
          <p><strong>Tracking:</strong> /qr-track?url=...&user=...&business=...&slug=...</p>
        </section>
        """
    )


@app.get("/qr-track-health")
def qr_track_health():
    return jsonify(
        {
            "status": "ok",
            "tracking": True,
            "profile_tracking": True,
            "requires": ["user", "business", "url"],
            "app": APP_NAME,
            "required_track_endpoint": "/qr-track",
            "stats_endpoint": "/qr-stats?user=USER_NAME&business=BUSINESS_NAME",
        }
    )


@app.get("/qr-track")
def qr_track():
    destination = clean_text(request.args.get("url"), limit=1200)
    if not destination:
        return "Missing final destination URL.", 400

    if not destination.startswith(("http://", "https://")):
        destination = "https://" + destination

    if not is_safe_url(destination):
        return "Invalid destination URL.", 400

    user_name, business_name, user_key = get_identity_from_request()
    if not user_name or not business_name:
        return "Missing profile details. Add &user=YourName&business=YourBusinessName to this QR tracking link.", 400

    slug = clean_slug(request.args.get("slug"), destination)
    campaign = clean_text(request.args.get("campaign"))
    source = clean_text(request.args.get("source"), "qr")
    medium = clean_text(request.args.get("medium"), "qr")
    notes = clean_text(request.args.get("notes"), limit=300)

    forwarded_for = request.headers.get("X-Forwarded-For", "")
    ip_address = forwarded_for.split(",")[0].strip() or request.remote_addr or ""

    with db() as con:
        con.execute(
            """
            INSERT INTO scans (
                scanned_at, user_name, business_name, user_key, slug, campaign, source, medium, notes,
                destination_url, ip_address, user_agent, referrer, language
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                user_name,
                business_name,
                user_key,
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


def dashboard_data(user_key: str):
    where, params = where_for_identity(user_key)
    with db() as con:
        total = con.execute(f"SELECT COUNT(*) AS total FROM scans {where}", params).fetchone()["total"]
        unique_qrs = con.execute(f"SELECT COUNT(DISTINCT slug) AS total FROM scans {where}", params).fetchone()["total"]
        campaigns = con.execute(
            f"SELECT COUNT(DISTINCT COALESCE(NULLIF(campaign, ''), '-')) AS total FROM scans {where}", params
        ).fetchone()["total"]
        last_scan = con.execute(f"SELECT MAX(scanned_at) AS last_scan FROM scans {where}", params).fetchone()["last_scan"]
        by_qr = con.execute(
            f"""
            SELECT slug, COUNT(*) AS scans, MAX(scanned_at) AS last_scan,
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
        by_source = con.execute(
            f"""
            SELECT COALESCE(NULLIF(source, ''), '-') AS source, COUNT(*) AS scans
            FROM scans
            {where}
            GROUP BY COALESCE(NULLIF(source, ''), '-')
            ORDER BY scans DESC
            LIMIT 8
            """,
            params,
        ).fetchall()
    return total, unique_qrs, campaigns, last_scan, by_qr, recent, by_source


@app.get("/qr-stats-data")
def qr_stats_data():
    user_name, business_name, user_key = get_identity_from_request()
    if not user_key:
        return jsonify({"error": "Missing user and business name."}), 400
    total, unique_qrs, campaigns, last_scan, by_qr, recent, by_source = dashboard_data(user_key)
    return jsonify(
        {
            "user_name": user_name,
            "business_name": business_name,
            "user_key": user_key,
            "total_scans": total,
            "unique_qrs": unique_qrs,
            "campaigns": campaigns,
            "last_scan": last_scan,
            "qr_codes": [dict(row) for row in by_qr],
            "recent_scans": [dict(row) for row in recent],
            "sources": [dict(row) for row in by_source],
        }
    )


@app.get("/qr-stats.csv")
def qr_stats_csv():
    user_name, business_name, user_key = get_identity_from_request()
    if not user_key:
        return "Missing user and business name.", 400
    where, params = where_for_identity(user_key)
    with db() as con:
        rows = con.execute(
            f"""
            SELECT scanned_at, user_name, business_name, slug, campaign, source, medium, notes,
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
        "scanned_at", "user_name", "business_name", "slug", "campaign", "source", "medium", "notes",
        "destination_url", "ip_address", "user_agent", "referrer", "language"
    ])
    for row in rows:
        writer.writerow([row[key] for key in row.keys()])

    filename_user = f"{normalise_identity(business_name)}_{normalise_identity(user_name)}"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=qr_scans_{filename_user}.csv"},
    )


@app.get("/qr-stats")
def qr_stats():
    user_name, business_name, user_key = get_identity_from_request()

    if not user_name or not business_name:
        return render_page(
            """
            <section class="hero">
              <div>
                <div class="logo">×</div>
                <div class="eyebrow">Private Dashboard</div>
                <h1>Enter your QR Studio profile.</h1>
                <p>Both fields are required. The dashboard only loads scans matching this exact name and business pair.</p>
              </div>
              <form class="form-card" method="get" action="/qr-stats">
                <label>Your name *</label>
                <input name="user" placeholder="Example: Brenn" required autofocus>
                <label>Business name *</label>
                <input name="business" placeholder="Example: Olive & Ivory" required>
                <button type="submit">View My QR Codes →</button>
              </form>
            </section>
            """,
            title="Open QR Stats",
        )

    total, unique_qrs, campaigns, last_scan, by_qr, recent, by_source = dashboard_data(user_key)
    csv_url = url_for("qr_stats_csv", user=user_name, business=business_name)
    json_url = url_for("qr_stats_data", user=user_name, business=business_name)

    return render_page(
        """
        <section class="hero">
          <div>
            <div class="logo">×</div>
            <div class="eyebrow">{{ business_name }} · {{ user_name }}</div>
            <h1>QR scan dashboard</h1>
            <p>Only scans connected to this name and business profile are shown here.</p>
          </div>
          <div class="form-card">
            <label>Workspace key</label>
            <input value="{{ user_key }}" readonly>
            <p class="small muted">Keep the same name + business setup in QR Studio Pro to keep scans grouped correctly.</p>
          </div>
        </section>

        <section class="grid">
          <div class="metric"><span>Total scans</span><strong>{{ total }}</strong></div>
          <div class="metric"><span>QR codes</span><strong>{{ unique_qrs }}</strong></div>
          <div class="metric"><span>Campaigns</span><strong>{{ campaigns }}</strong></div>
          <div class="metric"><span>Last scan</span><strong style="font-size:18px">{{ last_scan or 'No scans yet' }}</strong></div>
        </section>

        <div class="toolbar">
          <div><span class="badge">Advanced report</span><p class="small muted">Grouped QR performance, source split and recent scan history.</p></div>
          <div>
            <a class="btn btn-soft" href="{{ csv_url }}">Download CSV</a>
            <a class="btn btn-soft" href="{{ json_url }}">View JSON</a>
          </div>
        </div>

        <section class="card">
          <h2>QR code performance</h2>
          {% if by_qr %}
          <table>
            <thead><tr><th>QR slug</th><th>Scans</th><th>Campaign</th><th>Source</th><th>Last scan</th><th>Destination</th></tr></thead>
            <tbody>
              {% for row in by_qr %}
              <tr>
                <td>{{ row.slug }}</td>
                <td><span class="badge">{{ row.scans }}</span></td>
                <td>{{ row.campaign }}</td>
                <td>{{ row.source }} / {{ row.medium }}</td>
                <td class="small muted">{{ row.last_scan }}</td>
                <td class="url small">{{ row.destination_url }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
          {% else %}
          <div class="empty">No scans yet. Generate a tracked QR in QR Studio Pro, scan it once, then refresh this page.</div>
          {% endif %}
        </section>

        <section class="card">
          <h2>Source split</h2>
          {% if by_source %}
          <table>
            <thead><tr><th>Source</th><th>Scans</th></tr></thead>
            <tbody>{% for row in by_source %}<tr><td>{{ row.source }}</td><td><span class="badge">{{ row.scans }}</span></td></tr>{% endfor %}</tbody>
          </table>
          {% else %}<div class="empty">No source data yet.</div>{% endif %}
        </section>

        <section class="card">
          <h2>Recent scans</h2>
          {% if recent %}
          <table>
            <thead><tr><th>Time</th><th>QR</th><th>Campaign</th><th>IP</th><th>Device</th></tr></thead>
            <tbody>
              {% for row in recent %}
              <tr>
                <td class="small muted">{{ row.scanned_at }}</td>
                <td>{{ row.slug }}</td>
                <td>{{ row.campaign or '-' }}</td>
                <td class="small muted">{{ row.ip_address }}</td>
                <td class="small muted">{{ row.user_agent[:120] }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
          {% else %}<div class="empty">No recent scans yet.</div>{% endif %}
        </section>
        """,
        title=f"QR Stats - {business_name}",
        user_name=user_name,
        business_name=business_name,
        user_key=user_key,
        total=total,
        unique_qrs=unique_qrs,
        campaigns=campaigns,
        last_scan=last_scan,
        by_qr=by_qr,
        recent=recent,
        by_source=by_source,
        csv_url=csv_url,
        json_url=json_url,
    )


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
else:
    init_db()
