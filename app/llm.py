import json

from openai import OpenAI

from .config import settings

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.openai_api_key)
    return _client


def _ask(prompt, max_tokens=1500):
    resp = _get_client().chat.completions.create(
        model=settings.llm_model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return (resp.choices[0].message.content or "").strip()


def _parse_json(text):
    cleaned = text.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)


# Distilled from the "Signs of AI writing" patterns the humanizer skill targets.
_HUMANIZE = (
    "Write so it does not read as AI-generated:\n"
    "- Plain words. Avoid testament, landscape, showcase, leverage, delve, robust, pivotal, "
    "seamless, vibrant, underscore, foster, realm, tapestry, navigate (figurative).\n"
    "- Simple verbs: use is/has, not serves as/boasts/features.\n"
    "- No \"not just X, it's Y\" and no \"not only ... but also\". State the point plainly.\n"
    "- Avoid three-item lists used for rhythm; use a natural number of items.\n"
    "- No em dashes for effect (use commas or periods), no emojis, no bold, sentence case.\n"
    "- Cut filler (in order to -> to; due to the fact that -> because) and stacked hedging.\n"
    "- No signposting openers (I am excited to, I hope this helps, Let me know). Start with substance.\n"
    "- No grand closers (the future looks bright). End on something concrete.\n"
    "- Vary sentence length and sound like a specific person, not a template."
)


def _profile_block(profile, answers):
    keys = ("name", "phone", "email", "years", "domains", "summary", "skills", "titles", "education")
    safe = {k: profile.get(k, "") for k in keys}
    block = (
        "CANDIDATE PROFILE (JSON):\n"
        + json.dumps(safe, ensure_ascii=False, indent=2)
        + "\n\nSAVED ANSWERS - reuse these, never ask for anything already here (JSON):\n"
        + json.dumps(answers or {}, ensure_ascii=False, indent=2)
    )
    resume = (profile.get("resume_text") or "").strip()
    if resume:
        block += "\n\nFULL RESUME TEXT (pull only the points relevant to this role):\n###\n" + resume[:6000] + "\n###"
    return block


_EMAIL_RULES = (
    "Write subject + body for an application email: first person as the candidate, warm but plain, "
    "110-160 words. Name the exact role and company, use 2-3 specific points from the resume that match "
    "the post, and sign off with the candidate's name and phone on separate lines. No [placeholders]. "
    "If saved answers mark the candidate an immediate joiner, you may note availability.\n"
    "missing_info = facts that would materially strengthen the email but are absent from profile and "
    "saved answers (e.g. expected_ctc, notice_period). Empty list if none.\n\n" + _HUMANIZE
)


def parse_and_write_post(text, profile, answers):
    prompt = (
        "You turn a pasted hiring post into a ready-to-send job application email.\n\n"
        + _profile_block(profile, answers)
        + "\n\nHIRING POST:\n###\n" + text + "\n###\n\n"
        "Do all of this:\n"
        "1. Extract the recipient email address candidates should send applications to. If none, use \"\".\n"
        "2. Extract company name and every role listed with its experience range.\n"
        "3. Choose the single role that best fits the candidate; put it in chosen_role, the rest in other_roles.\n"
        "4. " + _EMAIL_RULES + "\n\n"
        "Return ONLY JSON, no markdown:\n"
        '{"recipient_email":"","company":"","roles":[{"title":"","experience":""}],'
        '"chosen_role":"","other_roles":[],"subject":"","body":"","missing_info":[{"key":"","label":""}]}'
    )
    return _parse_json(_ask(prompt))


def parse_alert(text):
    prompt = (
        "Extract every job listing from this job-alert email. For each give title, company, location, "
        "url (the listing/apply link if present), posted (any 'X days ago' or date text), and recipient_email "
        "ONLY if the email body itself contains a contact address to apply to.\n\n"
        "ALERT EMAIL:\n###\n" + text + "\n###\n\n"
        "Return ONLY JSON, no markdown:\n"
        '{"listings":[{"title":"","company":"","location":"","url":"","posted":"","recipient_email":""}]}'
    )
    data = _parse_json(_ask(prompt, max_tokens=2000))
    return data.get("listings", []) if isinstance(data, dict) else []


def write_email_for_listing(listing, profile, answers):
    prompt = (
        "Write a job application email for this listing.\n\n"
        + _profile_block(profile, answers)
        + "\n\nLISTING (JSON):\n" + json.dumps(listing, ensure_ascii=False, indent=2)
        + "\n\n" + _EMAIL_RULES + "\n\n"
        "Return ONLY JSON, no markdown:\n"
        '{"subject":"","body":"","missing_info":[{"key":"","label":""}]}'
    )
    return _parse_json(_ask(prompt))


def parse_resume(text):
    prompt = (
        "Extract a candidate profile from this resume. Pull everything present; leave a field \"\" if absent. "
        "summary = 2-3 plain sentences. skills = comma-separated. titles = roles held, comma-separated. "
        "education = degrees and schools, comma-separated. years = total years of experience.\n\n"
        "RESUME:\n###\n" + (text or "")[:9000] + "\n###\n\n"
        "Return ONLY JSON, no markdown:\n"
        '{"name":"","phone":"","email":"","years":"","domains":"","summary":"","skills":"","titles":"","education":""}'
    )
    return _parse_json(_ask(prompt))


def draft_application_answers(context_text, profile, answers):
    prompt = (
        "A job uses a web form or apply link (no email). Draft short, ready-to-paste answers to the questions "
        "such a form usually asks, tailored to the post using the candidate's resume.\n\n"
        + _profile_block(profile, answers)
        + "\n\nPOST / LISTING:\n###\n" + (context_text or "")[:4000] + "\n###\n\n"
        "Cover at least: why this role and company, most relevant experience, notice period, expected CTC, "
        "current location, plus anything the post explicitly asks. Use saved answers where available.\n"
        + _HUMANIZE + "\n\n"
        "Return ONLY JSON, no markdown:\n"
        '{"answers":[{"question":"","answer":""}],"missing_info":[{"key":"","label":""}]}'
    )
    return _parse_json(_ask(prompt, max_tokens=2000))


def map_form_fields(fields, profile, answers):
    prompt = (
        "Map the candidate's data to these Google Form fields. Only fill text fields you are confident about "
        "(name, email, phone, experience, location, short why-this-role, etc.). Skip anything uncertain.\n\n"
        + _profile_block(profile, answers)
        + "\n\nFORM FIELDS (JSON list of {title, entry_id}):\n" + json.dumps(fields, ensure_ascii=False)
        + "\n\nReturn ONLY JSON mapping entry_id (string) to value, no markdown:\n"
        '{"values":{"123456":"value"}}'
    )
    data = _parse_json(_ask(prompt))
    return data.get("values", {}) if isinstance(data, dict) else {}
