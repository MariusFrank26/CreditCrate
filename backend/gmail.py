"""
Sending campaign email through a Gmail account with an app password.

Each account connects once with its own app password (Google account, Security,
2-Step Verification, App passwords). The password is stored encrypted with
CREDENTIAL_KEY, a Fernet key kept in the server's environment, never in the code
or the database in plain text.

Sending uses Gmail's SMTP server, so messages go out from the user's own address
and appear in their Sent folder. Gmail allows about 500 messages a day from a
personal account; the daily cap is enforced in mailing.py from the send log.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

GMAIL_HOST = "smtp.gmail.com"
GMAIL_PORT = 465
DAILY_LIMIT = 300  # kept well under Gmail's own limit, so the account is less likely to be locked


class CredentialError(Exception):
    pass


def _fernet():
    # CREDENTIAL_KEY can hold several keys, separated by commas. The first one encrypts new
    # passwords; every key can decrypt, so the others can be removed after a rotation.
    keys = [k.strip() for k in os.getenv("CREDENTIAL_KEY", "").split(",") if k.strip()]
    if not keys:
        raise CredentialError("CREDENTIAL_KEY is not set on the server, so app passwords cannot be stored.")
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt_password(app_password):
    return _fernet().encrypt(app_password.encode()).decode()


def decrypt_password(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise CredentialError("The stored app password cannot be read. Connect the account again.") from e


def _connect(address, app_password):
    context = ssl.create_default_context()
    server = smtplib.SMTP_SSL(GMAIL_HOST, GMAIL_PORT, context=context, timeout=20)
    try:
        server.login(address, app_password)
    except Exception:
        server.quit()
        raise
    return server


def check_login(address, app_password):
    """Raises smtplib.SMTPAuthenticationError if Google rejects the address or app password."""
    _connect(address, app_password).quit()


class Sender:
    """One open SMTP connection for a whole campaign, so Gmail sees one session."""

    def __init__(self, address, app_password):
        self.address = address
        self._password = app_password
        self._server = None

    def __enter__(self):
        self._server = _connect(self.address, self._password)
        return self

    def __exit__(self, *exc):
        try:
            self._server.quit()
        except Exception:
            pass
        self._server = None

    def send(self, to, subject, text, html_body, unsubscribe_url, attachment=None):
        """attachment is (file path, file name, content type), or None."""
        message = EmailMessage()
        message["From"] = self.address
        message["To"] = to
        message["Subject"] = subject
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"  # lets mail apps unsubscribe in one tap
        message.set_content(text)
        message.add_alternative(html_body, subtype="html")
        if attachment:
            path, name, content_type = attachment
            maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
            with open(path, "rb") as f:
                message.add_attachment(f.read(), maintype=maintype or "application", subtype=subtype or "octet-stream", filename=name)
        self._server.send_message(message)
