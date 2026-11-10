"""
Accounts for CreditCrate: only signed-in users can run credit searches.

Sign-up is closed. Accounts are created on the server with create_user.py.
Newsletter sign-ups are stored in the same SQLite file.
"""

import os
import re
import time
import secrets
import sqlite3
import logging
from functools import wraps

from flask import request, jsonify
from itsdangerous import URLSafeSerializer, BadSignature
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

DB_DIR = os.getenv("CACHE_DB_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "creditcrate.db")  # same file as app.py uses

MIN_PASSWORD_LENGTH = 8
TRIAL_DAYS = 7
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

AUTH_SECRET = os.getenv("AUTH_SECRET")
if not AUTH_SECRET:
    AUTH_SECRET = secrets.token_hex(32)
    logger.warning("AUTH_SECRET is not set; everyone is signed out whenever the backend restarts.")
_tokens = URLSafeSerializer(AUTH_SECRET, salt="creditcrate-auth")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at REAL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        for column, kind in (("plan", "TEXT"), ("trial_ends_at", "REAL"), ("plan_expires_at", "REAL"), ("stripe_customer", "TEXT")):
            if column not in columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {kind}")
        if "email_verified_at" not in columns:
            # Existing accounts were created by the owner, so they count as verified from now on
            conn.execute("ALTER TABLE users ADD COLUMN email_verified_at REAL")
            conn.execute("UPDATE users SET email_verified_at = ?", (time.time(),))
        # Accounts that existed before trials got a fresh 7 days from now, not from when they were created
        conn.execute("UPDATE users SET trial_ends_at = ? WHERE trial_ends_at IS NULL", (time.time() + TRIAL_DAYS * 86400,))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS newsletter_signups (
                email TEXT PRIMARY KEY,
                created_at REAL
            )
        """)


def normalize_email(email):
    return (email or "").strip().lower()


def is_valid_email(email):
    return len(email) <= 254 and EMAIL_RE.match(email) is not None


def create_user(email, password, verified=False):
    """Creates the account, or replaces the password of an existing one. New accounts start unverified
    unless verified=True (the owner's own script), and the trial starts now either way."""
    email = normalize_email(email)
    if not is_valid_email(email):
        raise ValueError("Not a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    now = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (email, password_hash, created_at, trial_ends_at, email_verified_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET password_hash = excluded.password_hash
        """, (email, generate_password_hash(password), now, now + TRIAL_DAYS * 86400, now if verified else None))


def is_verified(email):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT email_verified_at FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
    return bool(row and row[0])


def mark_verified(email):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?) WHERE email = ?",
                     (time.time(), normalize_email(email)))


def verify_login(email, password):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
    return row is not None and check_password_hash(row[0], password)


def issue_token(email):
    return _tokens.dumps(normalize_email(email))


def current_email():
    """The signed-in account's email, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        return _tokens.loads(header[len("Bearer "):])
    except BadSignature:
        return None


def _signed_in():
    email = current_email()
    if not email:
        return False
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None


def access_status(email):
    """The account's plan: 'pro' while paid up, 'trial' for the first 7 days, otherwise 'expired'."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT plan, trial_ends_at, plan_expires_at FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return {"plan": "none", "active": False, "expires_at": None}
    return plan_for(*row)


def plan_for(plan, trial_ends_at, plan_expires_at, now=None):
    """The plan an account is on right now, from its stored plan and dates."""
    now = now or time.time()
    if plan == "lifetime":  # granted by the owner only, never expires
        return {"plan": "lifetime", "active": True, "expires_at": None}
    if plan == "pro" and plan_expires_at and plan_expires_at > now:
        return {"plan": "pro", "active": True, "expires_at": plan_expires_at}
    if trial_ends_at and trial_ends_at > now:
        return {"plan": "trial", "active": True, "expires_at": trial_ends_at}
    return {"plan": "expired", "active": False, "expires_at": trial_ends_at}


def login_required(view):
    """Signed in with an active trial or Pro plan."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _signed_in():
            return jsonify({"error": "Sign in to use CreditCrate."}), 401
        if not is_verified(current_email()):
            return jsonify({"error": "Check your email and open the link to verify your account.", "code": "unverified"}), 403
        if not access_status(current_email())["active"]:
            return jsonify({
                "error": "Your free trial has ended. Choose a plan to keep using CreditCrate.",
                "code": "trial_expired",
            }), 402
        return view(*args, **kwargs)
    return wrapper


def signed_in(view):
    """Signed in, even when the trial has ended. Used for settings, billing and the pricing screen."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _signed_in():
            return jsonify({"error": "Sign in to use CreditCrate."}), 401
        return view(*args, **kwargs)
    return wrapper


def add_newsletter_signup(email):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO newsletter_signups (email, created_at) VALUES (?, ?)",
            (normalize_email(email), time.time()),
        )
