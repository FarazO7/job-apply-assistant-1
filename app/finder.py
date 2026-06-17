"""LinkedIn-post finder + send agent.

Sourcing is legitimate: we query a search provider's index (Searlo, which returns
Google results) for PUBLIC LinkedIn hiring posts. We never scrape LinkedIn itself.

For each candidate post we draft an email, then run a verification gate. Only posts
that clear the gate are auto-sent (during IST business hours) or scheduled (off-hours).
Everything else lands in the review queue for the user to handle manually.
"""

import datetime as dt

import httpx

from . import gmail_client, ingest, llm
from .config import csv, settings
from .db import applications, profiles
from .models import dedupe_key, now

# IST timezone, with a fixed-offset fallback if the tz database is unavailable.
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------- timing
def ist_now():
    return now().astimezone(IST)


def in_send_window(ist=None):
    ist = ist or ist_now()
    return settings.agent_send_window_start <= ist.hour < settings.agent_send_window_end


def next_morning_utc(ist=None):
    """The next agent_morning_hour (10:00) in IST, returned as a UTC datetime."""
    ist = ist or ist_now()
    target = ist.replace(hour=settings.agent_morning_hour, minute=0, second=0, microsecond=0)
    if ist >= target:                       # already past 10:00 today -> tomorrow
        target = target + dt.timedelta(days=1)
    return target.astimezone(dt.timezone.utc)


# ---------------------------------------------------------------- email verification
def verify_email(addr):
    """True only if the address is well-formed AND its domain accepts mail (MX)."""
    if not addr or "@" not in addr:
        return False
    try:
        from email_validator import validate_email
        validate_email(addr.strip(), check_deliverability=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- daily usage / caps
def _today_str(ist=None):
    return (ist or ist_now()).strftime("%Y-%m-%d")


def _usage():
    """Return today's usage counters, resetting them when the IST date rolls over."""
    prof = ingest.get_profile()
    today = _today_str()
    if prof.get("agent_usage_date") != today:
        profiles.update_one(
            {"_id": "me"},
            {"$set": {"agent_usage_date": today, "agent_credits_used": 0, "agent_actions_done": 0}},
        )
        return {"date": today, "credits": 0, "actions": 0}
    return {
        "date": today,
        "credits": int(prof.get("agent_credits_used", 0) or 0),
        "actions": int(prof.get("agent_actions_done", 0) or 0),
    }


def _add_credits(n):
    _usage()  # ensures the day is current first
    profiles.update_one({"_id": "me"}, {"$inc": {"agent_credits_used": n}})


def _add_action():
    _usage()
    profiles.update_one({"_id": "me"}, {"$inc": {"agent_actions_done": 1}})


# ---------------------------------------------------------------- search provider
def _searlo_search(query):
    """One Searlo advanced-search call (1 credit). Returns (items, credits_deducted)."""
    url = settings.searlo_base_url.rstrip("/") + "/search/advanced"
    params = {
        "q": query,
        "limit": settings.search_results_per_query,
        "dateRange": settings.search_date_range,
    }
    headers = {"x-api-key": settings.searlo_api_key}
    with httpx.Client(timeout=20) as client:
        r = client.get(url, params=params, headers=headers)
    deducted = 1
    try:
        deducted = float(r.headers.get("X-Credits-Deducted", 1)) or 1
    except Exception:
        deducted = 1
    if r.status_code != 200:
        raise RuntimeError(f"searlo {r.status_code}: {r.text[:200]}")
    data = r.json()
    items = data.get("items", []) if isinstance(data, dict) else []
    return items, deducted


def search(query):
    if settings.search_provider == "searlo":
        return _searlo_search(query)
    raise RuntimeError(f"unknown search provider: {settings.search_provider}")


def _build_query(seed):
    """Wrap a role/location seed into a LinkedIn-post hiring query."""
    site = settings.search_site
    return f'{seed} ("hiring" OR "we are hiring" OR "we\'re hiring") site:{site}'


def _seed_queries(prof):
    qs = [q for q in (prof.get("search_queries") or []) if str(q).strip()]
    if qs:
        return qs
    return csv(settings.job_search_queries)


# ---------------------------------------------------------------- the finder
def run_finder(manual=False):
    """Find LinkedIn hiring posts, draft, verify, then auto-send / schedule / queue.

    manual=True is the user pressing 'Find now' (always runs).
    Auto sending/scheduling only happens when the agent is enabled; otherwise eligible
    drafts are placed in the review queue (status 'drafted') for one-click manual send.
    """
    prof = ingest.get_profile()
    answers = prof.get("answers", {})
    agent_on = bool(prof.get("agent_enabled"))
    seeds = _seed_queries(prof)

    summary = {
        "queries_run": 0, "credits_used": 0, "results_seen": 0, "new": 0,
        "auto_sent": 0, "scheduled": 0, "review": 0, "skipped_known": 0,
        "errors": [], "agent_enabled": agent_on,
    }
    if not seeds:
        summary["errors"].append("no search queries configured")
        return summary
    if not settings.searlo_api_key:
        summary["errors"].append("no search API key configured")
        return summary

    for seed in seeds:
        if _usage()["credits"] >= settings.searlo_daily_credit_cap:
            summary["errors"].append("daily credit cap reached")
            break
        try:
            items, deducted = search(_build_query(seed))
        except Exception as exc:
            summary["errors"].append(str(exc))
            continue
        _add_credits(deducted)
        summary["queries_run"] += 1
        summary["credits_used"] += deducted

        for item in items:
            link = (item.get("link") or "").strip()
            if not link or "/posts/" not in link:        # only real feed posts
                continue
            summary["results_seen"] += 1
            u = ingest._norm_url(link)
            if applications.find_one({"source_url_norm": u}):
                summary["skipped_known"] += 1
                continue
            text = (item.get("title", "") + "\n" + item.get("snippet", "")).strip()
            try:
                data = llm.parse_and_write_post(text, prof, answers)
            except Exception as exc:
                summary["errors"].append(f"draft failed: {exc}")
                continue

            recipients = [e for e in (data.get("recipient_emails") or []) if e and e.strip()]
            company = data.get("company", "")
            role = data.get("chosen_role", "")
            body = data.get("body", "")
            cc = ingest._join(data.get("cc_emails", []))
            bcc = ingest._join(data.get("bcc_emails", []))
            missing = data.get("missing_info", []) or []

            # ---- verification gate ----
            eligible = (
                bool(data.get("is_hiring", True))
                and bool(data.get("email_explicit"))
                and len(recipients) == 1
                and bool(body)
                and verify_email(recipients[0])
            )

            # Skip pure noise: not hiring and no address at all.
            if not eligible and not data.get("is_hiring", True) and not recipients:
                continue

            to = recipients[0] if len(recipients) == 1 else ingest._join(recipients)
            doc = {
                "source": "finder", "search_seed": seed,
                "dedupe_key": dedupe_key(company, role, u),
                "company": company, "role": role, "all_roles": [], "other_roles": [],
                "recipient_email": to, "cc_emails": cc, "bcc_emails": bcc,
                "subject": ingest.subject_for(role) if to else "", "body": body,
                "missing_info": missing,
                "source_url": link, "source_url_norm": u, "raw_text": text[:5000],
                "prepared_answers": [], "prefill_url": "", "sent_at": None,
                "needs_review": (not eligible), "review_reason": "",
            }

            if not eligible:
                doc["status"] = ingest._status_for(to, missing)
                doc["review_reason"] = _why_not(data, recipients, body)
                ingest._upsert(doc)
                summary["new"] += 1
                summary["review"] += 1
                continue

            # eligible: auto-send (in window) or schedule (off hours) IF agent is on
            if not agent_on:
                doc["status"] = "drafted"           # high-confidence draft, manual send
                ingest._upsert(doc)
                summary["new"] += 1
                summary["review"] += 1
                continue

            if _usage()["actions"] >= settings.agent_max_actions_per_day:
                doc["status"] = "drafted"
                doc["needs_review"] = True
                doc["review_reason"] = "daily auto-send cap reached"
                ingest._upsert(doc)
                summary["new"] += 1
                summary["review"] += 1
                continue

            if in_send_window():
                try:
                    gmail_client.send_email(
                        to=to, subject=doc["subject"], body=body,
                        resume_path=ingest.effective_resume_path(), cc=cc, bcc=bcc,
                    )
                    doc["status"] = "sent"
                    doc["sent_at"] = now()
                    ingest._upsert(doc)
                    _add_action()
                    summary["new"] += 1
                    summary["auto_sent"] += 1
                except Exception as exc:
                    doc["status"] = "drafted"
                    doc["needs_review"] = True
                    doc["review_reason"] = f"auto-send failed: {exc}"
                    ingest._upsert(doc)
                    summary["new"] += 1
                    summary["review"] += 1
            else:
                doc["status"] = "scheduled"
                doc["scheduled_for"] = next_morning_utc()
                ingest._upsert(doc)
                _add_action()
                summary["new"] += 1
                summary["scheduled"] += 1

    return summary


def _why_not(data, recipients, body):
    if not data.get("is_hiring", True):
        return "not clearly a hiring post"
    if not recipients:
        return "no apply-by-email address found"
    if not data.get("email_explicit"):
        return "email looked incomplete or obfuscated"
    if len(recipients) > 1:
        return "multiple recipient addresses — review which to use"
    if not body:
        return "could not draft an email"
    return "email failed verification (domain/MX)"


# ---------------------------------------------------------------- scheduled scan
def maybe_scan():
    """Called on an interval. Runs the finder once per configured IST slot per day."""
    prof = ingest.get_profile()
    if not prof.get("agent_enabled"):
        return
    ist = ist_now()
    slots = [int(h) for h in csv(settings.agent_scan_hours) if h.strip().isdigit()]
    if ist.hour not in slots:
        return
    slot_key = f"{_today_str(ist)}:{ist.hour}"
    if prof.get("agent_last_slot") == slot_key:
        return
    profiles.update_one({"_id": "me"}, {"$set": {"agent_last_slot": slot_key}})
    try:
        run_finder(manual=False)
    except Exception as exc:
        print("[finder] scan failed:", exc)


# ---------------------------------------------------------------- agent state (UI)
def get_state():
    prof = ingest.get_profile()
    u = _usage()
    return {
        "enabled": bool(prof.get("agent_enabled")),
        "search_queries": prof.get("search_queries") or [],
        "credits_used_today": u["credits"],
        "credit_cap": settings.searlo_daily_credit_cap,
        "actions_done_today": u["actions"],
        "action_cap": settings.agent_max_actions_per_day,
        "provider": settings.search_provider,
        "has_key": bool(settings.searlo_api_key),
        "scan_hours": csv(settings.agent_scan_hours),
        "send_window": [settings.agent_send_window_start, settings.agent_send_window_end],
    }


def set_state(enabled=None, search_queries=None):
    updates = {}
    if enabled is not None:
        updates["agent_enabled"] = bool(enabled)
    if search_queries is not None:
        updates["search_queries"] = [str(q).strip() for q in search_queries if str(q).strip()]
    if updates:
        profiles.update_one({"_id": "me"}, {"$set": updates}, upsert=True)
    return get_state()