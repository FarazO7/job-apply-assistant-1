import os
import re

from . import gmail_client, llm
from .config import csv, settings
from .db import applications, profiles
from .fetcher import best_effort_fetch
from .models import DEFAULT_PROFILE, dedupe_key, now
from urllib.parse import urlsplit, urlunsplit

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm_url(url):
    """Strip tracking query params so the same job collapses to one dedupe key."""
    if not url:
        return ""
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", "")).lower()
    except Exception:
        return url.split("?")[0].rstrip("/").lower()


def _join(items):
    return ", ".join([x.strip() for x in (items or []) if x and x.strip()])


def subject_for(role):
    r = (role or "this role").strip() or "this role"
    return f"Application for {r} | Immediate Joiner"


def get_profile():
    doc = profiles.find_one({"_id": "me"})
    if not doc:
        profiles.insert_one(dict(DEFAULT_PROFILE))
        return dict(DEFAULT_PROFILE)
    return doc


def effective_resume_path():
    p = get_profile().get("resume_path")
    if p and os.path.exists(p):
        return p
    return settings.resume_path


def _status_for(recipient, missing):
    if not recipient:
        return "apply_on_site"
    if missing:
        return "needs_info"
    return "drafted"


def _upsert(doc):
    key = doc["dedupe_key"]
    payload = {k: v for k, v in doc.items() if k != "created_at"}
    payload["updated_at"] = now()
    applications.update_one({"dedupe_key": key}, {"$setOnInsert": {"created_at": now()}, "$set": payload}, upsert=True)
    return applications.find_one({"dedupe_key": key})


def ingest_post(text=None, url=None):
    source = "post"
    if url and not text:
        status, fetched = best_effort_fetch(url)
        if status == "needs_paste":
            return {"needs_paste": True, "url": url}
        text = fetched
        source = "link"
    if not text or not text.strip():
        return {"error": "empty post"}
    profile = get_profile()
    data = llm.parse_and_write_post(text, profile, profile.get("answers", {}))
    to = _join(data.get("recipient_emails", []))
    cc = _join(data.get("cc_emails", []))
    bcc = _join(data.get("bcc_emails", []))
    missing = data.get("missing_info", []) or []
    company = data.get("company", "")
    role = data.get("chosen_role", "")
    doc = {
        "source": source,
        "dedupe_key": dedupe_key(company, role, _norm_url(url) or to),
        "company": company, "role": role,
        "all_roles": data.get("roles", []), "other_roles": data.get("other_roles", []),
        "recipient_email": to, "cc_emails": cc, "bcc_emails": bcc,
        "subject": subject_for(role) if to else "", "body": data.get("body", ""),
        "missing_info": missing, "status": _status_for(to, missing),
        "source_url": url or "", "raw_text": text[:5000],
        "prepared_answers": [], "prefill_url": "",
        "sent_at": None,
    }
    return _upsert(doc)


def ingest_alerts():
    profile = get_profile()
    answers = profile.get("answers", {})
    emails = gmail_client.read_alert_emails(csv(settings.alert_senders))
    seen = 0
    added = 0
    for mail in emails:
        try:
            listings = llm.parse_alert(mail["text"])
        except Exception:
            continue
        for listing in listings:
            seen += 1
            recipient = listing.get("recipient_email", "")
            company = listing.get("company", "")
            role = listing.get("title", "")
            body, missing = "", []
            if recipient:
                try:
                    drafted = llm.write_email_for_listing(listing, profile, answers)
                    body = drafted.get("body", "")
                    missing = drafted.get("missing_info", []) or []
                except Exception:
                    pass
            doc = {
                "source": "alert",
                "dedupe_key": dedupe_key(company, role, _norm_url(listing.get("url", ""))),
                "company": company, "role": role, "all_roles": [], "other_roles": [],
                "recipient_email": recipient, "cc_emails": "", "bcc_emails": "",
                "location": listing.get("location", ""),
                "posted": listing.get("posted", ""),
                "subject": subject_for(role) if recipient else "", "body": body,
                "missing_info": missing, "status": _status_for(recipient, missing),
                "source_url": listing.get("url", ""), "raw_text": "",
                "prepared_answers": [], "prefill_url": "",
                "sent_at": None,
            }
            before = applications.count_documents({"dedupe_key": doc["dedupe_key"]})
            _upsert(doc)
            if before == 0:
                added += 1
    return {"emails_read": len(emails), "listings_seen": seen, "new_applications": added}


def regenerate(app_doc):
    profile = get_profile()
    answers = profile.get("answers", {})
    if app_doc.get("raw_text"):
        data = llm.parse_and_write_post(app_doc["raw_text"], profile, answers)
        recipient = _join(data.get("recipient_emails", [])) or app_doc.get("recipient_email", "")
        body = data.get("body", "")
        missing = data.get("missing_info", []) or []
        role = data.get("chosen_role", "") or app_doc.get("role", "")
        cc = _join(data.get("cc_emails", [])) or app_doc.get("cc_emails", "")
        bcc = _join(data.get("bcc_emails", [])) or app_doc.get("bcc_emails", "")
    else:
        listing = {"title": app_doc.get("role", ""), "company": app_doc.get("company", ""),
                   "location": app_doc.get("location", ""), "url": app_doc.get("source_url", ""),
                   "recipient_email": app_doc.get("recipient_email", "")}
        drafted = llm.write_email_for_listing(listing, profile, answers)
        recipient = app_doc.get("recipient_email", "")
        body = drafted.get("body", "")
        missing = drafted.get("missing_info", []) or []
        role = app_doc.get("role", "")
        cc = app_doc.get("cc_emails", "")
        bcc = app_doc.get("bcc_emails", "")
    applications.update_one({"_id": app_doc["_id"]}, {"$set": {
        "subject": subject_for(role) if recipient else "", "body": body, "missing_info": missing,
        "recipient_email": recipient, "cc_emails": cc, "bcc_emails": bcc,
        "status": _status_for(recipient, missing), "updated_at": now()}})
    return applications.find_one({"_id": app_doc["_id"]})


def _extract_pdf_text(path):
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def save_resume(file_bytes, filename):
    """Save the uploaded PDF into the project folder, parse it, and update the profile."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "resume.pdf")
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    path = os.path.join(PROJECT_DIR, "resume_" + base)
    with open(path, "wb") as f:
        f.write(file_bytes)
    text = _extract_pdf_text(path)
    fields = {}
    if text.strip():
        try:
            fields = llm.parse_resume(text)
        except Exception:
            fields = {}
    update = {k: fields.get(k, "") for k in
              ("name", "phone", "email", "years", "domains", "summary", "skills", "titles", "education")
              if fields.get(k)}
    update["resume_text"] = text
    update["resume_path"] = path
    update["resume_filename"] = os.path.basename(path)
    profiles.update_one({"_id": "me"}, {"$set": update}, upsert=True)
    return profiles.find_one({"_id": "me"})