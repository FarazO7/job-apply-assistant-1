from typing import Optional
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import gmail_client, ingest
from .config import settings
from .db import applications, ensure_indexes, profiles
from .models import DEFAULT_PROFILE, now, serialize

scheduler = BackgroundScheduler()


def _scheduled_crawl():
    try:
        ingest.ingest_alerts()
    except Exception as exc:  # never let a bad cycle kill the scheduler
        print(f"[scheduler] alert crawl failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    if not profiles.find_one({"_id": "me"}):
        profiles.insert_one(dict(DEFAULT_PROFILE))
    scheduler.add_job(
        _scheduled_crawl,
        "interval",
        hours=settings.crawl_interval_hours,
        id="alert_crawl",
        replace_existing=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Job Apply Assistant", lifespan=lifespan)


# ---- request bodies ----
class PostIn(BaseModel):
    text: Optional[str] = None
    url: Optional[str] = None


class ProfileIn(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    years: Optional[str] = None
    domains: Optional[str] = None
    summary: Optional[str] = None
    skills: Optional[str] = None
    titles: Optional[str] = None
    education: Optional[str] = None


class AnswersIn(BaseModel):
    answers: dict


class EmailEdit(BaseModel):
    recipient_email: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None


def _oid(app_id: str) -> ObjectId:
    try:
        return ObjectId(app_id)
    except Exception:
        raise HTTPException(status_code=400, detail="bad id")


# ---- queue ----
@app.get("/applications")
def list_applications(status: Optional[str] = None):
    query = {"status": status} if status else {}
    docs = applications.find(query).sort("created_at", -1)
    return [serialize(d) for d in docs]


@app.get("/applications/{app_id}")
def get_application(app_id: str):
    doc = applications.find_one({"_id": _oid(app_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    return serialize(doc)


@app.post("/applications/from-post")
def add_from_post(body: PostIn):
    result = ingest.ingest_post(text=body.text, url=body.url)
    if result.get("needs_paste"):
        # link was gated/blocked — UI should ask the user to paste the post text
        return {"needs_paste": True, "url": result["url"]}
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return serialize(result)


@app.patch("/applications/{app_id}")
def edit_application(app_id: str, body: EmailEdit):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="nothing to update")
    updates["updated_at"] = now()
    applications.update_one({"_id": _oid(app_id)}, {"$set": updates})
    return serialize(applications.find_one({"_id": _oid(app_id)}))


@app.post("/applications/{app_id}/answers")
def answer_application(app_id: str, body: AnswersIn):
    # save reusable answers on the profile, then re-draft this application's email
    profiles.update_one(
        {"_id": "me"},
        {"$set": {f"answers.{k}": v for k, v in body.answers.items()}},
        upsert=True,
    )
    doc = applications.find_one({"_id": _oid(app_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    return serialize(ingest.regenerate(doc))


@app.post("/applications/{app_id}/send")
def send_application(app_id: str):
    doc = applications.find_one({"_id": _oid(app_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    if doc.get("status") == "sent":
        raise HTTPException(status_code=409, detail="already sent")
    if not doc.get("recipient_email"):
        raise HTTPException(status_code=400, detail="no recipient email to send to")
    if not (doc.get("subject") and doc.get("body")):
        raise HTTPException(status_code=400, detail="email not drafted yet")

    gmail_client.send_email(
        to=doc["recipient_email"],
        subject=doc["subject"],
        body=doc["body"],
        resume_path=ingest.effective_resume_path(),
    )
    applications.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": "sent", "sent_at": now(), "updated_at": now()}},
    )
    return serialize(applications.find_one({"_id": doc["_id"]}))


@app.post("/applications/{app_id}/skip")
def skip_application(app_id: str):
    applications.update_one(
        {"_id": _oid(app_id)}, {"$set": {"status": "skipped", "updated_at": now()}}
    )
    return {"ok": True}


# ---- profile ----
@app.get("/profile")
def get_profile():
    return serialize(ingest.get_profile())


@app.put("/profile")
def update_profile(body: ProfileIn):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    profiles.update_one({"_id": "me"}, {"$set": updates}, upsert=True)
    return serialize(profiles.find_one({"_id": "me"}))


# ---- manual alert crawl (also runs on the schedule) ----
@app.post("/ingest/alerts")
def run_alert_ingest():
    return ingest.ingest_alerts()


@app.get("/health")
def health():
    return {"ok": True, "crawl_interval_hours": settings.crawl_interval_hours}


from fastapi.responses import HTMLResponse as _HTMLResponse
from pathlib import Path as _Path

@app.get("/", response_class=_HTMLResponse)
def _index():
    return _Path(__file__).resolve().parent.parent.joinpath("index.html").read_text()


# ---- scheduled send ----
import datetime as _dt


def _send_doc(doc):
    gmail_client.send_email(
        to=doc["recipient_email"],
        subject=doc.get("subject", ""),
        body=doc.get("body", ""),
        resume_path=ingest.effective_resume_path(),
    )
    applications.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": "sent", "sent_at": now(), "updated_at": now()}},
    )


def _send_due_scheduled():
    try:
        for doc in list(applications.find({"status": "scheduled", "scheduled_for": {"$lte": now()}})):
            if not doc.get("recipient_email") or not (doc.get("subject") and doc.get("body")):
                continue
            try:
                _send_doc(doc)
            except Exception as exc:
                print("[scheduler] scheduled send failed:", exc)
    except Exception as exc:
        print("[scheduler] sweep failed:", exc)


class ScheduleIn(BaseModel):
    scheduled_for: str


@app.post("/applications/{app_id}/schedule")
def schedule_application(app_id: str, body: ScheduleIn):
    doc = applications.find_one({"_id": _oid(app_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    if doc.get("status") == "sent":
        raise HTTPException(status_code=409, detail="already sent")
    if not doc.get("recipient_email"):
        raise HTTPException(status_code=400, detail="no recipient email")
    if not (doc.get("subject") and doc.get("body")):
        raise HTTPException(status_code=400, detail="email not drafted yet")
    try:
        when = _dt.datetime.fromisoformat(body.scheduled_for.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        raise HTTPException(status_code=400, detail="bad datetime")
    applications.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": "scheduled", "scheduled_for": when, "updated_at": now()}},
    )
    return serialize(applications.find_one({"_id": doc["_id"]}))


@app.post("/applications/{app_id}/unschedule")
def unschedule_application(app_id: str):
    applications.update_one(
        {"_id": _oid(app_id)},
        {"$set": {"status": "drafted", "scheduled_for": None, "updated_at": now()}},
    )
    return serialize(applications.find_one({"_id": _oid(app_id)}))


scheduler.add_job(_send_due_scheduled, "interval", seconds=60, id="send_due_scheduled", replace_existing=True)


from fastapi import File, UploadFile
from . import llm, fetcher


@app.post("/profile/resume")
def upload_resume(file: UploadFile = File(...)):
    data = file.file.read()
    prof = ingest.save_resume(data, file.filename)
    return serialize(prof)


@app.post("/applications/{app_id}/prepare")
def prepare_application(app_id: str):
    doc = applications.find_one({"_id": _oid(app_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    profile = ingest.get_profile()
    answers = profile.get("answers", {})
    context = doc.get("raw_text") or (doc.get("company", "") + " " + doc.get("role", ""))
    try:
        prepared = llm.draft_application_answers(context, profile, answers).get("answers", [])
    except Exception:
        prepared = []
    prefill_url = ""
    url = doc.get("source_url", "")
    if url and fetcher.is_google_form(url):
        try:
            form_url, fields = fetcher.google_form_fields(url)
            if fields:
                values = llm.map_form_fields(fields, profile, answers)
                prefill_url = fetcher.build_prefill_url(form_url, values)
        except Exception:
            prefill_url = ""
    applications.update_one({"_id": doc["_id"]}, {"$set": {
        "prepared_answers": prepared, "prefill_url": prefill_url, "updated_at": now()}})
    return serialize(applications.find_one({"_id": doc["_id"]}))
