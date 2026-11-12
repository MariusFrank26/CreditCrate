"""
Sample inbox: a live view of the signed-in user's Gmail, read over IMAP with the same
app password used for sending.

Nothing is stored on the server. The list is built from message headers, attachment
names and sharing links. When someone downloads an attachment, the server fetches it
from Gmail and passes it straight to the browser. Dropbox and Drive links are only
shown, never fetched by the server.
"""

import base64
import imaplib
import io
import logging
import quopri
import re
import zipfile
from email import message_from_bytes
from email.header import decode_header, make_header

from flask import Blueprint, Response, jsonify

import accounts
import gmail
import mailing

logger = logging.getLogger(__name__)

bp = Blueprint("inbox", __name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
DAYS = 7  # how far back the sample scan looks
BATCH = 50  # messages per IMAP request, so a scan of hundreds of emails takes a few round trips
MAX_ZIP_BYTES = 300 * 1024 * 1024  # a whole message's samples are zipped in memory, up to this size
UID_RE = re.compile(r"UID (\d+)")
SAMPLE_EXTENSIONS = (".wav", ".mp3", ".aif", ".aiff", ".flac", ".zip", ".rar", ".7z")
SEARCH = ('has:attachment (filename:wav OR filename:mp3 OR filename:aif OR filename:aiff '
          'OR filename:flac OR filename:zip OR filename:rar OR filename:7z) '
          'OR (dropbox.com OR drive.google.com OR docs.google.com)')
LINK_RE = re.compile(r"https://(?:www\.dropbox\.com|drive\.google\.com|docs\.google\.com)/[^\s\"'<>]+")
PART_RE = re.compile(r"^\d+(\.\d+)*$")


def _connect(address, app_password):
    server = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
    server.login(address, app_password)
    server.select("INBOX", readonly=True)
    return server


def _parse_sexp(text, i=0):
    """Parses one IMAP structure such as BODYSTRUCTURE into nested lists."""
    while text[i] == " ":
        i += 1
    if text[i] == "(":
        items = []
        i += 1
        while True:
            while text[i] == " ":
                i += 1
            if text[i] == ")":
                return items, i + 1
            value, i = _parse_sexp(text, i)
            items.append(value)
    if text[i] == '"':
        out = []
        i += 1
        while text[i] != '"':
            if text[i] == "\\":
                i += 1
            out.append(text[i])
            i += 1
        return "".join(out), i + 1
    j = i
    while j < len(text) and text[j] not in " ()":
        j += 1
    token = text[i:j]
    return (None if token == "NIL" else token), j


def _structure_of(fetch_line):
    """The BODYSTRUCTURE from the first line of an IMAP FETCH response, or None."""
    text = fetch_line.decode("latin-1") if isinstance(fetch_line, (bytes, bytearray)) else str(fetch_line)
    start = text.find("BODYSTRUCTURE ")
    if start < 0:
        return None
    value, _ = _parse_sexp(text, start + len("BODYSTRUCTURE "))
    return value


def _params(raw):
    if not isinstance(raw, list):
        return {}
    return {str(raw[k]).lower(): raw[k + 1] for k in range(0, len(raw) - 1, 2) if raw[k] is not None}


def _attachments(structure, prefix=""):
    """Walks a BODYSTRUCTURE and returns the sample-like attachments with their part numbers."""
    found = []
    if not isinstance(structure, list) or not structure:
        return found
    if isinstance(structure[0], list):  # multipart: every item is a sub-part
        index = 0
        for sub in structure:
            if isinstance(sub, list):
                index += 1
                number = f"{prefix}.{index}" if prefix else str(index)
                found += _attachments(sub, number)
        return found
    if len(structure) < 7:
        return found
    mtype = (structure[0] or "").lower()
    params = _params(structure[2])
    disposition_index = 9 if mtype == "text" else 8
    disposition = structure[disposition_index] if len(structure) > disposition_index else None
    disposition_params = {}
    if isinstance(disposition, list) and len(disposition) > 1:
        disposition_params = _params(disposition[1])
    name = disposition_params.get("filename") or params.get("name") or ""
    if name and name.lower().endswith(SAMPLE_EXTENSIONS):
        found.append({
            "part": prefix or "1",
            "name": name,
            "size": int(structure[6]) if str(structure[6]).isdigit() else 0,
            "encoding": (structure[5] or "7bit").lower(),
        })
    return found


def _fetch_items(server, uids, what):
    """Yields (uid, metadata line, literal bytes) for each message in one FETCH over several uids."""
    _, data = server.uid("FETCH", ",".join(str(uid) for uid in uids), what)
    for item in data or []:
        if isinstance(item, tuple):
            meta = item[0].decode("latin-1")
            match = UID_RE.search(meta)
            if match:
                yield int(match.group(1)), meta, item[1]


def _links_in(raw):
    """Dropbox and Drive links in an email's text. Decodes quoted-printable and base64 first,
    so links split by line breaks or written as &amp; come out whole."""
    text = ""
    try:
        for part in message_from_bytes(raw).walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True) or b""
                text += payload.decode(part.get_content_charset() or "utf-8", "replace") + "\n"
    except Exception:
        text = raw.decode("utf-8", "replace")
    found = []  # in the order they appear in the email
    for match in LINK_RE.findall(text.replace("&amp;", "&")):
        link = match.rstrip(".,;:!?)")
        if link not in found:
            found.append(link)
    return found


def _recent_sample_uids(server):
    _, data = server.uid("SEARCH", "X-GM-RAW", f'"({SEARCH}) newer_than:{DAYS}d"')
    return [int(uid) for uid in data[0].split()]


BOUNCE_QUERY = '"(from:mailer-daemon OR from:postmaster) newer_than:2d"'
EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def bounced_addresses(address, password, sent_to):
    """The people we sent to (sent_to, lowercase emails) who appear in a recent delivery-failure email."""
    server = _connect(address, password)
    try:
        _, data = server.uid("SEARCH", "X-GM-RAW", BOUNCE_QUERY)
        uids = data[0].split() if data and data[0] else []
        found = set()
        for uid in uids[-200:]:  # shortcut: at most 200 failure emails per check, raise if a busy account needs more
            _, fetched = server.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = fetched[0][1] if fetched and isinstance(fetched[0], tuple) else b""
            for part in message_from_bytes(raw).walk():
                if part.is_multipart():
                    continue
                payload = part.get_payload(decode=True) or b""
                found |= set(EMAIL_IN_TEXT.findall(payload.decode("utf-8", "replace").lower())) & sent_to
        return found
    finally:
        server.logout()


def list_messages(address, password):
    """Every email from the last DAYS days with sample attachments or sharing links, newest first."""
    server = _connect(address, password)
    try:
        uids = _recent_sample_uids(server)
        messages = []
        for start in range(0, len(uids), BATCH):
            chunk = uids[start:start + BATCH]
            heads = {}
            for uid, meta, literal in _fetch_items(server, chunk,
                                                   "(UID BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"):
                structure = _structure_of(meta)
                heads[uid] = (literal, _attachments(structure) if structure else [])
            # Links are looked for in every email, including ones that also have files
            links = {}
            present = [uid for uid in chunk if uid in heads]
            if present:
                for uid, _, literal in _fetch_items(server, present, "(UID BODY.PEEK[]<0.150000>)"):
                    # Only the first link is the new pack. Later links are usually a parent folder or older weeks.
                    links[uid] = _links_in(literal)[:1]
            for uid in chunk:
                if uid not in heads:
                    continue
                literal, attachments = heads[uid]
                found_links = links.get(uid, [])
                if not attachments and not found_links:
                    continue
                headers = message_from_bytes(literal)
                messages.append({
                    "uid": uid,
                    "from": _decode(headers.get("From", "")),
                    "subject": _decode(headers.get("Subject", "")) or "(no subject)",
                    "date": headers.get("Date", ""),
                    "attachments": attachments,
                    "links": found_links,
                })
        messages.sort(key=lambda m: m["uid"], reverse=True)
        return messages
    finally:
        server.logout()


def _decode(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _connected_account():
    account = mailing._load_account(accounts.current_email())
    if not account:
        return None, None
    address, encrypted = account
    return address, gmail.decrypt_password(encrypted)


@bp.route("/api/inbox", methods=["GET"])
@accounts.login_required
def list_inbox():
    """Scans synchronously. The site uses /api/inbox/start instead, which runs in the background."""
    try:
        address, password = _connected_account()
    except gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 409
    if not address:
        return jsonify({"error": "Connect your Gmail account in Settings first."}), 409
    try:
        return jsonify({"messages": list_messages(address, password), "address": address})
    except imaplib.IMAP4.error as e:
        logger.warning(f"IMAP rejected {address}: {e}")
        return jsonify({"error": "Gmail rejected the login. Check IMAP access and the app password."}), 401
    except OSError:
        return jsonify({"error": "Could not reach Gmail. Try again in a moment."}), 502


@bp.route("/api/inbox/<int:uid>/attachments/<part>", methods=["GET"])
@accounts.login_required
def download_attachment(uid, part):
    if not PART_RE.match(part):
        return jsonify({"error": "Unknown attachment."}), 400
    try:
        address, password = _connected_account()
    except gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 409
    if not address:
        return jsonify({"error": "Connect your Gmail account in Settings first."}), 409
    try:
        server = _connect(address, password)
        try:
            _, data = server.uid("FETCH", str(uid), f"(BODYSTRUCTURE BODY.PEEK[{part}])")
            structure = _structure_of(data[0][0] if data and isinstance(data[0], tuple) else b"")
            # the file is the part named in the structure; its encoding decides how to decode it
            match = next((a for a in _attachments(structure) if a["part"] == part), None) if structure else None
            if not match:
                return jsonify({"error": "That attachment is no longer in this message."}), 404
            raw = data[0][1] if data and isinstance(data[0], tuple) else b""
        finally:
            server.logout()
    except imaplib.IMAP4.error:
        return jsonify({"error": "Gmail rejected the login."}), 401
    except OSError:
        return jsonify({"error": "Could not reach Gmail. Try again in a moment."}), 502

    body = _decode_bytes(raw, match["encoding"])
    # Keep only a safe ASCII file name for the header
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", match["name"]) or "sample"
    return Response(body, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"',
                             "Cache-Control": "no-store"})


def _decode_bytes(raw, encoding):
    if encoding == "base64":
        return base64.b64decode(raw)
    if encoding == "quoted-printable":
        return quopri.decodestring(raw)
    return raw


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or "sample"


@bp.route("/api/inbox/<int:uid>/zip", methods=["GET"])
@accounts.login_required
def download_pack(uid):
    """All sample files of one message as a single zip. Built in memory and not stored."""
    try:
        address, password = _connected_account()
    except gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 409
    if not address:
        return jsonify({"error": "Connect your Gmail account in Settings first."}), 409
    try:
        server = _connect(address, password)
        try:
            _, data = server.uid("FETCH", str(uid), "(BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            text = b" ".join(item[0] if isinstance(item, tuple) else item for item in data or [] if item != b")")
            structure = _structure_of(text)
            subject_raw = next((item[1] for item in data or [] if isinstance(item, tuple)), b"")
            files = _attachments(structure) if structure else []
            if not files:
                return jsonify({"error": "This message has no sample files."}), 404
            if sum(f["size"] for f in files) > MAX_ZIP_BYTES:
                return jsonify({"error": "These files are too big for one zip. Download them one by one."}), 413
            buffer = io.BytesIO()
            used = set()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
                for f in files:
                    _, part_data = server.uid("FETCH", str(uid), f"(BODY.PEEK[{f['part']}])")
                    raw = next((item[1] for item in part_data or [] if isinstance(item, tuple)), b"")
                    name = _safe_name(f["name"])
                    base, dot, ext = name.rpartition(".")
                    candidate, index = name, 2
                    while candidate in used:
                        candidate = f"{base}-{index}.{ext}" if dot else f"{name}-{index}"
                        index += 1
                    used.add(candidate)
                    archive.writestr(candidate, _decode_bytes(raw, f["encoding"]))
        finally:
            server.logout()
    except imaplib.IMAP4.error:
        return jsonify({"error": "Gmail rejected the login."}), 401
    except OSError:
        return jsonify({"error": "Could not reach Gmail. Try again in a moment."}), 502

    subject = _decode(message_from_bytes(subject_raw).get("Subject", "")) or f"samples-{uid}"
    return Response(buffer.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{_safe_name(subject)}.zip"',
                             "Cache-Control": "no-store"})


def register(app):
    app.register_blueprint(bp)
