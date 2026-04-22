from __future__ import annotations

import smtplib
from email.message import EmailMessage

import app.config as app_config


def send_email(subject: str, to_email: str, plain_text: str, html_text: str | None = None) -> tuple[bool, str | None]:
    s = app_config.SETTINGS
    if not s.SMTP_ENABLED:
        return False, "smtp_disabled"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = s.SMTP_FROM_EMAIL
    message["To"] = to_email
    message.set_content(plain_text)
    if html_text:
        message.add_alternative(html_text, subtype="html")

    try:
        if s.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(
                s.SMTP_HOST,
                s.SMTP_PORT,
                timeout=s.SMTP_TIMEOUT_SECONDS,
            )
        else:
            server = smtplib.SMTP(
                s.SMTP_HOST,
                s.SMTP_PORT,
                timeout=s.SMTP_TIMEOUT_SECONDS,
            )
        with server:
            if s.SMTP_USE_TLS and not s.SMTP_USE_SSL:
                server.starttls()
            if s.SMTP_USERNAME:
                server.login(s.SMTP_USERNAME, s.SMTP_PASSWORD)
            server.send_message(message)
        return True, None
    except Exception as exc:
        return False, str(exc)
