"""
Sets an account's plan by hand, e.g. to give your own account Pro or to restart a trial.

  python set_plan.py you@example.com pro 12     # Pro for 12 more months
  python set_plan.py you@example.com trial      # a fresh 7-day trial from now
  python set_plan.py you@example.com lifetime  # Lifetime Pro, never expires (owner grants only)
  python set_plan.py you@example.com free      # trial ended: the account is blocked until it pays
"""

import sys
import time

import accounts

TRIAL_DAYS = accounts.TRIAL_DAYS

if len(sys.argv) < 3:
    sys.exit(__doc__)

email = accounts.normalize_email(sys.argv[1])
plan = sys.argv[2]

with accounts.db() as conn:
    exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
    if not exists:
        sys.exit(f"No account for {email}.")
    if plan == "pro":
        months = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        conn.execute("UPDATE users SET plan = 'pro', plan_expires_at = ? WHERE email = ?",
                     (time.time() + months * 31 * 86400, email))
        print(f"{email}: Pro for {months} month(s).")
    elif plan == "trial":
        conn.execute("UPDATE users SET trial_ends_at = ? WHERE email = ?",
                     (time.time() + TRIAL_DAYS * 86400, email))
        print(f"{email}: new {TRIAL_DAYS}-day trial.")
    elif plan == "lifetime":
        conn.execute("UPDATE users SET plan = 'lifetime', plan_expires_at = NULL WHERE email = ?", (email,))
        print(f"{email}: Lifetime Pro.")
    elif plan == "free":
        conn.execute("UPDATE users SET plan = NULL, plan_expires_at = NULL, trial_ends_at = ? WHERE email = ?",
                     (time.time() - 1, email))
        print(f"{email}: trial ended, the account now needs Pro.")
    else:
        sys.exit("Plan must be 'pro', 'trial', 'lifetime' or 'free'.")
