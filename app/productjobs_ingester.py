"""
productjobs.in ingester for Application Desk (job-apply-assistant-1).

What this does
--------------
1. Walks the productjobs.in list endpoint page by page (498 jobs, 15/page => 34 pages).
2. For every email-apply job (apply_link is null), fetches the detail record to pull `apply_email`.
3. Classifies each job into one of three lanes so your send loop knows what it can automate.
4. De-duplicates against Mongo by the job's own UUID, so re-runs never re-queue the same role.

Scope note: this module SOURCES and CLASSIFIES jobs. Drafting (OpenAI) and sending (Gmail)
stay in your existing pipeline with the human-review queue in front of them. This just feeds it.

------------------------------------------------------------------------------------------------
CONFIRM BEFORE FIRST RUN (one line):
    API_BASE below is inferred from the list XHR name `jobs?page=1&search=&location=` plus the fact
    that productjobs.in is a Next app. It is almost certainly correct, but verify it:
      DevTools -> Network -> click the `jobs?page=1...` request -> Headers -> General -> Request URL.
    If the host/path differs (e.g. a Supabase REST URL), change API_BASE and nothing else.
------------------------------------------------------------------------------------------------
"""

from __future__ import annotations

import time
import logging
from datetime import datetime, timezone
from typing import Iterator, Optional

import httpx

logger = logging.getLogger("productjobs_ingester")

# --- Configuration ------------------------------------------------------------------------------

API_BASE = "https://productjobs.in/api"          # <-- CONFIRM this from Headers -> Request URL
LIST_URL = f"{API_BASE}/jobs"                     # takes params: page, search, location
DETAIL_URL = f"{API_BASE}/jobs/{{job_id}}"        # returns single record incl. apply_email

SOURCE_NAME = "productjobs.in"
PAGE_SIZE_HINT = 15                               # observed; the code derives real page count from `count`
REQUEST_TIMEOUT = 15.0
THROTTLE_SECONDS = 0.6                            # be polite; this is not your site
USER_AGENT = "ApplicationDesk/1.0 (+job-apply-assistant; contact: your-email@example.com)"

_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


# --- Classification -----------------------------------------------------------------------------

# Lanes:
#   "email"          -> apply_link is null, apply_email present  -> your send loop can fire
#   "linkedin_manual"-> a LinkedIn *profile* (/in/) -> DM-a-person -> DO NOT automate (prohibited path)
#   "external_apply" -> forms, ATS, career pages, LinkedIn job posts -> human review / form-fill lane

def classify(job: dict) -> str:
    """Decide which lane a raw list-record belongs to. Operates on list fields only."""
    apply_link = (job.get("apply_link") or "").strip()

    if not apply_link:
        # No link on the list record == email-apply on this site.
        return "email"

    lowered = apply_link.lower()
    if "linkedin.com/in/" in lowered:
        # Personal profile: applying means messaging a human. Keep this off the automated path.
        return "linkedin_manual"

    # Everything else (Google Forms, forms.gle, keka/ATS, career sites, linkedin.com/jobs/...) is a
    # legitimate external apply link -> not sendable by email, route to the review/form-fill lane.
    return "external_apply"


# --- HTTP fetch layer ---------------------------------------------------------------------------

def _get_json(client: httpx.Client, url: str, params: Optional[dict] = None) -> Optional[dict]:
    """Single GET returning parsed JSON, or None on any failure (logged, non-fatal)."""
    try:
        resp = client.get(url, params=params, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("HTTP %s for %s params=%s", exc.response.status_code, url, params)
    except httpx.HTTPError as exc:
        logger.warning("Request error for %s params=%s: %s", url, params, exc)
    except ValueError:
        logger.warning("Non-JSON response for %s params=%s", url, params)
    return None


def iter_all_jobs(
    client: httpx.Client,
    search: str = "",
    location: str = "",
) -> Iterator[dict]:
    """
    Yield every raw list-record across all pages.

    The real page count is derived from `count` in the first response, so this stays correct even if
    the site changes its page size. Narrow the pull up front with `search` / `location` if you don't
    want all 498 (e.g. location="Bangalore" or search="APM").
    """
    first = _get_json(client, LIST_URL, {"page": 1, "search": search, "location": location})
    if not first:
        logger.error("Could not fetch page 1; aborting. Check API_BASE.")
        return

    data = first.get("data", [])
    total = int(first.get("count", len(data)))
    page_size = len(data) or PAGE_SIZE_HINT
    total_pages = max(1, -(-total // page_size))  # ceil division

    logger.info("productjobs.in: %s jobs across ~%s pages (page_size=%s)", total, total_pages, page_size)

    for job in data:
        yield job

    for page in range(2, total_pages + 1):
        time.sleep(THROTTLE_SECONDS)
        payload = _get_json(client, LIST_URL, {"page": page, "search": search, "location": location})
        if not payload:
            logger.warning("Skipping page %s (fetch failed).", page)
            continue
        for job in payload.get("data", []):
            yield job


def fetch_apply_email(client: httpx.Client, job_id: str) -> Optional[str]:
    """Second hop: pull apply_email from the detail record. Only call for lane == 'email'."""
    detail = _get_json(client, DETAIL_URL.format(job_id=job_id))
    if not detail:
        return None
    email = detail.get("apply_email")
    return email.strip() if isinstance(email, str) and email.strip() else None


def fetch_description(client: httpx.Client, job_id: str) -> Optional[str]:
    """Optional: the detail `description` (markdown) is useful for tailoring the outreach email."""
    detail = _get_json(client, DETAIL_URL.format(job_id=job_id))
    return detail.get("description") if detail else None


def fetch_detail(client: httpx.Client, job_id: str) -> Optional[dict]:
    """Single detail fetch reused for both apply_email and description (one hop, not two)."""
    return _get_json(client, DETAIL_URL.format(job_id=job_id))


# --- Searlo verification hook -------------------------------------------------------------------

def verify_email_via_searlo(email: str) -> str:
    """
    Wire this to your EXISTING Searlo email-verifier util (you already use it in Application Desk).
    Return one of Searlo's statuses: 'valid' | 'invalid' | 'accept_all' | 'unknown' | 'error'.

    Left as a hook on purpose: I won't hardcode a guessed verify URL when you already have the
    working call. Only 'valid' / 'accept_all' should proceed to the send queue; skip the rest.
    """
    # from your_app.searlo import verify_email
    # return verify_email(email)["status"]
    return "unknown"


# --- Persistence --------------------------------------------------------------------------------

# Lane -> Application Desk status, reusing the app's EXISTING vocabulary so the queue UI
# keeps working. `email` lands as a reviewable lead (address in hand, e-mail not yet drafted);
# the other lanes are apply-on-site. The `lane` field preserves the finer distinction.
_LANE_STATUS = {
    "email": "needs_info",
    "external_apply": "apply_on_site",
    "linkedin_manual": "apply_on_site",   # manual DM only; `lane` flags it as do-not-automate
}


def build_record(
    job: dict,
    lane: str,
    apply_email: Optional[str],
    description: Optional[str] = None,
) -> dict:
    """Shape a record that matches Application Desk's `applications` schema.

    The site's own UUID becomes the `dedupe_key` (the app's unique index); `_id` is left
    for Mongo to assign as an ObjectId, because every /applications route addresses documents
    by ObjectId and would break on a string id. Field names follow the app's convention
    (`role`, `recipient_email`, `source_url`, `created_at`), not the raw productjobs names.
    """
    stamp = datetime.now(timezone.utc)
    return {
        "dedupe_key": f"{SOURCE_NAME}:{job['id']}",    # UUID -> unique, idempotent key
        "source": SOURCE_NAME,
        "role": job.get("title"),                      # app uses `role`, not `title`
        "company": job.get("company"),
        "location": job.get("normalized_location") or job.get("location"),
        "recipient_email": apply_email or "",          # only the email lane carries an address
        "cc_emails": "",
        "bcc_emails": "",
        "subject": "",                                 # drafted later by the existing pipeline
        "body": "",
        "missing_info": [],
        "all_roles": [],
        "other_roles": [],
        "prepared_answers": [],
        "prefill_url": "",
        "source_url": job.get("apply_link") or "",     # app uses `source_url`
        "apply_link": job.get("apply_link"),           # keep the raw fields too, for reference
        "apply_email": apply_email,
        "raw_text": (description or "")[:5000],         # lets regenerate() draft from the JD
        "experience_level": job.get("experience_level"),
        "work_type": job.get("work_type"),
        "posted": job.get("created_at"),               # the source's posting date
        "lane": lane,                                  # email | linkedin_manual | external_apply
        "status": _LANE_STATUS.get(lane, "apply_on_site"),
        "sent_at": None,
        "created_at": stamp,
        "updated_at": stamp,
    }


def upsert_jobs(collection, records: list[dict]) -> dict:
    """
    Insert new jobs only; never overwrite ones your pipeline has already touched.

    `collection` is the app's existing `applications` collection (pymongo). We match on the
    app's unique `dedupe_key`, and $setOnInsert means a re-run leaves already-seen jobs
    (and any status you have since changed) untouched -> safe, idempotent ingestion.
    """
    from pymongo import UpdateOne

    if not records:
        return {"matched": 0, "inserted": 0}

    ops = [
        UpdateOne({"dedupe_key": r["dedupe_key"]}, {"$setOnInsert": r}, upsert=True)
        for r in records
    ]
    result = collection.bulk_write(ops, ordered=False)
    return {"matched": result.matched_count, "inserted": result.upserted_count}


# --- Orchestration ------------------------------------------------------------------------------

def ingest(
    collection,
    search: str = "",
    location: str = "",
    verify_emails: bool = True,
) -> dict:
    """
    Full run: fetch -> classify -> (for email lane) pull address + verify -> dedup upsert.

    Returns a summary dict. Wire this into APScheduler on your usual IST cadence.
    """
    counts = {"email": 0, "linkedin_manual": 0, "external_apply": 0, "email_missing": 0, "email_bad": 0}
    records: list[dict] = []

    with httpx.Client() as client:
        for job in iter_all_jobs(client, search=search, location=location):
            lane = classify(job)
            apply_email = None
            description = None

            if lane == "email":
                time.sleep(THROTTLE_SECONDS)
                detail = fetch_detail(client, job["id"])          # one hop -> email + description
                if detail:
                    raw = detail.get("apply_email")
                    apply_email = raw.strip() if isinstance(raw, str) and raw.strip() else None
                    description = detail.get("description")
                if not apply_email:
                    # Flagged email-apply but no address surfaced -> route to review, don't drop it.
                    counts["email_missing"] += 1
                    lane = "external_apply"
                elif verify_emails:
                    status = verify_email_via_searlo(apply_email)
                    if status not in ("valid", "accept_all"):
                        counts["email_bad"] += 1
                        lane = "external_apply"  # keep the job, just don't auto-send to a bad address

            counts[lane] = counts.get(lane, 0) + 1
            records.append(build_record(job, lane, apply_email, description))

    written = upsert_jobs(collection, records)
    summary = {"source": SOURCE_NAME, "fetched": len(records), **written, "lanes": counts}
    logger.info("Ingest complete: %s", summary)
    return summary


if __name__ == "__main__":
    # Standalone smoke test. Prints classification without touching Mongo.
    logging.basicConfig(level=logging.INFO)
    with httpx.Client() as _c:
        seen = {"email": 0, "linkedin_manual": 0, "external_apply": 0}
        for _job in iter_all_jobs(_c):
            _lane = classify(_job)
            seen[_lane] = seen.get(_lane, 0) + 1
            if _lane == "email":
                _addr = fetch_apply_email(_c, _job["id"])
                print(f"[email]  {_job['title']} @ {_job['company']} -> {_addr}")
                time.sleep(THROTTLE_SECONDS)
        print("Lane totals:", seen)