import email
import email.utils
import imaplib
import os
import smtplib
import ssl
from email.header import decode_header
from email.message import EmailMessage

from bs4 import BeautifulSoup

from .config import settings


def _decode(value) -> str:
    if not value:
        return ""
    parts = []
    for chunk, enc in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", errors="ignore"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _text_from_message(msg) -> str:
    if msg.is_multipart():
        plain = None
        html = None
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = part.get_payload(decode=True)
            elif ctype == "text/html" and html is None:
                html = part.get_payload(decode=True)
        if plain:
            return plain.decode(errors="ignore")
        if html:
            return BeautifulSoup(html.decode(errors="ignore"), "html.parser").get_text(
                " ", strip=True
            )
        return ""
    payload = msg.get_payload(decode=True) or b""
    raw = payload.decode(errors="ignore")
    if msg.get_content_type() == "text/html":
        return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return raw


def read_alert_emails(senders: list[str], limit: int = 50) -> list[dict]:
    """Read UNSEEN inbox emails from the given senders and mark them seen."""
    out: list[dict] = []
    box = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        box.login(settings.gmail_address, settings.gmail_app_password)
        box.select("INBOX")
        _, data = box.search(None, "UNSEEN")
        ids = data[0].split()
        for num in ids[-limit:]:
            _, msg_data = box.fetch(num, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            from_addr = (email.utils.parseaddr(msg.get("From"))[1] or "").lower()
            if senders and not any(s.lower() in from_addr for s in senders):
                continue  # leave unrelated mail unread
            out.append(
                {
                    "from": from_addr,
                    "subject": _decode(msg.get("Subject")),
                    "text": _text_from_message(msg),
                }
            )
            box.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return out


def send_email(to: str, subject: str, body: str, resume_path: str = "", cc: str = "", bcc: str = "", from_addr: str = "") -> None:
    """Send a plain-text email with the resume attached, from the configured Gmail.
    `to`, `cc` and `bcc` may each hold several comma-separated addresses."""
    sender = from_addr or settings.gmail_address
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg.set_content(body)

    path = resume_path or settings.resume_path
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="application",
                subtype="pdf",
                filename=os.path.basename(path),
            )

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx) as server:
        server.login(settings.gmail_address, settings.gmail_app_password)
        server.send_message(msg)
