import os
import string
import random
import sqlite3
import base64
from io import BytesIO
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, abort, send_file
)
from werkzeug.middleware.proxy_fix import ProxyFix
import qrcode
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers.pil import RoundedModuleDrawer

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE = os.path.join(DATA_DIR, "qrtrack.db")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            original_url TEXT NOT NULL,
            short_code  TEXT UNIQUE NOT NULL,
            title       TEXT,
            created_at  TEXT DEFAULT (datetime('now', 'localtime')),
            scan_count  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS scans (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id    INTEGER NOT NULL,
            scanned_at TEXT DEFAULT (datetime('now', 'localtime')),
            user_agent TEXT,
            ip_address TEXT,
            FOREIGN KEY (link_id) REFERENCES links(id)
        );
    """)
    db.commit()
    db.close()


def generate_short_code(length: int = 7) -> str:
    chars = string.ascii_letters + string.digits
    db = get_db()
    try:
        while True:
            code = "".join(random.choices(chars, k=length))
            row = db.execute(
                "SELECT id FROM links WHERE short_code = ?", (code,)
            ).fetchone()
            if not row:
                return code
    finally:
        db.close()


def build_qr_image(data: str):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    try:
        return qr.make_image(
            image_factory=StyledPilImage,
            module_drawer=RoundedModuleDrawer(),
        )
    except Exception:
        return qr.make_image(fill_color="#1a1a2e", back_color="white")


def make_qr_b64(data: str) -> str:
    """Return a base64-encoded PNG of the QR code."""
    img = build_qr_image(data)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    db = get_db()
    links = db.execute(
        "SELECT * FROM links ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    return render_template("dashboard.html", links=links)


@app.route("/create", methods=["POST"])
def create():
    data = request.get_json(silent=True) or request.form
    original_url = (data.get("url") or "").strip()
    title = (data.get("title") or "").strip()

    if not original_url:
        return jsonify({"error": "URL is required"}), 400

    if not original_url.startswith(("http://", "https://")):
        original_url = "https://" + original_url

    short_code = generate_short_code()
    base_url = request.host_url.rstrip("/")
    tracking_url = f"{base_url}/r/{short_code}"

    # Generate QR preview image (stored as base64 in response)
    img_b64 = make_qr_b64(tracking_url)

    # Persist to DB
    db = get_db()
    db.execute(
        "INSERT INTO links (original_url, short_code, title) VALUES (?, ?, ?)",
        (original_url, short_code, title or original_url[:80]),
    )
    db.commit()
    db.close()

    return jsonify({
        "success": True,
        "short_code": short_code,
        "tracking_url": tracking_url,
        "qr_image": f"data:image/png;base64,{img_b64}",
        "qr_image_url": url_for("qr_image", short_code=short_code),
        "stats_url": url_for("stats", short_code=short_code),
    })


@app.route("/qrcode/<short_code>.png")
def qr_image(short_code):
    base_url = request.host_url.rstrip("/")
    tracking_url = f"{base_url}/r/{short_code}"
    img = build_qr_image(tracking_url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/r/<short_code>")
def redirect_and_track(short_code):
    db = get_db()
    link = db.execute(
        "SELECT * FROM links WHERE short_code = ?", (short_code,)
    ).fetchone()

    if not link:
        db.close()
        abort(404)

    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = forwarded.split(",")[0].strip() if forwarded else request.remote_addr
    ua = (request.headers.get("User-Agent") or "")[:500]

    db.execute(
        "INSERT INTO scans (link_id, user_agent, ip_address) VALUES (?, ?, ?)",
        (link["id"], ua, ip),
    )
    db.execute(
        "UPDATE links SET scan_count = scan_count + 1 WHERE id = ?",
        (link["id"],),
    )
    db.commit()

    original_url = link["original_url"]
    db.close()
    return redirect(original_url, code=302)


@app.route("/stats/<short_code>")
def stats(short_code):
    db = get_db()
    link = db.execute(
        "SELECT * FROM links WHERE short_code = ?", (short_code,)
    ).fetchone()

    if not link:
        db.close()
        abort(404)

    recent_scans = db.execute(
        "SELECT * FROM scans WHERE link_id = ? ORDER BY scanned_at DESC LIMIT 20",
        (link["id"],),
    ).fetchall()

    daily_stats = db.execute(
        """SELECT DATE(scanned_at) AS date, COUNT(*) AS count
           FROM scans WHERE link_id = ?
           GROUP BY DATE(scanned_at)
           ORDER BY date ASC LIMIT 30""",
        (link["id"],),
    ).fetchall()

    db.close()

    base_url = request.host_url.rstrip("/")
    tracking_url = f"{base_url}/r/{short_code}"
    qr_url = url_for("qr_image", short_code=short_code)

    return render_template(
        "stats.html",
        link=link,
        recent_scans=recent_scans,
        daily_stats=daily_stats,
        tracking_url=tracking_url,
        qr_url=qr_url,
    )


@app.route("/api/stats/<short_code>")
def api_stats(short_code):
    db = get_db()
    link = db.execute(
        "SELECT * FROM links WHERE short_code = ?", (short_code,)
    ).fetchone()

    if not link:
        db.close()
        return jsonify({"error": "Not found"}), 404

    daily_stats = db.execute(
        """SELECT DATE(scanned_at) AS date, COUNT(*) AS count
           FROM scans WHERE link_id = ?
           GROUP BY DATE(scanned_at)
           ORDER BY date ASC LIMIT 30""",
        (link["id"],),
    ).fetchall()

    db.close()
    return jsonify({
        "short_code": link["short_code"],
        "original_url": link["original_url"],
        "title": link["title"],
        "total_scans": link["scan_count"],
        "created_at": link["created_at"],
        "daily_scans": [
            {"date": s["date"], "count": s["count"]} for s in daily_stats
        ],
    })


@app.route("/delete/<short_code>", methods=["POST"])
def delete_link(short_code):
    db = get_db()
    link = db.execute(
        "SELECT id FROM links WHERE short_code = ?", (short_code,)
    ).fetchone()

    if link:
        db.execute("DELETE FROM scans WHERE link_id = ?", (link["id"],))
        db.execute("DELETE FROM links WHERE id = ?", (link["id"],))
        db.commit()
    db.close()
    return redirect(url_for("dashboard"))


# Initialize DB when app is imported (works for both Flask dev server and Gunicorn)
init_db()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=debug_mode, host="0.0.0.0", port=port)
