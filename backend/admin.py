"""Admin view for the owner: sign-ups, verified accounts and plans.

Only the emails in the ADMIN_EMAILS setting (comma-separated) can open it. The check runs on
the server, so nobody can give themselves access from the site. Plans are counted with the
same rules the app uses for access (see accounts.plan_for).
"""

import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, jsonify, request

import accounts

bp = Blueprint("admin", __name__)

DAY = 86400
CHART_DAYS = 30
RECENT_LIMIT = 25


def admin_emails():
    return {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_admin(email):
    return bool(email) and email in admin_emails()


def admin_only(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        email = accounts.current_email()
        if not email:
            return jsonify({"error": "Sign in first."}), 401
        if email not in admin_emails():
            return jsonify({"error": "This page is only for the CreditCrate owner."}), 403
        return view(*args, **kwargs)
    return wrapper


def _load_users():
    with accounts.db() as conn:
        conn.row_factory = sqlite3.Row
        users = [dict(r) for r in conn.execute(
            "SELECT email, created_at, email_verified_at, plan, trial_ends_at, plan_expires_at FROM users"
        )]
        newsletter = conn.execute("SELECT COUNT(*) FROM newsletter_signups").fetchone()[0]
    return users, newsletter


@bp.route("/api/admin/stats", methods=["GET"])
@admin_only
def admin_stats():
    now = time.time()
    users, newsletter = _load_users()

    verified = [u for u in users if u["email_verified_at"]]

    def signed_up_since(seconds):
        return sum(1 for u in users if (u["created_at"] or 0) >= now - seconds)

    # One bar per day, counted in UTC (the server's clock)
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    days = [today - timedelta(days=CHART_DAYS - 1 - i) for i in range(CHART_DAYS)]
    per_day = {d: {"signups": 0, "verified": 0} for d in days}
    for u in users:
        if not u["created_at"]:
            continue
        day = datetime.fromtimestamp(u["created_at"], tz=timezone.utc).date()
        if day in per_day:
            per_day[day]["signups"] += 1
            if u["email_verified_at"]:
                per_day[day]["verified"] += 1

    plans = Counter(accounts.plan_for(u["plan"], u["trial_ends_at"], u["plan_expires_at"], now)["plan"] for u in users)

    recent = sorted(users, key=lambda u: u["created_at"] or 0, reverse=True)[:RECENT_LIMIT]

    return jsonify({
        "generated_at": now,
        "totals": {
            "accounts": len(users),
            "verified": len(verified),
            "unverified": len(users) - len(verified),
            "newsletter": newsletter,
        },
        "signups": {
            "day": signed_up_since(DAY),
            "week": signed_up_since(7 * DAY),
            "month": signed_up_since(30 * DAY),
        },
        "daily": [
            {"day": d.isoformat(), **per_day[d]} for d in days
        ],
        "plans": {
            "trial": plans.get("trial", 0),
            "pro": plans.get("pro", 0),
            "lifetime": plans.get("lifetime", 0),
            "expired": plans.get("expired", 0),
        },
        "recent": [
            {
                "email": u["email"],
                "created_at": u["created_at"],
                "verified": bool(u["email_verified_at"]),
                "plan": accounts.plan_for(u["plan"], u["trial_ends_at"], u["plan_expires_at"], now)["plan"],
            }
            for u in recent
        ],
    })


ACCOUNT_VIEWS = {"all", "verified", "unverified", "trial", "pro", "lifetime", "expired", "new24", "new7", "new30"}


def _matches(view, user, now):
    """Whether an account belongs in the list the owner opened ("all", "verified", "day:YYYY-MM-DD", ...)."""
    created = user["created_at"] or 0
    if view == "all":
        return True
    if view == "verified":
        return bool(user["email_verified_at"])
    if view == "unverified":
        return not user["email_verified_at"]
    if view in ("trial", "pro", "lifetime", "expired"):
        plan = accounts.plan_for(user["plan"], user["trial_ends_at"], user["plan_expires_at"], now)["plan"]
        return plan == view
    if view in ("new24", "new7", "new30"):
        seconds = {"new24": DAY, "new7": 7 * DAY, "new30": 30 * DAY}[view]
        return created >= now - seconds
    if view.startswith("day:"):
        return bool(created) and datetime.fromtimestamp(created, tz=timezone.utc).date().isoformat() == view[4:]
    return False


@bp.route("/api/admin/accounts", methods=["GET"])
@admin_only
def admin_accounts():
    view = request.args.get("view", "all")
    if view not in ACCOUNT_VIEWS and not view.startswith("day:"):
        return jsonify({"error": "Unknown list."}), 400
    now = time.time()
    users, _ = _load_users()
    rows = [u for u in users if _matches(view, u, now)]
    rows.sort(key=lambda u: u["created_at"] or 0, reverse=True)
    return jsonify({
        "view": view,
        "accounts": [
            {
                "email": u["email"],
                "created_at": u["created_at"],
                "verified": bool(u["email_verified_at"]),
                "plan": accounts.plan_for(u["plan"], u["trial_ends_at"], u["plan_expires_at"], now)["plan"],
            }
            for u in rows
        ],
    })


PLAN_CHOICES = {"pro", "trial", "lifetime", "free"}


@bp.route("/api/admin/plan", methods=["POST"])
@admin_only
def admin_set_plan():
    """Changes one account's plan, the same way set_plan.py does."""
    data = request.get_json(silent=True) or {}
    email = accounts.normalize_email(data.get("email"))
    plan = (data.get("plan") or "").strip()
    if plan not in PLAN_CHOICES:
        return jsonify({"error": "Choose Pro, a free trial, lifetime or trial ended."}), 400
    try:
        months = int(data.get("months", 1))
    except (TypeError, ValueError):
        months = 0
    if plan == "pro" and not 1 <= months <= 120:
        return jsonify({"error": "Pro can be given for 1 to 120 months."}), 400
    if plan == "free" and email == accounts.current_email():
        return jsonify({"error": "You cannot end your own trial from here."}), 400

    now = time.time()
    with accounts.db() as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            return jsonify({"error": "No account with that email."}), 404
        if plan == "pro":
            conn.execute("UPDATE users SET plan = 'pro', plan_expires_at = ? WHERE email = ?",
                         (now + months * 31 * DAY, email))
        elif plan == "trial":
            conn.execute("UPDATE users SET trial_ends_at = ? WHERE email = ?",
                         (now + accounts.TRIAL_DAYS * DAY, email))
        elif plan == "lifetime":
            conn.execute("UPDATE users SET plan = 'lifetime', plan_expires_at = NULL WHERE email = ?", (email,))
        else:  # free: the trial is over and the account must choose Pro
            conn.execute("UPDATE users SET plan = NULL, plan_expires_at = NULL, trial_ends_at = ? WHERE email = ?",
                         (now - 1, email))
        row = dict(conn.execute(
            "SELECT plan, trial_ends_at, plan_expires_at FROM users WHERE email = ?", (email,)
        ).fetchone())
    return jsonify({
        "ok": True,
        "email": email,
        "plan": accounts.plan_for(row["plan"], row["trial_ends_at"], row["plan_expires_at"], now)["plan"],
    })


@bp.route("/api/admin/newsletter", methods=["GET"])
@admin_only
def admin_newsletter():
    with accounts.db() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT email, created_at FROM newsletter_signups ORDER BY created_at DESC"
        )]
    return jsonify({"subscribers": rows})


def register(app):
    app.register_blueprint(bp)
