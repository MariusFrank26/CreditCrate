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


def create_user(email, password):
    """Creates the account, or replaces the password of an existing one."""
    email = normalize_email(email)
    if not is_valid_email(email):
        raise ValueError("Not a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET password_hash = excluded.password_hash
        """, (email, generate_password_hash(password), time.time()))


def verify_login(email, password):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
    return row is not None and check_password_hash(row[0], password)


def issue_token(email):
    return _tokens.dumps(normalize_email(email))


def _signed_in():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    try:
        email = _tokens.loads(header[len("Bearer "):])
    except BadSignature:
        return False
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None


def login_required(view):
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
