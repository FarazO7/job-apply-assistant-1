import json
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_LOGIN_MARKERS = ["sign in", "join now", "authwall", "please log in", "log in to continue", "create your account"]
_LOGIN_PATHS = ["/login", "/authwall", "/uas/login", "/signup", "/checkpoint"]


def _raw_fetch(url):
    try:
        resp = httpx.get(url, headers={"User-Agent": _UA, "Accept-Language": "en"},
                         follow_redirects=True, timeout=12.0)
    except Exception:
        return "", ""
    return str(resp.url), resp.text


def best_effort_fetch(url):
    # One plain request, no retries, no bot-detection evasion. Falls back to paste on any block.
    final_url, html = _raw_fetch(url)
    if not html:
        return "needs_paste", ""
    if any(p in final_url.lower() for p in _LOGIN_PATHS):
        return "needs_paste", ""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    head = text[:4000].lower()
    if len(text) < 200 or any(m in head for m in _LOGIN_MARKERS):
        return "needs_paste", ""
    return "ok", text


def is_google_form(url):
    u = (url or "").lower()
    return "docs.google.com/forms" in u or "forms.gle" in u


def google_form_fields(url):
    """Best-effort. Returns (viewform_url, [{title, entry_id}]); empty list if it can't be parsed."""
    final_url, html = _raw_fetch(url)
    if not html:
        return url, []
    m = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\])\s*;", html, re.DOTALL)
    if not m:
        return final_url, []
    try:
        data = json.loads(m.group(1))
        fields = []
        for item in data[1][1]:
            title = item[1] if len(item) > 1 else ""
            subs = item[4] if len(item) > 4 else None
            if subs and subs[0] and subs[0][0] is not None:
                fields.append({"title": title or "", "entry_id": str(subs[0][0])})
        return final_url, fields
    except Exception:
        return final_url, []


def build_prefill_url(form_url, values):
    if not values:
        return form_url
    parts = urlsplit(form_url)
    path = parts.path
    if not path.endswith("viewform"):
        path = path.rstrip("/") + "/viewform"
    q = [("usp", "pp_url")]
    for entry_id, val in values.items():
        q.append(("entry." + str(entry_id), val))
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(q), ""))
