"""
Mailing for the hub: contacts, sample and beat packs, and campaigns.

Sending stays off until MAIL_MODE=resend is set. In the default dry_run mode a
campaign only reports who it would reach. Sending also needs confirm=true in the
request, so a stray request can't mail anyone.

Every email carries an unsubscribe link and the sender's postal address. Anyone
who unsubscribed is never mailed again, including when they are imported again.

Packs are not stored here. A pack is a name and a Dropbox or Google Drive link,
and the campaign email links to that address directly.
"""

import os
import csv
import io
import re
import base64
import html
import time
import sqlite3
import logging
from contextlib import contextmanager
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from flask import Blueprint, request, jsonify, redirect, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import accounts

logger = logging.getLogger(__name__)

bp = Blueprint("mailing", __name__)

MAIL_MODE = os.getenv("MAIL_MODE", "dry_run")  # "dry_run" or "resend"
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
MAIL_FROM = os.getenv("MAIL_FROM")  # e.g. "CreditCrate <hello@creditcrate.app>"
MAIL_SENDER_ADDRESS = os.getenv("MAIL_SENDER_ADDRESS")  # postal address, required for live sends
PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", "https://api.creditcrate.app/api").rstrip("/")

MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_RECIPIENTS_PER_SEND = 500
SEND_INTERVAL_SECONDS = 0.6  # Resend allows about two requests per second

# Pack links must point at Dropbox or Google Drive

# Open and click tracking. Tokens are signed, so the click redirect can only go to
# links we wrote into an email, never to an arbitrary address.
OPEN_SALT = "creditcrate-track-open"
CLICK_SALT = "creditcrate-track-click"
TRACK_URL_RE = re.compile(r"https?://[^\s<>\"']+")
PIXEL_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
PACK_HOSTS = {"dropbox.com", "drive.google.com", "docs.google.com"}

_db_path = None
_signer = None


def register(app, db_path):
    """Called once from app.py with the shared SQLite file."""
    global _db_path, _signer
    _db_path = db_path
    _signer = URLSafeTimedSerializer(accounts.AUTH_SECRET, salt="creditcrate-mail")
    app.config["MAX_CONTENT_LENGTH"] = MAX_CSV_BYTES + 64 * 1024
    init_db()
    app.register_blueprint(bp)


@contextmanager
def _conn():
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_contacts (
                email TEXT PRIMARY KEY,
                name TEXT,
                instagram TEXT,
                roles TEXT,
                tags TEXT,
                source TEXT,
                unsubscribed_at REAL,
                created_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_packs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                url TEXT,
                created_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                subject TEXT,
                body TEXT,
                pack_id INTEGER,
                status TEXT DEFAULT 'draft',
                created_at REAL,
                sent_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_sends (
                campaign_id INTEGER,
                email TEXT,
                status TEXT,
                error TEXT,
                sent_at REAL,
                PRIMARY KEY (campaign_id, email)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER,
                email TEXT,
                kind TEXT,
                url TEXT,
                created_at REAL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mail_campaigns)")}
        if "track" not in columns:
            conn.execute("ALTER TABLE mail_campaigns ADD COLUMN track INTEGER DEFAULT 1")


# ── Contacts ────────────────────────────────────────────────────────────────

def upsert_contact(conn, email, name="", instagram="", roles="", tags="", source="manual"):
    # Never touches unsubscribed_at, so re-importing a list can't re-subscribe anyone
    conn.execute("""
        INSERT INTO mail_contacts (email, name, instagram, roles, tags, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            name = COALESCE(NULLIF(excluded.name, ''), mail_contacts.name),
            instagram = COALESCE(NULLIF(excluded.instagram, ''), mail_contacts.instagram),
            roles = COALESCE(NULLIF(excluded.roles, ''), mail_contacts.roles),
            tags = COALESCE(NULLIF(excluded.tags, ''), mail_contacts.tags)
    """, (email, name, instagram, roles, tags, source, time.time()))


@bp.route("/api/contacts", methods=["POST"])
@accounts.login_required
def save_contact():
    data = request.get_json(silent=True) or {}
    email = accounts.normalize_email(data.get("email"))
    if not accounts.is_valid_email(email):
        return jsonify({"error": "Not a valid email address."}), 400
    with _conn() as conn:
        upsert_contact(conn, email, data.get("name", ""), data.get("instagram", ""),
                       data.get("roles", ""), data.get("tags", ""), data.get("source", "manual"))
    return jsonify({"ok": True})


@bp.route("/api/contacts/import", methods=["POST"])
@accounts.login_required
def import_contacts():
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "Upload a CSV file in the 'file' field."}), 400
    text = upload.stream.read(MAX_CSV_BYTES + 1).decode("utf-8-sig", errors="replace")
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        return jsonify({"error": "CSV is larger than 10 MB."}), 413

    reader = csv.DictReader(io.StringIO(text))
    if "email" not in {(h or "").strip().lower() for h in (reader.fieldnames or [])}:
        return jsonify({"error": "The CSV needs an 'email' column. Optional: name, instagram, roles, tags."}), 400

    saved = skipped = 0
    with _conn() as conn:
        for row in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            email = accounts.normalize_email(row.get("email"))
            if not accounts.is_valid_email(email):
                skipped += 1
                continue
            upsert_contact(conn, email, row.get("name", ""), row.get("instagram", ""),
                           row.get("roles", ""), row.get("tags", ""), "csv")
            saved += 1
    return jsonify({"saved": saved, "skipped": skipped})


@bp.route("/api/contacts", methods=["GET"])
@accounts.login_required
def list_contacts():
    role = request.args.get("role", "").strip()
    tag = request.args.get("tag", "").strip()
    query = "SELECT email, name, instagram, roles, tags, source, unsubscribed_at FROM mail_contacts WHERE unsubscribed_at IS NULL"
    args = []
    if role:
        query += " AND roles LIKE ?"
        args.append(f"%{role}%")
    if tag:
        query += " AND tags LIKE ?"
        args.append(f"%{tag}%")
    query += " ORDER BY email LIMIT 1000"
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(query, args)]
    return jsonify({"contacts": rows})


# ── Unsubscribe ─────────────────────────────────────────────────────────────

def unsubscribe_link(email):
    token = _signer.dumps({"unsub": email}, salt="creditcrate-mail-unsub")
    return f"{PUBLIC_API_URL}/unsubscribe?token={token}"


@bp.route("/api/unsubscribe", methods=["GET"])
def unsubscribe():
    try:
        email = _signer.loads(request.args.get("token", ""), salt="creditcrate-mail-unsub")["unsub"]
    except (BadSignature, SignatureExpired, KeyError):
        return "This unsubscribe link is not valid.", 400
    with _conn() as conn:
        conn.execute(
            "INSERT INTO mail_contacts (email, source, unsubscribed_at, created_at) VALUES (?, 'unsubscribed', ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET unsubscribed_at = COALESCE(mail_contacts.unsubscribed_at, excluded.unsubscribed_at)",
            (email, time.time(), time.time()),
        )
    return "You have been unsubscribed and will not receive more emails from CreditCrate."


# ── Packs (Dropbox and Google Drive links) ──────────────────────────────────

def normalize_pack_url(raw):
    """Returns a cleaned link to a Dropbox or Google Drive file or folder, or None."""
    url = (raw or "").strip()
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if parsed.scheme != "https" or host not in PACK_HOSTS:
        return None
    if host == "dropbox.com":
        # dl=1 makes Dropbox download the file instead of showing its preview page
        query = [(k, v) for k, v in parse_qsl(parsed.query) if k != "dl"] + [("dl", "1")]
        return urlunparse(parsed._replace(query=urlencode(query)))
    match = re.match(r"^/file/d/([\w-]+)", parsed.path)
    if host == "drive.google.com" and match:
        # Direct download link for a single Drive file
        return f"https://drive.google.com/uc?export=download&id={match.group(1)}"
    return url


@bp.route("/api/packs", methods=["POST"])
@accounts.login_required
def save_pack():
    data = request.get_json(silent=True) or {}
    url = normalize_pack_url(data.get("url"))
    if not url:
        return jsonify({"error": "Paste a Dropbox or Google Drive link (https)."}), 400
    name = (data.get("name") or "").strip()[:120]
    if not name:
        return jsonify({"error": "Give the pack a name."}), 400
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO mail_packs (name, url, created_at) VALUES (?, ?, ?)",
            (name, url, time.time()),
        )
    return jsonify({"id": cur.lastrowid, "name": name, "url": url})


@bp.route("/api/packs", methods=["GET"])
@accounts.login_required
def list_packs():
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, url, created_at FROM mail_packs ORDER BY created_at DESC")]
    return jsonify({"packs": rows})


@bp.route("/api/packs/<int:pack_id>", methods=["DELETE"])
@accounts.login_required
def delete_pack(pack_id):
    with _conn() as conn:
        cur = conn.execute("DELETE FROM mail_packs WHERE id = ?", (pack_id,))
    if cur.rowcount == 0:
        return jsonify({"error": "Pack not found."}), 404
    return jsonify({"ok": True})


# ── Campaigns ───────────────────────────────────────────────────────────────

@bp.route("/api/campaigns", methods=["POST"])
@accounts.login_required
def create_campaign():
    data = request.get_json(silent=True) or {}
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not subject or not body:
        return jsonify({"error": "A campaign needs a subject and a body."}), 400
    with _conn() as conn:
        if data.get("pack_id") and not conn.execute("SELECT 1 FROM mail_packs WHERE id = ?", (data["pack_id"],)).fetchone():
            return jsonify({"error": "Pack not found."}), 404
        cur = conn.execute(
            "INSERT INTO mail_campaigns (name, subject, body, pack_id, track, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ((data.get("name") or subject)[:120], subject, body, data.get("pack_id"),
             1 if data.get("track", True) else 0, time.time()),
        )
    return jsonify({"id": cur.lastrowid})


@bp.route("/api/campaigns", methods=["GET"])
@accounts.login_required
def list_campaigns():
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, subject, pack_id, status, created_at, sent_at FROM mail_campaigns ORDER BY created_at DESC")]
    return jsonify({"campaigns": rows})


def _recipients(conn, campaign_id, role, tag):
    query = """
        SELECT email, name FROM mail_contacts
        WHERE unsubscribed_at IS NULL
          AND email NOT IN (SELECT email FROM mail_sends WHERE campaign_id = ? AND status = 'sent')
    """
    args = [campaign_id]
    if role:
        query += " AND roles LIKE ?"
        args.append(f"%{role}%")
    if tag:
        query += " AND tags LIKE ?"
        args.append(f"%{tag}%")
    query += " ORDER BY email LIMIT ?"
    args.append(MAX_RECIPIENTS_PER_SEND)
    return [dict(r) for r in conn.execute(query, args)]


def _track_link(campaign_id, email, url):
    token = _signer.dumps({"c": campaign_id, "e": email, "u": url}, salt=CLICK_SALT)
    return f"{PUBLIC_API_URL}/t/c/{token}"


def _pixel_url(campaign_id, email):
    token = _signer.dumps({"c": campaign_id, "e": email}, salt=OPEN_SALT)
    return f"{PUBLIC_API_URL}/t/o/{token}/px.gif"


def _html_body(text, pixel_url):
    """HTML version of the text: URLs become links, and the open pixel goes at the end."""
    parts, last = [], 0
    for match in TRACK_URL_RE.finditer(text):
        parts.append(html.escape(text[last:match.start()]))
        url = html.escape(match.group(0))
        parts.append(f'<a href="{url}">{url}</a>')
        last = match.end()
    parts.append(html.escape(text[last:]))
    body = "".join(parts).replace("\n", "<br>\n")
    pixel = f'<img src="{pixel_url}" width="1" height="1" alt="" style="display:none">' if pixel_url else ""
    return f"<!doctype html><html><body>{body}{pixel}</body></html>"


def _render(campaign, contact, pack_url):
    """Returns (text, html) for one recipient."""
    campaign_id, email = campaign["id"], contact["email"]
    unsubscribe = unsubscribe_link(email)
    text = campaign["body"].replace("{{name}}", contact["name"] or "there")
    if pack_url:
        text += f"\n\nDownload the pack: {pack_url}"
    text += f"\n\n---\nNot interested? Unsubscribe: {unsubscribe}"
    if MAIL_SENDER_ADDRESS:
        text += f"\n{MAIL_SENDER_ADDRESS}"

    if not campaign["track"]:
        return text, _html_body(text, None)

    def rewrite(match):
        url = match.group(0).rstrip(".,;:!?)")
        trailing = match.group(0)[len(url):]
        if url.startswith(f"{PUBLIC_API_URL}/unsubscribe"):
            return match.group(0)  # the unsubscribe link itself is never rewritten
        return _track_link(campaign_id, email, url) + trailing

    tracked = TRACK_URL_RE.sub(rewrite, text)
    return tracked, _html_body(tracked, _pixel_url(campaign_id, email))


def _live_config_error():
    if MAIL_MODE != "resend":
        return "MAIL_MODE is not 'resend', so nothing is sent."
    missing = [k for k, v in {"RESEND_API_KEY": RESEND_API_KEY, "MAIL_FROM": MAIL_FROM,
                              "MAIL_SENDER_ADDRESS": MAIL_SENDER_ADDRESS}.items() if not v]
    return f"Set {', '.join(missing)} before sending." if missing else None


def _send_resend(to, subject, text, html_body, unsub_url):
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": MAIL_FROM, "to": [to], "subject": subject, "text": text, "html": html_body,
              "headers": {"List-Unsubscribe": f"<{unsub_url}>"}},
        timeout=15,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend {resp.status_code}: {resp.text[:200]}")


def _record_event(campaign_id, email, kind, url=None):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO mail_events (campaign_id, email, kind, url, created_at) VALUES (?, ?, ?, ?, ?)",
            (campaign_id, email, kind, url, time.time()),
        )


@bp.route("/api/t/o/<token>/px.gif", methods=["GET"])
def track_open(token):
    try:
        data = _signer.loads(token, salt=OPEN_SALT)
        _record_event(data["c"], data["e"], "open")
    except (BadSignature, SignatureExpired, KeyError):
        pass  # a broken or old pixel still gets the image, nothing is recorded
    response = Response(PIXEL_GIF, mimetype="image/gif")
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/t/c/<token>", methods=["GET"])
def track_click(token):
    try:
        data = _signer.loads(token, salt=CLICK_SALT)
    except (BadSignature, SignatureExpired):
        return "This link is not valid.", 404
    url = data["u"]
    if urlparse(url).scheme not in ("http", "https"):
        return "This link is not valid.", 404
    _record_event(data["c"], data["e"], "click", url)
    return redirect(url, code=302)


@bp.route("/api/campaigns/<int:campaign_id>/stats", methods=["GET"])
@accounts.login_required
def campaign_stats(campaign_id):
    with _conn() as conn:
        campaign = conn.execute("SELECT track FROM mail_campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not campaign:
            return jsonify({"error": "Campaign not found."}), 404
        sent = conn.execute(
            "SELECT COUNT(*) FROM mail_sends WHERE campaign_id = ? AND status = 'sent'", (campaign_id,)
        ).fetchone()[0]
        opens = conn.execute(
            "SELECT COUNT(*) AS events, COUNT(DISTINCT email) AS people FROM mail_events WHERE campaign_id = ? AND kind = 'open'",
            (campaign_id,),
        ).fetchone()
        clicks = conn.execute(
            "SELECT COUNT(*) AS events, COUNT(DISTINCT email) AS people FROM mail_events WHERE campaign_id = ? AND kind = 'click'",
            (campaign_id,),
        ).fetchone()
        links = [dict(r) for r in conn.execute(
            "SELECT url, COUNT(*) AS clicks, COUNT(DISTINCT email) AS people FROM mail_events "
            "WHERE campaign_id = ? AND kind = 'click' GROUP BY url ORDER BY clicks DESC LIMIT 20",
            (campaign_id,),
        )]
    return jsonify({
        "tracking": bool(campaign["track"]),
        "sent": sent,
        "opened": opens["people"],
        "open_events": opens["events"],
        "clicked": clicks["people"],
        "click_events": clicks["events"],
        "links": links,
    })


@bp.route("/api/campaigns/<int:campaign_id>/send", methods=["POST"])
@accounts.login_required
def send_campaign(campaign_id):
    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "").strip()
    tag = (data.get("tag") or "").strip()

    with _conn() as conn:
        campaign = conn.execute("SELECT * FROM mail_campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not campaign:
            return jsonify({"error": "Campaign not found."}), 404
        recipients = _recipients(conn, campaign_id, role, tag)

    if not data.get("confirm"):
        return jsonify({
            "mode": MAIL_MODE,
            "dry_run": True,
            "would_send": len(recipients),
            "sample": [r["email"] for r in recipients[:5]],
            "message": "Nothing was sent. Repeat the request with confirm: true to send.",
        })

    config_error = _live_config_error()
    if config_error:
        return jsonify({"error": config_error}), 409

    with _conn() as conn:
        pack = conn.execute("SELECT url FROM mail_packs WHERE id = ?", (campaign["pack_id"],)).fetchone() if campaign["pack_id"] else None
    pack_url = pack["url"] if pack else None
    sent = failed = 0
    for contact in recipients:
        text, html_body = _render(campaign, contact, pack_url)
        try:
            _send_resend(contact["email"], campaign["subject"], text, html_body, unsubscribe_link(contact["email"]))
            status, error = "sent", None
            sent += 1
        except (requests.RequestException, RuntimeError) as e:
            logger.warning(f"Send to {contact['email']} failed: {e}")
            status, error = "failed", str(e)[:300]
            failed += 1
        with _conn() as conn:
            conn.execute(
                "INSERT INTO mail_sends (campaign_id, email, status, error, sent_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(campaign_id, email) DO UPDATE SET status = excluded.status, error = excluded.error, sent_at = excluded.sent_at",
                (campaign_id, contact["email"], status, error, time.time()),
            )
        time.sleep(SEND_INTERVAL_SECONDS)

    with _conn() as conn:
        remaining = len(_recipients(conn, campaign_id, role, tag))
        if remaining == 0:
            conn.execute("UPDATE mail_campaigns SET status = 'sent', sent_at = ? WHERE id = ?", (time.time(), campaign_id))
    return jsonify({"sent": sent, "failed": failed, "remaining": remaining})
