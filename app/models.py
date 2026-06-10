import datetime
import hashlib


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def dedupe_key(company, role, extra=""):
    raw = "|".join([(company or "").strip().lower(), (role or "").strip().lower(), (extra or "").strip().lower()])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def serialize(doc):
    if not doc:
        return doc
    out = dict(doc)
    if "_id" in out:
        out["id"] = str(out.pop("_id"))
    for k in ("created_at", "updated_at", "sent_at", "scheduled_for"):
        v = out.get(k)
        if isinstance(v, datetime.datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=datetime.timezone.utc)
            out[k] = v.isoformat()
    out.pop("resume_text", None)  # never ship the big resume blob to the browser
    return out


DEFAULT_PROFILE = {
    "_id": "me",
    "name": "",
    "phone": "",
    "email": "",
    "years": "",
    "domains": "",
    "summary": "",
    "skills": "",
    "titles": "",
    "education": "",
    "resume_text": "",
    "resume_path": "",
    "resume_filename": "",
    "answers": {},
}
