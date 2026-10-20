import os
import string
import random
import sqlite3
import base64
import hmac
import secrets
from collections import defaultdict, deque
from io import BytesIO
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, abort, send_file, session
)
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import RequestEntityTooLarge
import qrcode
from PIL import Image, ImageOps, UnidentifiedImageError

app = Flask(__name__)
IS_PRODUCTION = os.environ.get("FLASK_ENV", "production").lower() == "production"
SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_USER = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

if not SECRET_KEY or not ADMIN_USER or not ADMIN_PASSWORD:
    raise RuntimeError(
        "SECRET_KEY, ADMIN_USERNAME, and ADMIN_PASSWORD must be configured as environment variables."
    )

app.secret_key = SECRET_KEY
app.config.update(
    MAX_CONTENT_LENGTH=2 * 1024 * 1024 + 64 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
# Trust forwarding headers only from the single reverse proxy configured for the app.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE = os.path.join(DATA_DIR, "qrtrack.db")
MAX_LOGO_BYTES = 2 * 1024 * 1024
ALLOWED_LOGO_FORMATS = {"PNG", "JPEG", "WEBP"}
Image.MAX_IMAGE_PIXELS = 12_000_000
THAI_TZ = timezone(timedelta(hours=7))
THAI_MONTHS_ABBR = [
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
]

# Credentials only exist in process memory; keep them out of source control.
ADMIN_PASS_HASH = generate_password_hash(ADMIN_PASSWORD)
LOGIN_ATTEMPTS: dict[str, deque[datetime]] = defaultdict(deque)
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW = timedelta(minutes=15)


# User class for Flask-Login
class User(UserMixin):
    def __init__(self, username):
        self.id = username
        self.username = username


def client_ip() -> str:
    """Return the first proxy-provided client address after ProxyFix handling."""
    return request.remote_addr or "unknown"


def anonymize_ip(ip_address: str) -> str:
    """Keep coarse scan-location information without retaining a full IP address."""
    if "." in ip_address:
        pieces = ip_address.split(".")
        if len(pieces) == 4:
            return ".".join(pieces[:3] + ["0"])
    if ":" in ip_address:
        return ":".join(ip_address.split(":")[:4]) + "::"
    return "unknown"


def csrf_token() -> str:
    token = session.get("csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}


@app.before_request
def protect_state_changing_requests():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    submitted_token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    if not hmac.compare_digest(submitted_token, session.get("csrf_token", "")):
        abort(400, description="Invalid CSRF token")
    return None


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self' data:; connect-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com"
    )
    if IS_PRODUCTION and request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.errorhandler(RequestEntityTooLarge)
def request_too_large(_error):
    if request.path == "/create":
        return jsonify({"error": "Logo must be 2 MB or smaller"}), 413
    return "Request too large", 413


@login_manager.user_loader
def load_user(user_id):
    if user_id == ADMIN_USER:
        return User(user_id)
    return None


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
    # Keep uploaded logos with the QR record so regenerated/downloaded images
    # always match the preview, including after an application restart.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(links)")}
    if "logo_data" not in columns:
        db.execute("ALTER TABLE links ADD COLUMN logo_data BLOB")
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


def validate_destination_url(value: str) -> str | None:
    """Allow only well-formed web URLs; QR redirects are never server-side fetches."""
    if len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def build_qr_image(data: str, logo_data: bytes | None = None):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    # Use the standard high-contrast black-and-white QR style.  It provides
    # the clearest scan result and matches conventional official QR codes.
    image = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    if logo_data:
        try:
            logo = Image.open(BytesIO(logo_data))
            logo = ImageOps.exif_transpose(logo).convert("RGBA")
            # Error correction level H permits a prominent centre logo while
            # preserving reliable scanning.  Do not add a square background:
            # transparent pixels in a PNG remain transparent around the logo.
            logo_side = max(1, int(min(image.size) * 0.27))
            logo.thumbnail((logo_side, logo_side), Image.Resampling.LANCZOS)
            position = ((image.width - logo.width) // 2, (image.height - logo.height) // 2)
            image.alpha_composite(logo, position)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
            # A previously stored but unreadable file must not prevent a QR download.
            pass
    return image


def make_qr_b64(data: str, logo_data: bytes | None = None) -> str:
    """Return a base64-encoded PNG of the QR code."""
    img = build_qr_image(data, logo_data)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def utc_now_string() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def to_thai_display(timestamp: str | None, include_time: bool = True) -> str:
    if not timestamp:
        return "-"

    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(timestamp, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue

    if parsed is None:
        return timestamp

    now_thai = datetime.now(THAI_TZ)
    local_candidate = parsed.replace(tzinfo=THAI_TZ)
    thai_time = parsed.astimezone(THAI_TZ)

    # Some existing rows were already stored in Thailand local time.
    # If converting from UTC pushes the value unrealistically into the future,
    # keep the original local timestamp instead of double-shifting it.
    if thai_time > now_thai + timedelta(hours=1):
        thai_time = local_candidate

    return thai_time.strftime("%Y-%m-%d %H:%M" if include_time else "%Y-%m-%d")


def parse_to_thai_time(timestamp: str | None):
    if not timestamp:
        return None

    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(timestamp, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue

    if parsed is None:
        return None

    now_thai = datetime.now(THAI_TZ)
    local_candidate = parsed.replace(tzinfo=THAI_TZ)
    thai_time = parsed.astimezone(THAI_TZ)
    if thai_time > now_thai + timedelta(hours=1):
        thai_time = local_candidate
    return thai_time


def thai_datetime_human(timestamp: str | None, include_time: bool = True) -> str:
    thai_time = parse_to_thai_time(timestamp)
    if thai_time is None:
        return "-"

    month = THAI_MONTHS_ABBR[thai_time.month - 1]
    if include_time:
        return f"{thai_time.day} {month} {thai_time.year} เวลา {thai_time.strftime('%H:%M')} น."
    return f"{thai_time.day} {month} {thai_time.year}"


def serialize_link(link):
    return {
        "id": link["id"],
        "short_code": link["short_code"],
        "title": link["title"],
        "original_url": link["original_url"],
        "created_at": to_thai_display(link["created_at"]),
        "created_at_human": thai_datetime_human(link["created_at"]),
        "created_date_human": thai_datetime_human(link["created_at"], include_time=False),
        "scan_count": link["scan_count"],
    }


def serialize_scan(scan):
    return {
        "id": scan["id"],
        "link_id": scan["link_id"],
        "scanned_at": to_thai_display(scan["scanned_at"]),
        "scanned_at_human": thai_datetime_human(scan["scanned_at"]),
        "user_agent": scan["user_agent"],
        "ip_address": scan["ip_address"],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    
    if request.method == "POST":
        now = datetime.now(timezone.utc)
        attempts = LOGIN_ATTEMPTS[client_ip()]
        while attempts and now - attempts[0] > LOGIN_WINDOW:
            attempts.popleft()
        if len(attempts) >= MAX_LOGIN_ATTEMPTS:
            return render_template("login.html", error="Too many login attempts. Please try again later."), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        if username == ADMIN_USER and check_password_hash(ADMIN_PASS_HASH, password):
            # Start a fresh session after authentication to prevent session fixation.
            session.clear()
            user = User(username)
            login_user(user)
            session.permanent = True
            LOGIN_ATTEMPTS.pop(client_ip(), None)
            return redirect(url_for("index"))
        else:
            attempts.append(now)
            return render_template("login.html", error="ชื่อผู้ใช้ หรือรหัสผ่านไม่ถูกต้อง")
    
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    links = db.execute(
        "SELECT * FROM links ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    return render_template("dashboard.html", links=links)


@app.route("/create", methods=["POST"])
@login_required
def create():
    data = request.get_json(silent=True) or request.form
    original_url = (data.get("url") or "").strip()
    title = (data.get("title") or "").strip()

    if not original_url:
        return jsonify({"error": "URL is required"}), 400

    if not original_url.startswith(("http://", "https://")):
        original_url = "https://" + original_url
    original_url = validate_destination_url(original_url)
    if not original_url:
        return jsonify({"error": "Please enter a valid http or https URL"}), 400

    logo_data = None
    logo_file = request.files.get("logo")
    if logo_file and logo_file.filename:
        logo_data = logo_file.read(MAX_LOGO_BYTES + 1)
        if len(logo_data) > MAX_LOGO_BYTES:
            return jsonify({"error": "Logo must be 2 MB or smaller"}), 400
        try:
            with Image.open(BytesIO(logo_data)) as logo:
                if logo.format not in ALLOWED_LOGO_FORMATS:
                    raise ValueError
                logo.verify()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
            return jsonify({"error": "Please upload a PNG, JPG, or WEBP image"}), 400

    short_code = generate_short_code()
    base_url = request.host_url.rstrip("/")
    tracking_url = f"{base_url}/r/{short_code}"

    # Generate QR preview image (stored as base64 in response)
    img_b64 = make_qr_b64(tracking_url, logo_data)

    # Persist to DB
    db = get_db()
    db.execute(
        "INSERT INTO links (original_url, short_code, title, created_at, logo_data) VALUES (?, ?, ?, ?, ?)",
        (original_url, short_code, title or original_url[:80], utc_now_string(), logo_data),
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
@login_required
def qr_image(short_code):
    db = get_db()
    link = db.execute("SELECT logo_data FROM links WHERE short_code = ?", (short_code,)).fetchone()
    db.close()
    if not link:
        abort(404)
    base_url = request.host_url.rstrip("/")
    tracking_url = f"{base_url}/r/{short_code}"
    img = build_qr_image(tracking_url, link["logo_data"])
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/r/<short_code>")
def redirect_tracker(short_code):

    db = get_db()
    link = db.execute(
        "SELECT * FROM links WHERE short_code = ?", (short_code,)
    ).fetchone()

    if not link:
        db.close()
        abort(404)

    ip = anonymize_ip(client_ip())
    ua = (request.headers.get("User-Agent") or "")[:200]

    db.execute(
        "INSERT INTO scans (link_id, scanned_at, user_agent, ip_address) VALUES (?, ?, ?, ?)",
        (link["id"], utc_now_string(), ua, ip),
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
@login_required
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
        """SELECT DATE(datetime(scanned_at, '+7 hours')) AS date, COUNT(*) AS count
           FROM scans WHERE link_id = ?
           GROUP BY DATE(datetime(scanned_at, '+7 hours'))
           ORDER BY date ASC LIMIT 30""",
        (link["id"],),
    ).fetchall()

    db.close()

    base_url = request.host_url.rstrip("/")
    tracking_url = f"{base_url}/r/{short_code}"
    qr_url = url_for("qr_image", short_code=short_code)
    formatted_link = serialize_link(link)
    formatted_scans = [serialize_scan(scan) for scan in recent_scans]

    return render_template(
        "stats.html",
        link=formatted_link,
        recent_scans=formatted_scans,
        daily_stats=daily_stats,
        tracking_url=tracking_url,
        qr_url=qr_url,
    )


@app.route("/api/stats/<short_code>")
@login_required
def api_stats(short_code):
    db = get_db()
    link = db.execute(
        "SELECT * FROM links WHERE short_code = ?", (short_code,)
    ).fetchone()

    if not link:
        db.close()
        return jsonify({"error": "Not found"}), 404

    daily_stats = db.execute(
        """SELECT DATE(datetime(scanned_at, '+7 hours')) AS date, COUNT(*) AS count
           FROM scans WHERE link_id = ?
           GROUP BY DATE(datetime(scanned_at, '+7 hours'))
           ORDER BY date ASC LIMIT 30""",
        (link["id"],),
    ).fetchall()

    db.close()
    return jsonify({
        "short_code": link["short_code"],
        "original_url": link["original_url"],
        "title": link["title"],
        "total_scans": link["scan_count"],
        "created_at": to_thai_display(link["created_at"]),
        "daily_scans": [
            {"date": s["date"], "count": s["count"]} for s in daily_stats
        ],
    })


@app.route("/delete/<short_code>", methods=["POST"])
@login_required
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


@app.route("/api/dashboard-stats")
@login_required
def api_dashboard_stats():
    """API endpoint for real-time dashboard updates"""
    db = get_db()
    links = db.execute(
        "SELECT id, short_code, title, original_url, created_at, scan_count FROM links ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    
    return jsonify({
        "total_links": len(links),
        "total_scans": sum(l["scan_count"] for l in links),
        "links": [serialize_link(link) for link in links],
    })



# Initialize DB when app is imported (works for both Flask dev server and Gunicorn)
init_db()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=debug_mode, host="0.0.0.0", port=port)
