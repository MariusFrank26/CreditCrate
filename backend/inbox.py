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
import logging
import quopri
import re
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
MAX_MESSAGES = 25
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


def _message_list(server):
    uids = server.uid("SEARCH", "X-GM-RAW", f'"{SEARCH}"')[1][0].split()
    newest = [int(u) for u in uids][-MAX_MESSAGES:][::-1]
    messages = []
    for uid in newest:
        _, data = server.uid("FETCH", str(uid), "(BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if not data or not data[0]:
            continue
        head = data[0][1] if isinstance(data[0], tuple) else b""
        headers = message_from_bytes(head)
        structure = _structure_of(data[0][0]) if isinstance(data[0], tuple) else None
        attachments = _attachments(structure) if structure else []
        links = []
        if not attachments:
            _, text_data = server.uid("FETCH", str(uid), "(BODY.PEEK[TEXT]<0.40000>)")
            body = text_data[0][1].decode("utf-8", "replace") if text_data and isinstance(text_data[0], tuple) else ""
            links = sorted(set(LINK_RE.findall(body)))
        if not attachments and not links:
            continue
        messages.append({
            "uid": uid,
            "from": _decode(headers.get("From", "")),
            "subject": _decode(headers.get("Subject", "")) or "(no subject)",
            "date": headers.get("Date", ""),
            "attachments": attachments,
            "links": links,
        })
    return messages


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
    try:
        address, password = _connected_account()
    except gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 409
    if not address:
        return jsonify({"error": "Connect your Gmail account in Settings first."}), 409
    try:
        server = _connect(address, password)
        try:
            messages = _message_list(server)
        finally:
            server.logout()
    except imaplib.IMAP4.error as e:
        logger.warning(f"IMAP rejected {address}: {e}")
        return jsonify({"error": "Gmail rejected the login. Check IMAP access and the app password."}), 401
    except OSError:
        return jsonify({"error": "Could not reach Gmail. Try again in a moment."}), 502
    return jsonify({"messages": messages, "address": address})


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

    if match["encoding"] == "base64":
        body = base64.b64decode(raw)
    elif match["encoding"] == "quoted-printable":
        body = quopri.decodestring(raw)
    else:
        body = raw
    # Keep only a safe ASCII file name for the header
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", match["name"]) or "sample"
    return Response(body, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"',
                             "Cache-Control": "no-store"})


def register(app):
    app.register_blueprint(bp)
