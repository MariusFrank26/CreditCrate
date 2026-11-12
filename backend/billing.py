"""
Account settings, password change, and Pro subscriptions through Stripe.

Pro costs $9 a month or $50 a year. Stripe takes the payment, and its webhook
tells this server when a subscription is paid, which sets plan_expires_at.

Needed on the server (see README):
  STRIPE_SECRET_KEY, STRIPE_PRICE_MONTH, STRIPE_PRICE_YEAR, STRIPE_WEBHOOK_SECRET
  PUBLIC_SITE_URL (default https://creditcrate.app)
"""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time

import requests
from flask import Blueprint, request, jsonify

import accounts
import admin
import mailing

logger = logging.getLogger(__name__)

bp = Blueprint("billing", __name__)

STRIPE_API = "https://api.stripe.com/v1"
PERIOD_DAYS = {"month": 31, "year": 366}
PAID_GRACE_SECONDS = 3 * 86400  # a late renewal keeps working for a few days
WEBHOOK_TOLERANCE = 300

_db_path = None


def register(app, db_path):
    global _db_path
    _db_path = db_path
    with sqlite3.connect(db_path) as conn:
        # Stripe can send the same event twice; each event id is only applied once
        conn.execute("CREATE TABLE IF NOT EXISTS stripe_events (id TEXT PRIMARY KEY, received_at REAL)")
    app.register_blueprint(bp)


def _config(name):
    return os.getenv(name, "").strip()


def verify_stripe_signature(payload, header, secret, now=None, tolerance=WEBHOOK_TOLERANCE):
    """Checks the Stripe-Signature header against the raw request body."""
    if not header or not secret:
        return False
    timestamp = None
    signatures = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        return False
    try:
        t = int(timestamp)
    except ValueError:
        return False
    now = time.time() if now is None else now
    if abs(now - t) > tolerance:
        return False
    signed = f"{t}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


# ── account ─────────────────────────────────────────────────────────────────

@bp.route("/api/account", methods=["GET"])
@accounts.signed_in
def get_account():
    email = accounts.current_email()
    status = accounts.access_status(email)
    mail = mailing._account_status(email)
    return jsonify({
        "email": email,
        "plan": status["plan"],
        "active": status["active"],
        "expires_at": status["expires_at"],
        "mail": {"connected": mail["connected"], "address": mail.get("address")},
        "is_admin": admin.is_admin(email),  # shows the Admin link in the top bar
    })


@bp.route("/api/account/password", methods=["POST"])
@accounts.signed_in
def change_password():
    data = request.get_json(silent=True) or {}
    email = accounts.current_email()
    current = data.get("current") or ""
    new = data.get("new") or ""
    if not accounts.verify_login(email, current):
        return jsonify({"error": "Your current password is not correct."}), 401
    if new == current:
        return jsonify({"error": "Choose a different password."}), 400
    try:
        accounts.create_user(email, new)  # replaces the password of an existing account
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


# ── billing ─────────────────────────────────────────────────────────────────

@bp.route("/api/billing/checkout", methods=["POST"])
@accounts.signed_in
def start_checkout():
    data = request.get_json(silent=True) or {}
    interval = data.get("interval")
    price = _config("STRIPE_PRICE_MONTH") if interval == "month" else _config("STRIPE_PRICE_YEAR") if interval == "year" else ""
    key = _config("STRIPE_SECRET_KEY")
    if interval not in PERIOD_DAYS:
        return jsonify({"error": "Choose monthly or yearly."}), 400
    if not key or not price:
        return jsonify({"error": "Payments are not set up on this server yet."}), 503
    site = _config("PUBLIC_SITE_URL") or "https://creditcrate.app"
    email = accounts.current_email()
    try:
        resp = requests.post(f"{STRIPE_API}/checkout/sessions", auth=(key, ""), timeout=15, data={
            "mode": "subscription",
            "line_items[0][price]": price,
            "line_items[0][quantity]": 1,
            "customer_email": email,
            "client_reference_id": email,
            "metadata[interval]": interval,
            "subscription_data[metadata][interval]": interval,
            "success_url": f"{site}/settings.html?paid=1",
            "cancel_url": f"{site}/pricing.html",
        })
    except requests.RequestException:
        return jsonify({"error": "Could not reach Stripe. Try again in a moment."}), 502
    if resp.status_code >= 300:
        logger.error(f"Stripe checkout failed: {resp.status_code} {resp.text[:300]}")
        return jsonify({"error": "Stripe could not start the checkout."}), 502
    return jsonify({"url": resp.json()["url"]})


def _set_paid_until(customer_id, email, until):
    with sqlite3.connect(_db_path) as conn:
        if email:
            conn.execute("UPDATE users SET plan = CASE WHEN plan = 'lifetime' THEN plan ELSE 'pro' END, plan_expires_at = ?, stripe_customer = ? WHERE email = ?",
                         (until, customer_id, email))
        elif customer_id:
            conn.execute("UPDATE users SET plan = CASE WHEN plan = 'lifetime' THEN plan ELSE 'pro' END, plan_expires_at = ? WHERE stripe_customer = ?",
                         (until, customer_id))


@bp.route("/api/billing/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    secret = _config("STRIPE_WEBHOOK_SECRET")
    if not verify_stripe_signature(payload, request.headers.get("Stripe-Signature", ""), secret):
        return jsonify({"error": "Bad signature."}), 400
    event = json.loads(payload)
    event_id = event.get("id") or ""
    if not event_id:
        return jsonify({"error": "The event has no id."}), 400
    with sqlite3.connect(_db_path) as conn:
        if conn.execute("SELECT 1 FROM stripe_events WHERE id = ?", (event_id,)).fetchone():
            return jsonify({"ok": True, "duplicate": True})
    kind = event.get("type")
    obj = event.get("data", {}).get("object", {})
    now = time.time()

    if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
        interval = (obj.get("metadata") or {}).get("interval", "month")
        email = obj.get("client_reference_id") or obj.get("customer_email")
        days = PERIOD_DAYS.get(interval, PERIOD_DAYS["month"])
        _set_paid_until(obj.get("customer"), email, now + days * 86400 + PAID_GRACE_SECONDS)

    elif kind == "invoice.paid":
        lines = (obj.get("lines") or {}).get("data") or []
        period_end = lines[0]["period"]["end"] if lines and lines[0].get("period") else None
        if period_end:
            _set_paid_until(obj.get("customer"), None, period_end + PAID_GRACE_SECONDS)

    # Recorded only after it was applied, so a failed attempt is retried by Stripe
    with sqlite3.connect(_db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO stripe_events (id, received_at) VALUES (?, ?)", (event_id, time.time()))
    return jsonify({"ok": True})
