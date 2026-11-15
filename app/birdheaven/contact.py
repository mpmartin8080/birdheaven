"""The front page's contact form: validate, rate-limit, and email the message.

    POST /api/contact   {"name", "email", "message", "website", "elapsed"}

The visitor's address goes in Reply-To, so replying from the inbox answers
them directly. The destination (CONTACT_EMAIL) is never sent to the browser,
which keeps it away from address scrapers.

Delivery is plain SMTP (SMTP_HOST etc.; see deploy/.env.example). With no
SMTP_HOST, each message is written to the container log at WARNING level
instead, so nothing is lost while mail isn't set up yet -- read them with
`deploy-droplet logs web`.

Spam defences, all cheap and invisible to people:
  * `website` is a honeypot field, hidden from people; bots fill it in.
  * `elapsed` is how long the page was open before sending. Nobody types a
    message in under three seconds.
  * Per-IP and site-wide rate limits (in memory; the service runs one worker).
Bot submissions get the same "sent" reply as real ones, so there is nothing to
learn by probing.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
import threading
import time
from collections import deque
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .config import Config

log = logging.getLogger("birdheaven.contact")

NAME_MAX = 100
EMAIL_MAX = 254
MESSAGE_MIN = 2
MESSAGE_MAX = 5000
MIN_ELAPSED_MS = 3000

# Deliberately loose: anything shaped like local@domain.tld, with no
# whitespace or characters that could break out of a header.
_EMAIL = re.compile(r"^[^@\s<>()\[\]\\,;:\"']+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ContactError(Exception):
    """A problem to show the visitor, with the HTTP status to send it with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class RateLimiter:
    """At most `limit` hits per key in any `window` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 5000:  # forget idle keys now and then
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] <= self.window}
            return True


per_ip = RateLimiter(limit=5, window=3600)
site_wide = RateLimiter(limit=100, window=86400)


def _one_line(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def validate(body: dict) -> tuple[str, str, str] | None:
    """(name, email, message), or None for a submission that looks like a bot."""
    if not isinstance(body, dict):
        raise ContactError("Please send the form as JSON.")
    if str(body.get("website") or "").strip():
        return None
    try:
        elapsed = float(body.get("elapsed") or 0)
    except (TypeError, ValueError):
        elapsed = 0
    if elapsed < MIN_ELAPSED_MS:
        return None

    name = _one_line(body.get("name"), NAME_MAX + 1)
    email = str(body.get("email") or "").strip()
    message = _CONTROL.sub("", str(body.get("message") or "")).replace("\r\n", "\n").strip()

    if not name:
        raise ContactError("Please tell us your name.")
    if len(name) > NAME_MAX:
        raise ContactError(f"Please keep your name under {NAME_MAX} characters.")
    if len(email) > EMAIL_MAX or not _EMAIL.match(email):
        raise ContactError("That email address doesn't look right. We need it to reply.")
    if len(message) < MESSAGE_MIN:
        raise ContactError("Please write a message.")
    if len(message) > MESSAGE_MAX:
        raise ContactError(f"Please keep your message under {MESSAGE_MAX:,} characters.")
    return name, email, message


def build_message(cfg: Config, site_name: str, name: str, email: str, message: str,
                  client_ip: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((f"{site_name} contact form", cfg.contact_from))
    msg["To"] = cfg.contact_email
    msg["Reply-To"] = formataddr((name, email))
    msg["Subject"] = f"{site_name}: message from {name}"
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=cfg.contact_from.rpartition("@")[2] or None)
    msg.set_content(
        f"{message}\n\n"
        f"-- \n"
        f"From: {name} <{email}>\n"
        f"Sent with the contact form on the {site_name} front page"
        f" (visitor IP {client_ip}).\n"
        f"Reply to this email to answer them.\n"
    )
    return msg


def deliver(cfg: Config, msg: EmailMessage) -> None:
    """Send over SMTP, or log the message when SMTP isn't configured."""
    if not cfg.smtp_host:
        log.warning("contact form (SMTP_HOST not set, so logged instead of emailed):\n%s",
                    msg.as_string())
        return
    context = ssl.create_default_context()
    if cfg.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20,
                                                context=context)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
    with server:
        if cfg.smtp_security == "starttls":
            server.starttls(context=context)
        if cfg.smtp_username:
            server.login(cfg.smtp_username, cfg.smtp_password)
        server.send_message(msg)


def handle(cfg: Config, site_name: str, body: dict, client_ip: str) -> None:
    """Validate and send one submission. Raises ContactError for the visitor."""
    fields = validate(body)
    if fields is None:
        log.info("contact form: dropped a likely bot submission from %s", client_ip)
        return
    if not per_ip.allow(client_ip):
        raise ContactError("That's a lot of messages in a short time. Please try again later.", 429)
    if not site_wide.allow("*"):
        log.warning("contact form: site-wide daily limit reached")
        raise ContactError("The contact form is very busy right now. Please try again tomorrow.", 429)
    name, email, message = fields
    msg = build_message(cfg, site_name, name, email, message, client_ip)
    try:
        deliver(cfg, msg)
    except (OSError, smtplib.SMTPException) as err:
        log.error("contact form: sending via %s:%s failed: %s", cfg.smtp_host, cfg.smtp_port, err)
        raise ContactError("Sorry, your message couldn't be sent just now. Please try again later.",
                           502) from None
    log.info("contact form: message from %s delivered to %s", client_ip,
             "SMTP" if cfg.smtp_host else "the log")
