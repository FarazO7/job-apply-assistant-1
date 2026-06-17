# Job Apply Assistant (backend)

Turns hiring posts into ready-to-send application emails, queued for you to review and send
with one request. Two ways jobs come in:

1. **Alert emails** — you save searches on LinkedIn / Naukri / Indeed; they email you new
   matches. A worker reads those alert emails from your inbox every 2 hours and queues the
   listings.
2. **Paste / forward a post** — hand it a CUPI-style "email me your resume at X" post (as text,
   or a public URL) and it drafts the email and extracts the recipient.

Nothing is scraped. Sourcing is your own inbox plus posts you paste.

---

## What you need first

- **Python 3.11+**
- **MongoDB** connection string — [Atlas](https://www.mongodb.com/cloud/atlas) free tier is fine
- **OpenAI API key** — https://platform.openai.com/api-keys
- **Gmail app password** (not your normal password):
  1. Turn on 2-Step Verification: https://myaccount.google.com/security
  2. Create an app password: https://myaccount.google.com/apppasswords
  3. Use that 16-character value as `GMAIL_APP_PASSWORD`
  - Note: Workspace admins can disable app passwords / IMAP. If yours is disabled, switch the
    Gmail calls to OAuth — same flow, more setup.
- A **resume PDF** on disk (its path goes in `RESUME_PATH`)

## Setup

```bash
cd job-apply-assistant
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then fill in every value
python run.py                 # serves http://127.0.0.1:8000  (docs at /docs)
```

Set your profile once so emails are written in your voice:

```bash
curl -X PUT http://127.0.0.1:8000/profile -H "Content-Type: application/json" -d '{
  "name": "Faraz Ali", "phone": "7991193433", "email": "you@gmail.com",
  "years": "~2 years", "domains": "edtech, fintech",
  "summary": "Product experience across edtech and fintech: growth systems, workflow automation, product analytics, end-to-end ownership. Improved conversion 32%, built automated workflows."
}'
```

## Using it

```bash
# Paste a hiring post (or pass a public "url" instead of "text")
curl -X POST http://127.0.0.1:8000/applications/from-post \
  -H "Content-Type: application/json" \
  -d '{"text": "I am hiring for ... send your resume to sahaj.n@getcupi.com"}'

# See the queue
curl http://127.0.0.1:8000/applications

# Send one (attaches your resume, sends from your Gmail)
curl -X POST http://127.0.0.1:8000/applications/<id>/send

# Force an alert read now (otherwise it runs every 2 hours)
curl -X POST http://127.0.0.1:8000/ingest/alerts
```

If it needs a detail it doesn't have (e.g. expected CTC), the application comes back with
`status: "needs_info"` and a `missing_info` list. Answer once and it re-drafts and remembers
the answer for every future job:

```bash
curl -X POST http://127.0.0.1:8000/applications/<id>/answers \
  -H "Content-Type: application/json" \
  -d '{"answers": {"expected_ctc": "18 LPA", "notice_period": "Immediate"}}'
```

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/applications?status=` | List the queue (filter by status) |
| POST | `/applications/from-post` | Add one post (`text` or `url`); returns `needs_paste` if a link is gated |
| PATCH | `/applications/{id}` | Edit recipient / subject / body before sending |
| POST | `/applications/{id}/answers` | Save reusable answers + re-draft |
| POST | `/applications/{id}/send` | Send with resume attached (idempotent) |
| POST | `/applications/{id}/skip` | Drop one from the queue |
| GET/PUT | `/profile` | Your profile used to tailor every email |
| POST | `/ingest/alerts` | Run the alert read manually |

## Status values

- `drafted` — has a recipient and a finished email; ready to send.
- `needs_info` — has a recipient but the draft wants a detail; answer it, then send.
- `apply_on_site` — no email address in the listing; apply via its link instead.
- `sent` / `skipped`.

## Honest notes

- **Most alert-sourced jobs land as `apply_on_site`.** Job-board listings expose an Apply
  button, not an email, so the one-click *send* mainly fires on the hiring posts you paste or
  forward (the ones that say "mail your resume to X"). That is expected, not a bug.
- **Link fetch is best-effort, single attempt.** A public post may parse; a gated one returns
  `needs_paste` and you paste the text. It does not retry, spoof a browser, or work around
  blocks — that is the line that keeps it off LinkedIn's prohibited automated-access path.
- **Dedupe + idempotent send.** A unique index on `dedupe_key` means the same posting is never
  queued twice, and `/send` refuses to re-send anything already `sent`.
- **No auth, localhost only.** This binds to `127.0.0.1`. Don't expose it to the internet
  without putting authentication in front of it — it can send email as you.

## Frontend

This is the API. A review-queue UI calls these endpoints: list `drafted` applications, show
each email, and a Send button per row. The earlier in-browser tool can be repointed at this
API instead of generating emails client-side.
