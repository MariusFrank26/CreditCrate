"""
Mailing for the hub: contacts, sample and beat packs, and campaigns.

Sending goes through the signed-in user's own Gmail account (see gmail.py). A
preview only reports who would be reached. Sending also needs confirm=true in the
request, so a stray request can't mail anyone, and a daily cap of 500 applies.

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
import random
import smtplib
import threading
import time
import sqlite3
import logging
from contextlib import contextmanager
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from flask import Blueprint, request, jsonify, redirect, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import accounts
import gmail

logger = logging.getLogger(__name__)

bp = Blueprint("mailing", __name__)

MAIL_SENDER_ADDRESS = os.getenv("MAIL_SENDER_ADDRESS")  # postal address, required for live sends
PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", "https://api.creditcrate.app/api").rstrip("/")

MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_RECIPIENTS_PER_SEND = 500
SEND_INTERVAL_SECONDS = (2.0, 4.0)  # a random pause between emails, like a person sending by hand
_sending = set()  # campaign ids currently being sent
_sending_lock = threading.Lock()

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


def _migrate_owner(conn, table, create_sql, columns):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "owner" in existing:
        return
    first = conn.execute("SELECT email FROM users ORDER BY created_at LIMIT 1").fetchone()
    owner = first[0] if first else None
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
    conn.execute(create_sql)
    conn.execute(f"INSERT INTO {table} (owner, {columns}) SELECT ?, {columns} FROM {table}_old", (owner,))
    conn.execute(f"DROP TABLE {table}_old")


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_contacts (
                owner TEXT,
                email TEXT,
                name TEXT,
                instagram TEXT,
                roles TEXT,
                tags TEXT,
                source TEXT,
                unsubscribed_at REAL,
                created_at REAL,
                PRIMARY KEY (owner, email)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_packs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                name TEXT,
                url TEXT,
                created_at REAL
            )
        """)
        _migrate_owner(conn, "mail_contacts", """
            CREATE TABLE mail_contacts (
                owner TEXT, email TEXT, name TEXT, instagram TEXT, roles TEXT, tags TEXT, source TEXT,
                unsubscribed_at REAL, created_at REAL, PRIMARY KEY (owner, email)
            )""", "email, name, instagram, roles, tags, source, unsubscribed_at, created_at")
        contact_columns = {row[1] for row in conn.execute("PRAGMA table_info(mail_contacts)")}
        if "album" not in contact_columns:
            conn.execute("ALTER TABLE mail_contacts ADD COLUMN album TEXT")
        _migrate_owner(conn, "mail_packs", """
            CREATE TABLE mail_packs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, name TEXT, url TEXT, created_at REAL
            )""", "id, name, url, created_at")
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
        if "owner" not in columns:
            conn.execute("ALTER TABLE mail_campaigns ADD COLUMN owner TEXT")
        sent_columns = {row[1] for row in conn.execute("PRAGMA table_info(mail_sends)")}
        if "sender" not in sent_columns:
            conn.execute("ALTER TABLE mail_sends ADD COLUMN sender TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_accounts (
                owner TEXT PRIMARY KEY,
                address TEXT,
                password_enc TEXT,
                updated_at REAL
            )
        """)


# ── Contacts ────────────────────────────────────────────────────────────────

def upsert_contact(conn, owner, email, name="", instagram="", roles="", tags="", source="manual", album=""):
    # Never touches unsubscribed_at, so re-importing a list can't re-subscribe anyone
    conn.execute("""
        INSERT INTO mail_contacts (owner, email, name, instagram, roles, tags, source, album, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner, email) DO UPDATE SET
            name = COALESCE(NULLIF(excluded.name, ''), mail_contacts.name),
            instagram = COALESCE(NULLIF(excluded.instagram, ''), mail_contacts.instagram),
            roles = COALESCE(NULLIF(excluded.roles, ''), mail_contacts.roles),
            tags = COALESCE(NULLIF(excluded.tags, ''), mail_contacts.tags),
            album = COALESCE(NULLIF(excluded.album, ''), mail_contacts.album)
    """, (owner, email, name, instagram, roles, tags, source, album, time.time()))


@bp.route("/api/contacts", methods=["POST"])
@accounts.login_required
def save_contact():
    data = request.get_json(silent=True) or {}
    email = accounts.normalize_email(data.get("email"))
    if not accounts.is_valid_email(email):
        return jsonify({"error": "Not a valid email address."}), 400
    with _conn() as conn:
        upsert_contact(conn, accounts.current_email(), email, data.get("name", ""), data.get("instagram", ""),
                       data.get("roles", ""), data.get("tags", ""), data.get("source", "manual"),
                       str(data.get("album", ""))[:200])
    return jsonify({"ok": True})


@bp.route("/api/contacts/bulk", methods=["POST"])
@accounts.login_required
def save_contacts_bulk():
    """Saves many contacts at once, e.g. the emails found for an album in Credits."""
    data = request.get_json(silent=True) or {}
    items = data.get("contacts")
    if not isinstance(items, list) or len(items) > 500:
        return jsonify({"error": "Send a list of up to 500 contacts."}), 400
    source = str(data.get("source", "manual"))[:40]
    album = str(data.get("album", ""))[:200]
    saved = skipped = 0
    with _conn() as conn:
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            email = accounts.normalize_email(item.get("email"))
            if not accounts.is_valid_email(email):
                skipped += 1
                continue
            upsert_contact(conn, accounts.current_email(), email, str(item.get("name", ""))[:200],
                           str(item.get("instagram", ""))[:100], str(item.get("roles", ""))[:200],
                           "", source, album)
            saved += 1
    return jsonify({"saved": saved, "skipped": skipped})


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
            upsert_contact(conn, accounts.current_email(), email, row.get("name", ""), row.get("instagram", ""),
                           row.get("roles", ""), row.get("tags", ""), "csv")
            saved += 1
    return jsonify({"saved": saved, "skipped": skipped})


@bp.route("/api/contacts", methods=["GET"])
@accounts.login_required
def list_contacts():
    role = request.args.get("role", "").strip()
    tag = request.args.get("tag", "").strip()
    query = "SELECT email, name, instagram, roles, tags, source, album, unsubscribed_at FROM mail_contacts WHERE owner = ? AND unsubscribed_at IS NULL"
    args = [accounts.current_email()]
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

def unsubscribe_link(email, owner):
    token = _signer.dumps({"unsub": email, "o": owner}, salt="creditcrate-mail-unsub")
    return f"{PUBLIC_API_URL}/unsubscribe?token={token}"


@bp.route("/api/unsubscribe", methods=["GET"])
def unsubscribe():
    try:
        data = _signer.loads(request.args.get("token", ""), salt="creditcrate-mail-unsub")
        email, owner = data["unsub"], data["o"]
    except (BadSignature, SignatureExpired, KeyError):
        return "This unsubscribe link is not valid.", 400
    with _conn() as conn:
        conn.execute(
            "INSERT INTO mail_contacts (owner, email, source, unsubscribed_at, created_at) VALUES (?, ?, 'unsubscribed', ?, ?) "
            "ON CONFLICT(owner, email) DO UPDATE SET unsubscribed_at = COALESCE(mail_contacts.unsubscribed_at, excluded.unsubscribed_at)",
            (owner, email, time.time(), time.time()),
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
            "INSERT INTO mail_packs (owner, name, url, created_at) VALUES (?, ?, ?, ?)",
            (accounts.current_email(), name, url, time.time()),
        )
    return jsonify({"id": cur.lastrowid, "name": name, "url": url})


@bp.route("/api/packs", methods=["GET"])
@accounts.login_required
def list_packs():
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, url, created_at FROM mail_packs WHERE owner = ? ORDER BY created_at DESC",
            (accounts.current_email(),))]
    return jsonify({"packs": rows})


@bp.route("/api/packs/<int:pack_id>", methods=["DELETE"])
@accounts.login_required
def delete_pack(pack_id):
    with _conn() as conn:
        cur = conn.execute("DELETE FROM mail_packs WHERE id = ? AND owner = ?", (pack_id, accounts.current_email()))
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
        if data.get("pack_id") and not conn.execute(
                "SELECT 1 FROM mail_packs WHERE id = ? AND owner = ?", (data["pack_id"], accounts.current_email())).fetchone():
            return jsonify({"error": "Pack not found."}), 404
        cur = conn.execute(
            "INSERT INTO mail_campaigns (name, subject, body, pack_id, track, created_at, owner) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ((data.get("name") or subject)[:120], subject, body, data.get("pack_id"),
             1 if data.get("track", True) else 0, time.time(), accounts.current_email()),
        )
    return jsonify({"id": cur.lastrowid})


@bp.route("/api/campaigns", methods=["GET"])
@accounts.login_required
def list_campaigns():
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT c.id, c.name, c.subject, c.pack_id, c.status, c.created_at, c.sent_at, c.track, "
            "(SELECT COUNT(*) FROM mail_sends s WHERE s.campaign_id = c.id AND s.status = 'sent') AS sent, "
            "(SELECT COUNT(DISTINCT e.email) FROM mail_events e WHERE e.campaign_id = c.id AND e.kind = 'open') AS opened, "
            "(SELECT COUNT(DISTINCT e.email) FROM mail_events e WHERE e.campaign_id = c.id AND e.kind = 'click') AS clicked "
            "FROM mail_campaigns c WHERE c.owner = ? ORDER BY c.created_at DESC",
            (accounts.current_email(),))]
    return jsonify({"campaigns": rows})


def _recipients(conn, campaign_id, owner, role, tag):
    query = """
        SELECT email, name FROM mail_contacts
        WHERE owner = ? AND unsubscribed_at IS NULL
          AND email NOT IN (SELECT email FROM mail_sends WHERE campaign_id = ? AND status = 'sent')
    """
    args = [owner, campaign_id]
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
    unsubscribe = unsubscribe_link(email, campaign["owner"])
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


def _load_account(owner):
    with _conn() as conn:
        row = conn.execute("SELECT address, password_enc FROM mail_accounts WHERE owner = ?", (owner,)).fetchone()
    return (row["address"], row["password_enc"]) if row else None


def _sent_today(address):
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM mail_sends WHERE sender = ? AND status = 'sent' AND sent_at > ?",
            (address, time.time() - 86400),
        ).fetchone()[0]


def _account_status(owner):
    account = _load_account(owner)
    if not account:
        return {"connected": False, "daily_limit": gmail.DAILY_LIMIT, "sent_today": 0}
    address = account[0]
    return {"connected": True, "address": address, "daily_limit": gmail.DAILY_LIMIT, "sent_today": _sent_today(address)}


def _log_send(campaign_id, email, sender, status, error):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO mail_sends (campaign_id, email, status, error, sent_at, sender) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(campaign_id, email) DO UPDATE SET status = excluded.status, error = excluded.error, "
            "sent_at = excluded.sent_at, sender = excluded.sender",
            (campaign_id, email, status, error, time.time(), sender),
        )


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


@bp.route("/api/mail/account", methods=["GET"])
@accounts.login_required
def get_account():
    return jsonify(_account_status(accounts.current_email()))


@bp.route("/api/mail/account", methods=["POST"])
@accounts.login_required
def connect_account():
    data = request.get_json(silent=True) or {}
    address = accounts.normalize_email(data.get("address"))
    app_password = (data.get("app_password") or "").replace(" ", "")
    if not accounts.is_valid_email(address):
        return jsonify({"error": "Enter the Gmail address you want to send from."}), 400
    if len(app_password) < 8:
        return jsonify({"error": "Paste the 16-character app password from your Google account."}), 400
    try:
        encrypted = gmail.encrypt_password(app_password)
    except gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 503
    try:
        gmail.check_login(address, app_password)
    except smtplib.SMTPAuthenticationError:
        return jsonify({"error": "Google did not accept this address and app password."}), 401
    except (smtplib.SMTPException, OSError):
        return jsonify({"error": "Could not reach Gmail. Try again in a moment."}), 502
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO mail_accounts (owner, address, password_enc, updated_at) VALUES (?, ?, ?, ?)",
            (accounts.current_email(), address, encrypted, time.time()),
        )
    return jsonify(_account_status(accounts.current_email()))


@bp.route("/api/mail/account", methods=["DELETE"])
@accounts.login_required
def disconnect_account():
    with _conn() as conn:
        conn.execute("DELETE FROM mail_accounts WHERE owner = ?", (accounts.current_email(),))
    return jsonify(_account_status(accounts.current_email()))


@bp.route("/api/campaigns/<int:campaign_id>/stats", methods=["GET"])
@accounts.login_required
def campaign_stats(campaign_id):
    with _conn() as conn:
        campaign = conn.execute(
            "SELECT track, status FROM mail_campaigns WHERE id = ? AND owner = ?",
            (campaign_id, accounts.current_email()),
        ).fetchone()
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
        "status": campaign["status"],
        "tracking": bool(campaign["track"]),
        "sent": sent,
        "opened": opens["people"],
        "open_events": opens["events"],
        "clicked": clicks["people"],
        "click_events": clicks["events"],
        "links": links,
    })


@bp.route("/api/activity", methods=["GET"])
@accounts.login_required
def activity():
    owner = accounts.current_email()
    with _conn() as conn:
        recent = [dict(r) for r in conn.execute(
            "SELECT e.kind, e.email, e.created_at, c.subject, COALESCE(NULLIF(m.name, ''), e.email) AS name "
            "FROM mail_events e JOIN mail_campaigns c ON c.id = e.campaign_id AND c.owner = ? "
            "LEFT JOIN mail_contacts m ON m.email = e.email AND m.owner = c.owner ORDER BY e.created_at DESC LIMIT 8",
            (owner,),
        )]
        top = [dict(r) for r in conn.execute(
            "SELECT e.email, COUNT(*) AS opens, COALESCE(NULLIF(m.name, ''), e.email) AS name "
            "FROM mail_events e JOIN mail_campaigns c ON c.id = e.campaign_id AND c.owner = ? "
            "LEFT JOIN mail_contacts m ON m.email = e.email AND m.owner = c.owner WHERE e.kind = 'open' "
            "GROUP BY e.email ORDER BY opens DESC LIMIT 3",
            (owner,),
        )]
    return jsonify({"recent": recent, "top": top})


def _run_campaign(campaign, batch, pack_url, role, tag, address, app_password):
    """Runs in a background thread. Sends one message per person over one Gmail session."""
    status = "sent"
    try:
        with gmail.Sender(address, app_password) as sender:
            for contact in batch:
                text, html_body = _render(campaign, contact, pack_url)
                try:
                    sender.send(contact["email"], campaign["subject"], text, html_body,
                                unsubscribe_link(contact["email"], campaign["owner"]))
                    _log_send(campaign["id"], contact["email"], address, "sent", None)
                except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
                    logger.warning(f"Send to {contact['email']} refused: {e}")
                    _log_send(campaign["id"], contact["email"], address, "failed", str(e)[:300])
                time.sleep(random.uniform(*SEND_INTERVAL_SECONDS))
    except smtplib.SMTPAuthenticationError:
        status = "stopped: Google rejected the login"
    except (smtplib.SMTPException, OSError) as e:
        status = f"stopped: {str(e)[:120]}"
    except gmail.CredentialError as e:
        status = f"stopped: {str(e)[:120]}"
    with _conn() as conn:
        remaining = len(_recipients(conn, campaign["id"], campaign["owner"], role, tag))
    if status == "sent" and remaining > 0:
        status = "partial"  # the daily limit or a stop left people waiting
    with _conn() as conn:
        conn.execute(
            "UPDATE mail_campaigns SET status = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END WHERE id = ?",
            (status, status, time.time(), campaign["id"]),
        )
    with _sending_lock:
        _sending.discard(campaign["id"])


@bp.route("/api/campaigns/<int:campaign_id>/send", methods=["POST"])
@accounts.login_required
def send_campaign(campaign_id):
    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "").strip()
    tag = (data.get("tag") or "").strip()
    owner = accounts.current_email()

    with _conn() as conn:
        campaign = conn.execute(
            "SELECT * FROM mail_campaigns WHERE id = ? AND owner = ?", (campaign_id, owner)
        ).fetchone()
        if not campaign:
            return jsonify({"error": "Campaign not found."}), 404
        recipients = _recipients(conn, campaign_id, owner, role, tag)
        pack = conn.execute("SELECT url FROM mail_packs WHERE id = ? AND owner = ?", (campaign["pack_id"], owner)).fetchone() \
            if campaign["pack_id"] else None
    pack_url = pack["url"] if pack else None
    account = _load_account(owner)

    if not data.get("confirm"):
        return jsonify({
            "dry_run": True,
            "can_send": bool(account),
            "would_send": len(recipients),
            "sample": [r["email"] for r in recipients[:5]],
            "message": "Nothing was sent. Repeat the request with confirm: true to send.",
        })

    if not account:
        return jsonify({"error": "Connect your Gmail account first."}), 409
    address, encrypted = account
    try:
        app_password = gmail.decrypt_password(encrypted)
    except gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 409

    with _sending_lock:
        if campaign_id in _sending:
            return jsonify({"error": "This campaign is already sending."}), 409
        allowance = gmail.DAILY_LIMIT - _sent_today(address)
        if allowance <= 0:
            return jsonify({"error": f"You reached the daily limit of {gmail.DAILY_LIMIT} emails. Try again tomorrow."}), 409
        batch = recipients[:allowance]
        if not batch:
            return jsonify({"error": "Nobody left to send to in this campaign."}), 409
        _sending.add(campaign_id)

    with _conn() as conn:
        conn.execute("UPDATE mail_campaigns SET status = 'sending' WHERE id = ?", (campaign_id,))
    thread = threading.Thread(
        target=_run_campaign,
        args=(dict(campaign), batch, pack_url, role, tag, address, app_password),
        daemon=True,
    )
    thread.start()
    return jsonify({
        "started": True,
        "queued": len(batch),
        "waiting": len(recipients) - len(batch),
        "left_today": allowance - len(batch),
    })


@bp.route("/api/mail/test", methods=["POST"])
@accounts.login_required
def send_test():
    data = request.get_json(silent=True) or {}
    owner = accounts.current_email()
    campaign = None
    with _conn() as conn:
        row = conn.execute("SELECT * FROM mail_campaigns WHERE id = ? AND owner = ?",
                           (data.get("campaign_id"), owner)).fetchone()
        if row:
            campaign = dict(row)
            pack = conn.execute("SELECT url FROM mail_packs WHERE id = ? AND owner = ?", (row["pack_id"], owner)).fetchone() \
                if row["pack_id"] else None
    if not campaign:
        return jsonify({"error": "Campaign not found."}), 404
    account = _load_account(owner)
    if not account:
        return jsonify({"error": "Connect your Gmail account first."}), 409
    address, encrypted = account
    try:
        app_password = gmail.decrypt_password(encrypted)
        me = {"email": address, "name": "Test"}
        text, html_body = _render(campaign, me, pack["url"] if pack else None)
        with gmail.Sender(address, app_password) as sender:
            sender.send(address, "[Test] " + campaign["subject"], text, html_body, unsubscribe_link(address, owner))
    except gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 409
    except (smtplib.SMTPException, OSError) as e:
        return jsonify({"error": f"Gmail did not accept the test: {str(e)[:150]}"}), 502
    return jsonify({"ok": True, "sent_to": address})
