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
import json
import random
import signal
import statistics
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

PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", "https://api.creditcrate.app/api").rstrip("/")

MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_RECIPIENTS_PER_SEND = 500
SEND_INTERVAL_SECONDS = (2.0, 4.0)  # a random pause between emails, like a person sending by hand
# Sends are queued in the database (mail_queue) and a background worker sends them one by one.
# Because the queue is saved, a restart or redeploy does not lose anything: the worker picks up
# where it left off when the backend starts again.
_queue_lock = threading.Lock()  # guards claiming and enqueueing rows
_stop = threading.Event()  # set when the backend is shutting down
_worker = None

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
    _start_worker()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_shutdown)


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER,
                owner TEXT,
                email TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                queued_at REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS mail_queue_status ON mail_queue (status, id)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mail_campaigns)")}
        if "track" not in columns:
            conn.execute("ALTER TABLE mail_campaigns ADD COLUMN track INTEGER DEFAULT 1")
        if "owner" not in columns:
            conn.execute("ALTER TABLE mail_campaigns ADD COLUMN owner TEXT")
        if "audience" not in columns:
            # who the campaign goes to (role, tag or chosen people), saved when it is first sent
            conn.execute("ALTER TABLE mail_campaigns ADD COLUMN audience TEXT")
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
        # Dropbox links saved earlier had dl=1, which forces a download. Show the preview page instead.
        conn.execute("UPDATE mail_packs SET url = REPLACE(url, 'dl=1', 'dl=0') "
                     "WHERE url LIKE '%dropbox.com%' AND url LIKE '%dl=1%'")
        # Drive links saved earlier forced a download too. Switch them to the preview page.
        for row in conn.execute("SELECT id, url FROM mail_packs WHERE url LIKE 'https://drive.google.com/uc?export=download&id=%'").fetchall():
            file_id = row[1].split("id=", 1)[1].split("&", 1)[0]
            conn.execute("UPDATE mail_packs SET url = ? WHERE id = ?", (f"https://drive.google.com/file/d/{file_id}/view", row[0]))


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
        # Not every producer wants the whole pack downloaded, so the recipient chooses:
        # Dropbox shows its preview page unless the link forces a download (dl=1).
        query = [(k, "0" if k == "dl" and v == "1" else v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
        return urlunparse(parsed._replace(query=urlencode(query)))
    match = re.match(r"^/file/d/([\w-]+)", parsed.path)
    if host == "drive.google.com" and match:
        # Drive's preview page for the file, so the recipient chooses whether to download
        return f"https://drive.google.com/file/d/{match.group(1)}/view"
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


@bp.route("/api/campaigns/<int:campaign_id>", methods=["DELETE"])
@accounts.login_required
def delete_campaign(campaign_id):
    # Only drafts can be deleted. Sent campaigns keep their history.
    with _conn() as conn:
        row = conn.execute(
            "SELECT status FROM mail_campaigns WHERE id = ? AND owner = ?",
            (campaign_id, accounts.current_email()),
        ).fetchone()
        if not row:
            return jsonify({"error": "Campaign not found."}), 404
        if row["status"] not in (None, "draft"):
            return jsonify({"error": "Only drafts can be deleted."}), 409
        conn.execute("DELETE FROM mail_campaigns WHERE id = ? AND owner = ?", (campaign_id, accounts.current_email()))
    return jsonify({"ok": True})


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


def _recipients(conn, campaign_id, owner, role, tag, emails=None):
    """Everyone the campaign still owes: not sent yet (or failed), not unsubscribed, in the audience."""
    query = """
        SELECT email, name, instagram, roles FROM mail_contacts
        WHERE owner = ? AND unsubscribed_at IS NULL
          AND email NOT IN (SELECT email FROM mail_sends WHERE campaign_id = ? AND status IN ('sent', 'failed'))
    """
    args = [owner, campaign_id]
    if role:
        query += " AND roles LIKE ?"
        args.append(f"%{role}%")
    if tag:
        query += " AND tags LIKE ?"
        args.append(f"%{tag}%")
    if emails is not None:
        query += " AND email IN (" + ",".join("?" * len(emails)) + ")"
        args.extend(emails)
    query += " ORDER BY email"
    return [dict(r) for r in conn.execute(query, args)]


def _audience(campaign):
    """The saved audience of a campaign: a role, a tag, a chosen list, or nothing (everyone)."""
    try:
        return json.loads(campaign["audience"] or "{}")
    except (TypeError, ValueError):
        return {}


def _remaining(conn, campaign):
    """How many people the campaign still owes."""
    a = _audience(campaign)
    return len(_recipients(conn, campaign["id"], campaign["owner"], a.get("role", ""), a.get("tag", ""), a.get("emails")))


def _window_state(address, now=None):
    """(emails the account can still send in the last 24 hours, when its full daily allowance is back)."""
    now = now or time.time()
    if not address:
        return gmail.DAILY_LIMIT, now
    with _conn() as conn:
        times = [r[0] for r in conn.execute(
            "SELECT sent_at FROM mail_sends WHERE sender = ? AND status = 'sent' AND sent_at > ?",
            (address, now - 86400),
        )]
    free = max(0, gmail.DAILY_LIMIT - len(times))
    full_at = max(times) + 86400 if times else now
    return free, full_at


AVERAGE_SEND_SECONDS = sum(SEND_INTERVAL_SECONDS) / 2
OPENS_CHART_HOURS = 48  # the opens chart shows the first two days after the first send


def _done_estimate(address, remaining, now=None):
    """When the last of the remaining people gets the campaign, given the daily limit."""
    now = now or time.time()
    if remaining <= 0:
        return now
    free, full_at = _window_state(address, now)
    if remaining <= free:
        return now + remaining * AVERAGE_SEND_SECONDS
    rest = remaining - free
    days = -(-rest // gmail.DAILY_LIMIT)  # rounds up: how many more days of sending
    last_day = rest - (days - 1) * gmail.DAILY_LIMIT
    return full_at + (days - 1) * 86400 + last_day * AVERAGE_SEND_SECONDS


def _free_again_at(address, now=None):
    """When the account can send again: now if it has room, else when its oldest send in the last 24 hours expires."""
    now = now or time.time()
    if not address:
        return now
    with _conn() as conn:
        times = [r[0] for r in conn.execute(
            "SELECT sent_at FROM mail_sends WHERE sender = ? AND status = 'sent' AND sent_at > ?",
            (address, now - 86400),
        )]
    if len(times) < gmail.DAILY_LIMIT:
        return now
    return min(times) + 86400


def _busy_campaign(owner, address, exclude_id):
    """Another campaign that still owes people. A new campaign waits until it is done.
    Returns (campaign, done_at) or None."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mail_campaigns WHERE owner = ? AND id != ? AND status IN ('sending', 'partial')",
            (owner, exclude_id),
        ).fetchall()
        for row in rows:
            remaining = _remaining(conn, row)
            if remaining > 0:
                return dict(row), _done_estimate(address, remaining)
    return None


def _queue_batch(campaign_id, owner, audience, address):
    """Queues as many of the campaign's people as today's limit allows, and saves the audience.
    Returns (queued, remaining, free_before), or None if the campaign is already sending."""
    with _queue_lock:
        free, _ = _window_state(address)
        with _conn() as conn:
            campaign = conn.execute("SELECT * FROM mail_campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if campaign is None or campaign["status"] == "sending":
                return None
            people = _recipients(conn, campaign_id, owner, audience.get("role", ""), audience.get("tag", ""), audience.get("emails"))
            batch = people[:free]
            conn.execute("UPDATE mail_campaigns SET audience = ? WHERE id = ?", (json.dumps(audience), campaign_id))
            if not batch:
                return 0, len(people), free
            conn.execute("DELETE FROM mail_queue WHERE campaign_id = ? AND status = 'pending'", (campaign_id,))
            now = time.time()
            conn.executemany(
                "INSERT INTO mail_queue (campaign_id, owner, email, status, queued_at) VALUES (?, ?, ?, 'pending', ?)",
                [(campaign_id, owner, r["email"], now) for r in batch],
            )
            conn.execute("UPDATE mail_campaigns SET status = 'sending' WHERE id = ?", (campaign_id,))
            return len(batch), len(people) - len(batch), free


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


def fill_variables(text, contact, pack_url):
    """Replaces {{variables}} with this person's details. Unknown variables are left as typed."""
    name = (contact.get("name") or "").strip()
    values = {
        "{{name}}": name or "there",
        "{{first_name}}": name.split()[0] if name else "there",
        "{{instagram}}": (contact.get("instagram") or "").strip(),
        "{{roles}}": ", ".join(r.strip() for r in (contact.get("roles") or "").split(",") if r.strip()),
        "{{email}}": contact.get("email") or "",
        "{{pack_link}}": pack_url or "",
    }
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def _render(campaign, contact, pack_url):
    """Returns (text, html) for one recipient."""
    campaign_id, email = campaign["id"], contact["email"]
    unsubscribe = unsubscribe_link(email, campaign["owner"])
    text = fill_variables(campaign["body"], contact, pack_url)
    if pack_url and "{{pack_link}}" not in campaign["body"]:
        text += "\n\nDownload the pack: " + pack_url
    text += f"\n\n---\nNot interested? Unsubscribe: {unsubscribe}"

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


@bp.route("/api/mail/check", methods=["POST"])
@accounts.signed_in
def check_mail_connection():
    """Logs in to Gmail with the saved app password, without sending anything."""
    account = _load_account(accounts.current_email())
    if not account:
        return jsonify({"ok": False, "error": "No Gmail account is connected yet."}), 409
    address, encrypted = account
    try:
        gmail.check_login(address, gmail.decrypt_password(encrypted))
    except gmail.CredentialError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except smtplib.SMTPAuthenticationError:
        return jsonify({"ok": False, "error": "Google rejected the saved app password. Connect the account again."}), 401
    except (smtplib.SMTPException, OSError):
        return jsonify({"ok": False, "error": "Could not reach Gmail. Try again in a moment."}), 502
    return jsonify({"ok": True, "address": address})


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
            "SELECT * FROM mail_campaigns WHERE id = ? AND owner = ?",
            (campaign_id, accounts.current_email()),
        ).fetchone()
        if not campaign:
            return jsonify({"error": "Campaign not found."}), 404
        sent = conn.execute(
            "SELECT COUNT(*) FROM mail_sends WHERE campaign_id = ? AND status = 'sent'", (campaign_id,)
        ).fetchone()[0]
        # A finished campaign owes nobody; a running or partly sent one owes the people still left
        remaining = 0 if campaign["status"] == "sent" else _remaining(conn, campaign)
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
        failed = conn.execute(
            "SELECT COUNT(*) FROM mail_sends WHERE campaign_id = ? AND status = 'failed'", (campaign_id,)
        ).fetchone()[0]
        unsubscribed = conn.execute(
            "SELECT COUNT(*) FROM mail_sends s JOIN mail_contacts m ON m.owner = ? AND m.email = s.email "
            "WHERE s.campaign_id = ? AND s.status = 'sent' AND m.unsubscribed_at IS NOT NULL",
            (accounts.current_email(), campaign_id),
        ).fetchone()[0]
        first_sent = conn.execute(
            "SELECT MIN(sent_at) FROM mail_sends WHERE campaign_id = ? AND status = 'sent'", (campaign_id,)
        ).fetchone()[0]
        open_times = [r[0] for r in conn.execute(
            "SELECT created_at FROM mail_events WHERE campaign_id = ? AND kind = 'open'", (campaign_id,)
        )]
        # for each person: when they were sent to, and when they first opened
        first_opens = conn.execute(
            "SELECT s.sent_at, MIN(e.created_at) FROM mail_sends s "
            "JOIN mail_events e ON e.campaign_id = s.campaign_id AND e.email = s.email AND e.kind = 'open' "
            "WHERE s.campaign_id = ? AND s.status = 'sent' GROUP BY s.email",
            (campaign_id,),
        ).fetchall()

    by_hour = [0] * OPENS_CHART_HOURS
    later = 0
    if first_sent:
        for t in open_times:
            hour = max(0, int((t - first_sent) // 3600))
            if hour < OPENS_CHART_HOURS:
                by_hour[hour] += 1
            else:
                later += 1
    gaps = [max(0, first - sent) / 3600 for sent, first in first_opens]
    median_open_hours = round(statistics.median(gaps), 1) if gaps else None
    account = _load_account(accounts.current_email())
    address = account[0] if account else None
    done_at = _done_estimate(address, remaining) if remaining > 0 else None
    can_send_at = _free_again_at(address) if remaining > 0 else None
    return jsonify({
        "can_send_at": can_send_at,  # when more emails can go out under the daily limit
        "status": campaign["status"],
        "tracking": bool(campaign["track"]),
        "sent": sent,
        "total": sent + remaining,
        "remaining": remaining,
        "done_at": done_at,  # when the last person gets it; the next campaign can start then too
        "sent_at": campaign["sent_at"],
        "opened": opens["people"],
        "open_events": opens["events"],
        "clicked": clicks["people"],
        "click_events": clicks["events"],
        "links": links,
        "failed": failed,
        "unsubscribed": unsubscribed,
        "median_open_hours": median_open_hours,
        "opens_by_hour": by_hour,  # opens in each hour after the first send, for the chart
        "opens_later": later,
    })


@bp.route("/api/campaigns/<int:campaign_id>/people", methods=["GET"])
@accounts.login_required
def campaign_people(campaign_id):
    owner = accounts.current_email()
    with _conn() as conn:
        if not conn.execute("SELECT 1 FROM mail_campaigns WHERE id = ? AND owner = ?", (campaign_id, owner)).fetchone():
            return jsonify({"error": "Campaign not found."}), 404
        people = [dict(r) for r in conn.execute(
            "SELECT s.email, s.status, s.error, s.sent_at, COALESCE(NULLIF(m.name, ''), s.email) AS name, "
            "m.unsubscribed_at IS NOT NULL AS unsubscribed, "
            "(SELECT COUNT(*) FROM mail_events e WHERE e.campaign_id = s.campaign_id AND e.email = s.email AND e.kind = 'open') AS opens, "
            "(SELECT MIN(e.created_at) FROM mail_events e WHERE e.campaign_id = s.campaign_id AND e.email = s.email AND e.kind = 'open') AS first_open, "
            "(SELECT COUNT(*) FROM mail_events e WHERE e.campaign_id = s.campaign_id AND e.email = s.email AND e.kind = 'click') AS clicks "
            "FROM mail_sends s LEFT JOIN mail_contacts m ON m.owner = ? AND m.email = s.email "
            "WHERE s.campaign_id = ? ORDER BY clicks DESC, opens DESC, name",
            (owner, campaign_id),
        )]
        link_rows = conn.execute(
            "SELECT email, url, COUNT(*) AS clicks FROM mail_events WHERE campaign_id = ? AND kind = 'click' "
            "GROUP BY email, url ORDER BY clicks DESC",
            (campaign_id,),
        ).fetchall()
    links = {}
    for row in link_rows:
        links.setdefault(row["email"], []).append({"url": row["url"], "clicks": row["clicks"]})
    for person in people:
        person["unsubscribed"] = bool(person["unsubscribed"])
        person["links"] = links.get(person["email"], [])
    return jsonify({"people": people})


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


def _set_queue_row(row_id, status, error=None):
    with _conn() as conn:
        conn.execute("UPDATE mail_queue SET status = ?, error = ? WHERE id = ?", (status, error, row_id))


def _stop_campaign(campaign_id, status):
    """Stops a campaign. Its people stay queued, and sending again picks them up."""
    with _conn() as conn:
        conn.execute("UPDATE mail_campaigns SET status = ? WHERE id = ?", (status, campaign_id))
    logger.warning(f"Campaign {campaign_id} stopped: {status}")


def _finish_if_done(campaign_id):
    """Marks a campaign sent (or partial, if people are still waiting) once its queue is empty."""
    with _conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM mail_queue WHERE campaign_id = ? AND status IN ('pending', 'sending')",
            (campaign_id,),
        ).fetchone()[0]
        if left:
            return
        campaign = conn.execute("SELECT * FROM mail_campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not campaign or campaign["status"] != "sending":
            return
        if _remaining(conn, campaign) > 0:
            conn.execute("UPDATE mail_campaigns SET status = 'partial' WHERE id = ?", (campaign_id,))
        else:
            conn.execute("UPDATE mail_campaigns SET status = 'sent', sent_at = ? WHERE id = ?", (time.time(), campaign_id))


def _reconcile():
    """Finishes campaigns that are still marked sending but have nothing left in the queue."""
    with _conn() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT c.id FROM mail_campaigns c WHERE c.status = 'sending' AND NOT EXISTS "
            "(SELECT 1 FROM mail_queue q WHERE q.campaign_id = c.id AND q.status IN ('pending', 'sending'))"
        )]
    for campaign_id in ids:
        _finish_if_done(campaign_id)


def _next_queued():
    """Takes the next waiting email of a campaign that is sending. Returns None when there is none."""
    with _queue_lock:
        with _conn() as conn:
            row = conn.execute(
                "SELECT q.id, q.campaign_id, q.owner, q.email FROM mail_queue q "
                "JOIN mail_campaigns c ON c.id = q.campaign_id "
                "WHERE q.status = 'pending' AND c.status = 'sending' ORDER BY q.id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute("UPDATE mail_queue SET status = 'sending' WHERE id = ?", (row["id"],))
    return dict(row)


def _send_queued(item, senders):
    """Sends one queued email. senders keeps one open Gmail connection per address."""
    campaign_id, owner, email = item["campaign_id"], item["owner"], item["email"]
    with _conn() as conn:
        campaign = conn.execute("SELECT * FROM mail_campaigns WHERE id = ?", (campaign_id,)).fetchone()
        contact = conn.execute(
            "SELECT email, name, instagram, roles, unsubscribed_at FROM mail_contacts WHERE owner = ? AND email = ?",
            (owner, email),
        ).fetchone()
        pack = conn.execute("SELECT url FROM mail_packs WHERE id = ? AND owner = ?", (campaign["pack_id"], owner)).fetchone() \
            if campaign and campaign["pack_id"] else None
    if not campaign or campaign["status"] != "sending":
        _set_queue_row(item["id"], "pending")  # campaign was stopped meanwhile; keep the person queued
        return
    if not contact or contact["unsubscribed_at"]:
        _set_queue_row(item["id"], "skipped", "Unsubscribed or removed")
        return
    account = _load_account(owner)
    if not account:
        _set_queue_row(item["id"], "pending")
        _stop_campaign(campaign_id, "stopped: Connect your Gmail account first.")
        return
    address, encrypted = account
    if _sent_today(address) >= gmail.DAILY_LIMIT:
        # The daily limit is full. The person is not queued any more; the campaign picks them up again when it has room.
        with _conn() as conn:
            conn.execute("DELETE FROM mail_queue WHERE id = ?", (item["id"],))
        _stop_campaign(campaign_id, "partial")
        return
    try:
        sender = senders.get(address)
        if sender is None:
            sender = gmail.Sender(address, gmail.decrypt_password(encrypted))
            sender.__enter__()
            senders[address] = sender
        pack_url = pack["url"] if pack else None
        text, html_body = _render(dict(campaign), dict(contact), pack_url)
        sender.send(email, fill_variables(campaign["subject"], dict(contact), pack_url), text, html_body,
                    unsubscribe_link(email, owner))
        _log_send(campaign_id, email, address, "sent", None)
        _set_queue_row(item["id"], "sent")
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
        logger.warning(f"Send to {email} refused: {e}")
        _log_send(campaign_id, email, address, "failed", str(e)[:300])
        _set_queue_row(item["id"], "failed", str(e)[:300])
    except smtplib.SMTPAuthenticationError:
        _drop_sender(senders, address)
        _set_queue_row(item["id"], "pending")
        _stop_campaign(campaign_id, "stopped: Google rejected the login")
    except gmail.CredentialError as e:
        _drop_sender(senders, address)
        _set_queue_row(item["id"], "pending")
        _stop_campaign(campaign_id, f"stopped: {str(e)[:120]}")
    except (smtplib.SMTPException, OSError) as e:
        _drop_sender(senders, address)
        _set_queue_row(item["id"], "pending")
        _stop_campaign(campaign_id, f"stopped: {str(e)[:120]}")


def _drop_sender(senders, address):
    sender = senders.pop(address, None)
    if sender is not None:
        try:
            sender.__exit__(None, None, None)
        except Exception:
            pass


def _close_senders(senders):
    for address in list(senders):
        _drop_sender(senders, address)


def _resume_partial():
    """Queues the rest of each partly sent campaign once today's limit has room again."""
    with _conn() as conn:
        partial = [dict(r) for r in conn.execute("SELECT * FROM mail_campaigns WHERE status = 'partial'")]
    for campaign in partial:
        account = _load_account(campaign["owner"])
        if not account:
            continue
        address = account[0]
        if _window_state(address)[0] <= 0:
            continue
        result = _queue_batch(campaign["id"], campaign["owner"], _audience(campaign), address)
        if result is not None and result[0] == 0 and result[1] == 0:
            # nobody is left to send to any more (for example, everyone unsubscribed)
            with _conn() as conn:
                conn.execute(
                    "UPDATE mail_campaigns SET status = 'sent', sent_at = ? WHERE id = ? AND status = 'partial'",
                    (time.time(), campaign["id"]),
                )


def _worker_loop():
    """The one background thread that sends queued email, one message at a time.
    An unexpected error is logged and the loop carries on, so the sender keeps running."""
    senders = {}
    try:
        while not _stop.is_set():
            try:
                item = _next_queued()
                if item is None:
                    _close_senders(senders)  # idle: do not hold Gmail connections open
                    _reconcile()
                    _resume_partial()
                    _stop.wait(2)
                    continue
                _send_queued(item, senders)
                _finish_if_done(item["campaign_id"])
                # A random pause between emails, like a person sending by hand
                _stop.wait(random.uniform(*SEND_INTERVAL_SECONDS))
            except Exception:
                logger.exception("Campaign sender hit an error; carrying on")
                _stop.wait(5)
    finally:
        _close_senders(senders)


def _start_worker():
    """Starts the sender. Anything that was mid-send when the backend last stopped goes back to waiting."""
    global _worker
    with _conn() as conn:
        conn.execute("UPDATE mail_queue SET status = 'pending' WHERE status = 'sending'")
    _reconcile()
    _worker = threading.Thread(target=_worker_loop, name="campaign-sender", daemon=True)
    _worker.start()


def _on_shutdown(signum, frame):
    """On a redeploy the server gets SIGTERM. Let the current email finish, then exit."""
    _stop.set()
    if _worker is not None:
        _worker.join(timeout=8)
    raise SystemExit(0)


@bp.route("/api/campaigns/<int:campaign_id>/send", methods=["POST"])
@accounts.login_required
def send_campaign(campaign_id):
    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "").strip()
    tag = (data.get("tag") or "").strip()
    owner = accounts.current_email()
    emails = None
    if isinstance(data.get("emails"), list):
        emails = sorted({str(e).strip().lower() for e in data["emails"] if str(e).strip()})
        if not emails:
            return jsonify({"error": "Choose at least one person to send to."}), 400
        if len(emails) > 900:
            return jsonify({"error": "Choose up to 900 people at a time."}), 400

    with _conn() as conn:
        campaign = conn.execute(
            "SELECT * FROM mail_campaigns WHERE id = ? AND owner = ?", (campaign_id, owner)
        ).fetchone()
        if not campaign:
            return jsonify({"error": "Campaign not found."}), 404
        recipients = _recipients(conn, campaign_id, owner, role, tag, emails)
        pack = conn.execute("SELECT url FROM mail_packs WHERE id = ? AND owner = ?", (campaign["pack_id"], owner)).fetchone() \
            if campaign["pack_id"] else None
    pack_url = pack["url"] if pack else None
    account = _load_account(owner)

    free_today, _ = _window_state(account[0] if account else None)
    if not data.get("confirm"):
        return jsonify({
            "dry_run": True,
            "can_send": bool(account),
            "would_send": len(recipients),
            "today": min(free_today, len(recipients)),
            "sample": [r["email"] for r in recipients[:5]],
            "message": "Nothing was sent. Repeat the request with confirm: true to send.",
        })

    if not account:
        return jsonify({"error": "Connect your Gmail account first."}), 409
    address, encrypted = account
    try:
        gmail.decrypt_password(encrypted)  # fail early if the saved password cannot be read
    except gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 409

    busy = _busy_campaign(owner, address, campaign_id)
    if busy:
        busy_campaign, _ = busy
        name = busy_campaign["name"] or busy_campaign["subject"]
        return jsonify({
            "error": f"“{name}” is still sending. The next campaign can be started when it is done.",
        }), 409

    if not recipients:
        return jsonify({"error": "Nobody left to send to in this campaign."}), 409
    audience = {"role": role, "tag": tag, "emails": emails}
    result = _queue_batch(campaign_id, owner, audience, address)
    if result is None:
        return jsonify({"error": "This campaign is already sending."}), 409
    queued, remaining, free = result
    if queued == 0:
        return jsonify({
            "error": f"You reached the daily limit of {gmail.DAILY_LIMIT} emails. "
                     "The campaign continues automatically when the limit has room again.",
        }), 409
    done_at = _done_estimate(address, remaining)
    return jsonify({
        "started": True,
        "queued": queued,
        "waiting": remaining,
        "left_today": free - queued,
        "done_at": done_at,
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
            sender.send(address, "[Test] " + fill_variables(campaign["subject"], me, pack["url"] if pack else None),
                        text, html_body, unsubscribe_link(address, owner))
    except gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 409
    except (smtplib.SMTPException, OSError) as e:
        return jsonify({"error": f"Gmail did not accept the test: {str(e)[:150]}"}), 502
    return jsonify({"ok": True, "sent_to": address})
