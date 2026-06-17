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
    "- In the body, do not open with hype like \"I am excited to\" or \"I am thrilled\". Open with substance.\n"
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
    bg = (profile.get("background") or "").strip()
    if bg:
        block += ("\n\nKEY WINS / BACKGROUND (draw on the points relevant to the post; "
                  "use at most one specific achievement in an email):\n###\n" + bg[:4000] + "\n###")
    resume = (profile.get("resume_text") or "").strip()
    if resume:
        block += "\n\nFULL RESUME TEXT (pull only the points relevant to this role):\n###\n" + resume[:6000] + "\n###"
    return block


_EMAIL_RULES = (
    "Write subject + body for a SHORT application email, about 80-110 words total: a greeting line, "
    "two short paragraphs, then a sign-off.\n"
    "- Greeting: if the post names a contact person, use 'Dear <that name>,'. Otherwise use 'Dear Hiring Manager,'.\n"
    "- Body: first person as the candidate, plain and warm. Name the exact role and company. Include at most ONE "
    "specific achievement from the candidate's background or skills, and only if it matches something the post asks "
    "for; otherwise keep it general. No [placeholders].\n"
    "- Sign-off: a line 'Best regards,', then the candidate's name, then the phone number, each on its own line.\n"
    "- If saved answers mark the candidate an immediate joiner, you may note availability in one short clause.\n"
    "missing_info = facts that would materially strengthen the email but are absent from profile and saved answers "
    "(e.g. expected_ctc, notice_period). Empty list if none.\n\n"
    "Keep the greeting and the sign-off exactly as instructed. The rules below apply to the wording of the body, "
    "not to removing the greeting or sign-off:\n" + _HUMANIZE
)


def parse_and_write_post(text, profile, answers):
    prompt = (
        "You turn a pasted hiring post into a ready-to-send job application email.\n\n"
        + _profile_block(profile, answers)
        + "\n\nHIRING POST:\n###\n" + text + "\n###\n\n"
        "Do all of this:\n"
        "1. Extract EVERY email address the post says to send the application to into recipient_emails. "
        "Strip any 'mailto:' prefix and surrounding brackets — an address written as "
        "[name@co.com](mailto:name@co.com) is just name@co.com. If the post explicitly says to CC an "
        "address put it in cc_emails; if it says to BCC an address put it in bcc_emails. Empty lists if none.\n"
        "   Separately, set email_explicit true ONLY if a complete, unambiguous address was literally present. "
        "If one looks truncated/cut-off, obfuscated (e.g. 'name [at] co dot com'), or you had to guess it, still "
        "extract what you can into recipient_emails but set email_explicit false.\n"
        "   Set is_hiring true only if this is genuinely a job/hiring post.\n"
        "2. Extract company name and every role listed with its experience range.\n"
        "3. Choose the single role that best fits the candidate; put it in chosen_role, the rest in other_roles.\n"
        "4. " + _EMAIL_RULES + "\n\n"
        "Return ONLY JSON, no markdown:\n"
        '{"recipient_emails":[],"email_explicit":false,"is_hiring":true,"cc_emails":[],"bcc_emails":[],"company":"",'
        '"roles":[{"title":"","experience":""}],"chosen_role":"","other_roles":[],'
        '"subject":"","body":"","missing_info":[{"key":"","label":""}]}'
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
        "such a form usually asks, tailored to the post using the candidate's background and resume.\n\n"
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