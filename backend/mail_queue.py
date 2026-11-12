"""The campaign send queue: saves who is still to get a campaign, and a single background worker
that sends it one email at a time. Saved in the database, so a restart resumes where it stopped.
"""

import json
import logging
import os
import random
import smtplib
import threading
import time

import gmail
import mailing

logger = logging.getLogger(__name__)


_queue_lock = threading.Lock()  # guards claiming and enqueueing rows


_stop = threading.Event()  # set when the backend is shutting down


_worker = None


def _clear_attachment(campaign_id):
    """Removes a campaign's attached file from the server and forgets it."""
    with mailing._conn() as conn:
        row = conn.execute("SELECT attachment_path FROM mail_campaigns WHERE id = ?", (campaign_id,)).fetchone()
        conn.execute(
            "UPDATE mail_campaigns SET attachment_path = NULL, attachment_name = NULL, "
            "attachment_type = NULL, attachment_size = NULL WHERE id = ?",
            (campaign_id,),
        )
    if row and row["attachment_path"]:
        try:
            os.remove(row["attachment_path"])
        except OSError:
            pass  # already gone


def _audience(campaign):
    """The saved audience of a campaign: a role, a tag, a chosen list, or nothing (everyone)."""
    try:
        return json.loads(campaign["audience"] or "{}")
    except (TypeError, ValueError):
        return {}


def _remaining(conn, campaign):
    """How many people the campaign still owes."""
    a = _audience(campaign)
    return len(mailing._recipients(conn, campaign["id"], campaign["owner"], a.get("role", ""), a.get("tag", ""), a.get("emails")))


def _window_state(address, now=None):
    """(emails the account can still send in the last 24 hours, when its full daily allowance is back)."""
    now = now or time.time()
    if not address:
        return gmail.DAILY_LIMIT, now
    with mailing._conn() as conn:
        times = [r[0] for r in conn.execute(
            "SELECT sent_at FROM mail_sends WHERE sender = ? AND status = 'sent' AND sent_at > ?",
            (address, now - 86400),
        )]
    free = max(0, gmail.DAILY_LIMIT - len(times))
    full_at = max(times) + 86400 if times else now
    return free, full_at


def _done_estimate(address, remaining, now=None):
    """When the last of the remaining people gets the campaign, given the daily limit."""
    now = now or time.time()
    if remaining <= 0:
        return now
    free, full_at = _window_state(address, now)
    if remaining <= free:
        return now + remaining * mailing.AVERAGE_SEND_SECONDS
    rest = remaining - free
    days = -(-rest // gmail.DAILY_LIMIT)  # rounds up: how many more days of sending
    last_day = rest - (days - 1) * gmail.DAILY_LIMIT
    return full_at + (days - 1) * 86400 + last_day * mailing.AVERAGE_SEND_SECONDS


def _free_again_at(address, now=None):
    """When the account can send again: now if it has room, else when its oldest send in the last 24 hours expires."""
    now = now or time.time()
    if not address:
        return now
    with mailing._conn() as conn:
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
    with mailing._conn() as conn:
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
        with mailing._conn() as conn:
            campaign = conn.execute("SELECT * FROM mail_campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if campaign is None or campaign["status"] == "sending":
                return None
            people = mailing._recipients(conn, campaign_id, owner, audience.get("role", ""), audience.get("tag", ""), audience.get("emails"))
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


def _set_queue_row(row_id, status, error=None):
    with mailing._conn() as conn:
        conn.execute("UPDATE mail_queue SET status = ?, error = ? WHERE id = ?", (status, error, row_id))


def _stop_campaign(campaign_id, status):
    """Stops a campaign. Its people stay queued, and sending again picks them up."""
    with mailing._conn() as conn:
        conn.execute("UPDATE mail_campaigns SET status = ? WHERE id = ?", (status, campaign_id))
    logger.warning(f"Campaign {campaign_id} stopped: {status}")


def _finish_if_done(campaign_id):
    """Marks a campaign sent (or partial, if people are still waiting) once its queue is empty.
    A campaign that is fully sent no longer needs its attachment, so the file is removed."""
    finished = False
    with mailing._conn() as conn:
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
            finished = True
    if finished:
        _clear_attachment(campaign_id)


def _reconcile():
    """Finishes campaigns that are still marked sending but have nothing left in the queue."""
    with mailing._conn() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT c.id FROM mail_campaigns c WHERE c.status = 'sending' AND NOT EXISTS "
            "(SELECT 1 FROM mail_queue q WHERE q.campaign_id = c.id AND q.status IN ('pending', 'sending'))"
        )]
    for campaign_id in ids:
        _finish_if_done(campaign_id)


def _next_queued():
    """Takes the next waiting email of a campaign that is sending. Returns None when there is none."""
    with _queue_lock:
        with mailing._conn() as conn:
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
    with mailing._conn() as conn:
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
    account = mailing._load_account(owner)
    if not account:
        _set_queue_row(item["id"], "pending")
        _stop_campaign(campaign_id, "stopped: Connect your Gmail account first.")
        return
    address, encrypted = account
    if mailing._sent_today(address) >= gmail.DAILY_LIMIT:
        # The daily limit is full. The person is not queued any more; the campaign picks them up again when it has room.
        with mailing._conn() as conn:
            conn.execute("DELETE FROM mail_queue WHERE id = ?", (item["id"],))
        _stop_campaign(campaign_id, "partial")
        return
    attachment = None
    if campaign["attachment_path"]:
        if not os.path.exists(campaign["attachment_path"]):
            _set_queue_row(item["id"], "pending")
            _stop_campaign(campaign_id, "stopped: The attached file is missing. Attach it again to continue.")
            return
        attachment = (campaign["attachment_path"], campaign["attachment_name"], campaign["attachment_type"])
    try:
        sender = senders.get(address)
        if sender is None:
            sender = gmail.Sender(address, gmail.decrypt_password(encrypted))
            sender.__enter__()
            senders[address] = sender
        pack_url = pack["url"] if pack else None
        text, html_body = mailing._render(dict(campaign), dict(contact), pack_url)
        extra = {"attachment": attachment} if attachment else {}
        sender.send(email, mailing.fill_variables(campaign["subject"], dict(contact), pack_url), text, html_body,
                    mailing.unsubscribe_link(email, owner), **extra)
        mailing._log_send(campaign_id, email, address, "sent", None)
        _set_queue_row(item["id"], "sent")
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
        logger.warning(f"Send to {email} refused: {e}")
        mailing._log_send(campaign_id, email, address, "failed", str(e)[:300])
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
    with mailing._conn() as conn:
        partial = [dict(r) for r in conn.execute("SELECT * FROM mail_campaigns WHERE status = 'partial'")]
    for campaign in partial:
        account = mailing._load_account(campaign["owner"])
        if not account:
            continue
        address = account[0]
        if _window_state(address)[0] <= 0:
            continue
        result = _queue_batch(campaign["id"], campaign["owner"], _audience(campaign), address)
        if result is not None and result[0] == 0 and result[1] == 0:
            # nobody is left to send to any more (for example, everyone unsubscribed)
            with mailing._conn() as conn:
                conn.execute(
                    "UPDATE mail_campaigns SET status = 'sent', sent_at = ? WHERE id = ? AND status = 'partial'",
                    (time.time(), campaign["id"]),
                )
            _clear_attachment(campaign["id"])


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
                # A mailing.random pause between emails, like a person sending by hand
                _stop.wait(random.uniform(*mailing.SEND_INTERVAL_SECONDS))
            except Exception:
                logger.exception("Campaign sender hit an error; carrying on")
                _stop.wait(5)
    finally:
        _close_senders(senders)


def _start_worker():
    """Starts the sender. Anything that was mid-send when the backend last stopped goes back to waiting."""
    global _worker
    with mailing._conn() as conn:
        conn.execute("UPDATE mail_queue SET status = 'pending' WHERE status = 'sending'")
    _reconcile()
    _worker = threading.Thread(target=_worker_loop, name="campaign-sender", daemon=True)
    _worker.start()


def _on_shutdown():
    """Runs when the server exits cleanly (gunicorn stops its worker on a redeploy).
    Lets the current email finish before the process ends."""
    _stop.set()
    if _worker is not None:
        _worker.join(timeout=8)
