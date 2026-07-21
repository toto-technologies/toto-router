"""Mailer — stdlib smtplib behind settings, loud dev mode.

Configured (TOTO_GW_SMTP_HOST set): send a STARTTLS email, blocking send pushed off the event
loop via anyio.to_thread. Unconfigured (dev): print the verify link to stdout so local flows
work with no mail server. Links are NEVER returned in an API response — the token only ever
travels to the inbox (or the dev console).
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

import anyio

from .config import Settings


def _send_smtp(settings: Settings, to: str, subject: str, body: str) -> None:
    """Blocking STARTTLS send — run via anyio.to_thread so the event loop isn't parked."""
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    from . import egress  # smtplib can't ride the httpx chokepoint — gate inline (same check)

    egress.check_host(settings.smtp_host, "mailer")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_pass)
        smtp.send_message(msg)


async def send_verification(settings: Settings, email: str, verify_url: str) -> None:
    """Send (or, in dev, print) the verification link. Never surfaces the link to the caller."""
    if not settings.smtp_enabled:
        # Dev mode: the link lands in the server log, never the HTTP response.
        print(f"VERIFY LINK for {email}: {verify_url}", flush=True)
        return
    body = (
        f"Welcome to Toto.\n\nConfirm your email to finish signing in:\n\n{verify_url}\n\n"
        "This link expires in 24 hours. If you didn't request it, ignore this email."
    )
    await anyio.to_thread.run_sync(_send_smtp, settings, email, "Confirm your Toto account", body)
