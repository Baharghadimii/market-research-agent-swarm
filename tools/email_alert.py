"""
email_alert.py — Shared Gmail SMTP alert sender for the watcher scripts.

Used by both tools/site_healthcheck.py and tools/model_watcher.py so there's
one place that knows how to send an alert email, instead of duplicating the
SMTP logic in every script.

Requires GMAIL_APP_PASSWORD in .env (an app password generated at
myaccount.google.com/apppasswords for GMAIL_ADDRESS below — confirmed
working 2026-07-17).
"""

import os
import smtplib
from email.mime.text import MIMEText

GMAIL_ADDRESS      = "bahareh.ghad@gmail.com"
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


def send_alert(subject: str, body: str) -> tuple:
    """Sends an alert email to GMAIL_ADDRESS. Returns (ok: bool, detail: str)."""
    if not GMAIL_APP_PASSWORD:
        return False, "GMAIL_APP_PASSWORD not set in .env — cannot send email alert"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = GMAIL_ADDRESS

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        return True, "sent successfully"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
