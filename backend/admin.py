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

from flask import Blueprint, jsonify

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
    with sqlite3.connect(accounts.DB_PATH) as conn:
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


def register(app):
    app.register_blueprint(bp)
