"""
Sign-up with email verification, verification resends, and password reset.

Platform mail (verification and reset links) goes out through Resend, using
RESEND_API_KEY and MAIL_FROM. It is separate from the Gmail sending that users do
for their campaigns. Without those two variables, sign-up and reset are refused
with a clear message instead of creating accounts nobody can verify.

Links are signed and expire: verification after 48 hours, reset after one hour,
and a reset link stops working as soon as the password has changed.
"""

import logging
import os

import requests
from flask import Blueprint, jsonify, redirect, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import accounts

logger = logging.getLogger(__name__)

bp = Blueprint("auth_flow", __name__)

VERIFY_SALT = "creditcrate-verify"
RESET_SALT = "creditcrate-reset"
VERIFY_SECONDS = 48 * 3600
RESET_SECONDS = 3600


class MailNotConfigured(Exception):
    pass


def _site():
    return os.getenv("PUBLIC_SITE_URL", "https://creditcrate.app").rstrip("/")


def _api():
    return os.getenv("PUBLIC_API_URL", "https://api.creditcrate.app/api").rstrip("/")


def _signer(salt):
    return URLSafeTimedSerializer(accounts.AUTH_SECRET, salt=salt)


def send_platform_mail(to, subject, text):
    key = os.getenv("RESEND_API_KEY", "").strip()
    sender = os.getenv("MAIL_FROM", "").strip()
    if not key or not sender:
        raise MailNotConfigured()
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}"},
        json={"from": sender, "to": [to], "subject": subject, "text": text},
        timeout=15,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend {resp.status_code}: {resp.text[:200]}")


def _account_exists(email):
    with accounts.db() as conn:
        return conn.execute("SELECT password_hash FROM users WHERE email = ?", (email,)).fetchone()


def _send_verification(email):
    token = _signer(VERIFY_SALT).dumps({"e": email})
    link = f"{_api()}/verify?token={token}"
    send_platform_mail(
        email,
        "Verify your CreditCrate account",
        "Welcome to CreditCrate. Open this link to verify your email and start your 7-day free trial:\n\n"
        f"{link}\n\nThe link works for 48 hours. If you didn't sign up, you can ignore this email.",
    )


def _send_reset(email, password_hash):
    token = _signer(RESET_SALT).dumps({"e": email, "h": password_hash[-16:]})
    link = f"{_site()}/reset.html?token={token}"
    send_platform_mail(
        email,
        "Reset your CreditCrate password",
        "Someone asked to reset the password for this CreditCrate account.\n\n"
        "If you did this, click the link to choose a new password. "
        "If you didn't do this, your password might have been leaked. Click the link to change it right away.\n\n"
        f"{link}\n\nThe link works for one hour and stops working once the password has changed.",
    )


@bp.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(silent=True) or {}
    email = accounts.normalize_email(data.get("email"))
    password = data.get("password") or ""
    if not accounts.is_valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < accounts.MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"Use at least {accounts.MIN_PASSWORD_LENGTH} characters for your password."}), 400
    if data.get("accept_terms") is not True:
        return jsonify({"error": "Please accept the privacy policy and terms to continue."}), 400

    ip = accounts.client_ip()
    if accounts.limited(f"signup-ip:{ip}", 5, 3600):
        return jsonify({"error": "Too many sign-ups from this connection. Try again later."}), 429

    if _account_exists(email):
        if accounts.is_verified(email):
            return jsonify({"error": "You already have an account. Sign in instead."}), 409
        # Not verified yet: same answer as a new sign-up, and the sign-in page can send a new link
        return jsonify({"sent": True})

    if not accounts.create_user(email, password, replace=False):
        # Another request created this account a moment ago, so it must not change the password
        if accounts.is_verified(email):
            return jsonify({"error": "You already have an account. Sign in instead."}), 409
        return jsonify({"sent": True})
    try:
        _send_verification(email)
    except MailNotConfigured:
        _delete_account(email)
        return jsonify({"error": "Sign-up is not set up on the server yet. Try again later."}), 503
    except Exception as e:
        logger.error(f"Verification mail failed: {e}")
        _delete_account(email)
        return jsonify({"error": "We could not send the verification email. Try again in a moment."}), 502
    return jsonify({"sent": True})


def _delete_account(email):
    with accounts.db() as conn:
        conn.execute("DELETE FROM users WHERE email = ? AND email_verified_at IS NULL", (email,))


@bp.route("/api/verify", methods=["GET"])
def verify():
    try:
        data = _signer(VERIFY_SALT).loads(request.args.get("token", ""), max_age=VERIFY_SECONDS)
    except (BadSignature, SignatureExpired):
        return "This verification link has expired or is not valid. Log in to ask for a new one.", 400
    accounts.mark_verified(data["e"])
    return redirect(f"{_site()}/index.html?verified=1", code=302)


@bp.route("/api/verify/resend", methods=["POST"])
def resend_verification():
    email = accounts.normalize_email((request.get_json(silent=True) or {}).get("email"))
    if accounts.is_valid_email(email) and not accounts.limited(f"verify:{email}", 3, 3600):
        row = _account_exists(email)
        if row and not accounts.is_verified(email):
            try:
                _send_verification(email)
            except Exception as e:
                logger.error(f"Verification resend failed: {e}")
    return jsonify({"sent": True})


@bp.route("/api/password/forgot", methods=["POST"])
def forgot_password():
    email = accounts.normalize_email((request.get_json(silent=True) or {}).get("email"))
    if accounts.is_valid_email(email) and not accounts.limited(f"reset:{email}", 3, 3600):
        row = _account_exists(email)
        if row:
            try:
                _send_reset(email, row[0])
            except Exception as e:
                logger.error(f"Reset mail failed: {e}")
    # always the same answer, so the form does not reveal who has an account
    return jsonify({"sent": True})


@bp.route("/api/password/reset", methods=["POST"])
def reset_password():
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    try:
        payload = _signer(RESET_SALT).loads(data.get("token") or "", max_age=RESET_SECONDS)
    except (BadSignature, SignatureExpired):
        return jsonify({"error": "This reset link has expired or was already used. Ask for a new one."}), 400
    row = _account_exists(payload["e"])
    if not row or not row[0].endswith(payload["h"]):
        return jsonify({"error": "This reset link was already used. Ask for a new one."}), 400
    if len(password) < accounts.MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"Use at least {accounts.MIN_PASSWORD_LENGTH} characters for your password."}), 400
    accounts.create_user(payload["e"], password)
    accounts.mark_verified(payload["e"])  # the reset proves the address is theirs
    return jsonify({"ok": True})


def register(app):
    app.register_blueprint(bp)
