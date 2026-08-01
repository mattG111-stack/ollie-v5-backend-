"""Outbound email + SMS for onboarding verification.

Both channels degrade gracefully: if no provider is configured we log the
message (including the code) to the server instead of sending, so the whole
signup → verify flow works end-to-end in development with zero external setup.

- Email: Resend (set RESEND_API_KEY). https://resend.com
- SMS: Twilio (not wired yet — codes are logged until credentials are added).
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("ollie.notify")


def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send an email via Resend. Returns True if handed off to the provider.
    With no RESEND_API_KEY set, logs the message and returns False."""
    key = settings.resend_api_key
    if not key:
        log.warning("[email:log-only] to=%s subject=%s\n%s", to, subject, text or html)
        return False
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"from": settings.email_from, "to": [to], "subject": subject, "html": html},
            timeout=15,
        )
        if r.status_code >= 300:
            log.error("resend send failed %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:                       # never let email break the request
        log.error("resend send error: %s", e)
        return False


def send_sms(to: str, body: str) -> bool:
    """Send an SMS via Twilio. Returns True if handed off to the provider.
    Twilio isn't wired yet — for now we always log the code and return False."""
    sid, token, frm = settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number
    if not (sid and token and frm):
        log.warning("[sms:log-only] to=%s body=%s", to, body)
        return False
    try:
        r = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"To": to, "From": frm, "Body": body},
            auth=(sid, token),
            timeout=15,
        )
        if r.status_code >= 300:
            log.error("twilio send failed %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        log.error("twilio send error: %s", e)
        return False


def verification_email_html(code: str, minutes: int) -> str:
    return (
        f'<div style="font-family:system-ui,sans-serif;max-width:440px;margin:0 auto">'
        f'<h2 style="color:#0A8754">Confirm your email</h2>'
        f'<p>Your Ollie verification code is:</p>'
        f'<div style="font-size:32px;font-weight:700;letter-spacing:.2em;'
        f'background:#f3f4f6;border-radius:12px;padding:16px;text-align:center">{code}</div>'
        f'<p style="color:#6b7280;font-size:13px">This code expires in {minutes} minutes.</p>'
        f'</div>'
    )
