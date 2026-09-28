"""
Submit a newsletter signup form, self-contained — no dependency on any of
Claude's own tools, so this runs from inside the app's own button click,
including once deployed. That click is the one moment code is allowed to act
(see the "Subscribe" button in app.py): nothing here ever runs on its own.

Handles plain HTML forms (ConvertKit/Kit classic embeds, many Mailchimp and
WordPress forms). A page whose signup box is entirely JavaScript-rendered, with
no static <form>, cannot be filled this way — it fails with a manual link
rather than guessing. A CAPTCHA-protected page is detected and never attempted
at all: bypassing bot-detection is a hard rule, not a permission question.
"""

import logging
import os
import re
from typing import Dict, Optional
from urllib.parse import urljoin

import certifi
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; InspoPipelineBot/1.0)"}
CAPTCHA_MARKERS = re.compile(r"recaptcha|hcaptcha|turnstile|cf-challenge|g-recaptcha", re.I)
SUCCESS_MARKERS = re.compile(r"thank|check your (email|inbox)|confirm your subscription|"
                              r"almost there|you'?re (in|subscribed)|success", re.I)
EMAIL_FIELD_HINTS = re.compile(r"email", re.I)


def _has_captcha(html: str) -> bool:
    return bool(CAPTCHA_MARKERS.search(html))


def _find_email_form(html: str, base_url: str) -> Optional[Dict]:
    """First <form> with a visible email-type/email-named input. -> {action, method, fields, email_field}."""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        email_field = None
        fields: Dict[str, str] = {}
        for inp in form.find_all(["input", "textarea"]):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "text").lower()
            if itype in ("submit", "button", "reset", "file", "image"):
                continue
            fields[name] = inp.get("value") or ""
            if email_field is None and (itype == "email" or EMAIL_FIELD_HINTS.search(name)):
                email_field = name
        if email_field:
            return {
                "action": urljoin(base_url, form.get("action") or base_url),
                "method": (form.get("method") or "GET").upper(),
                "fields": fields,
                "email_field": email_field,
            }
    return None


def submit_signup(url: str, email: str, timeout: int = 20) -> Dict:
    """
    -> {"status": "subscribed" | "pending_confirmation" | "captcha" | "no_form" | "failed",
        "message": str}  — always a message safe to show the user directly.
    "pending_confirmation" means the page indicated a confirmation email is coming; whether
    that gets auto-completed is a separate step (needs its own Gmail access for the app).
    """
    try:
        page = requests.get(url, headers=HEADERS, timeout=timeout, verify=certifi.where())
        page.raise_for_status()
    except Exception as e:
        return {"status": "failed", "message": f"Could not open the signup page: {e}"}

    if _has_captcha(page.text):
        return {"status": "captcha",
                "message": f"This page is protected against automated sign-ups. Please subscribe yourself: {url}"}

    form = _find_email_form(page.text, url)
    if not form:
        return {"status": "no_form",
                "message": f"Couldn't find a plain sign-up form on this page (it may be a JavaScript widget). "
                           f"Please subscribe yourself: {url}"}

    fields = dict(form["fields"])
    fields[form["email_field"]] = email
    try:
        if form["method"] == "POST":
            resp = requests.post(form["action"], data=fields, headers=HEADERS, timeout=timeout,
                                 verify=certifi.where(), allow_redirects=True)
        else:
            resp = requests.get(form["action"], params=fields, headers=HEADERS, timeout=timeout,
                                verify=certifi.where(), allow_redirects=True)
    except Exception as e:
        return {"status": "failed", "message": f"The signup form didn't go through: {e}"}

    if not resp.ok:
        return {"status": "failed",
                "message": f"The signup form returned an error (status {resp.status_code}). "
                           f"Please subscribe yourself: {url}"}
    if _has_captcha(resp.text):
        return {"status": "captcha",
                "message": f"The form asked for a CAPTCHA after submitting. Please subscribe yourself: {url}"}
    if re.search(r"confirm|check your (email|inbox)|verify", resp.text, re.I):
        return {"status": "pending_confirmation",
                "message": "Submitted. This newsletter needs a confirmation email click before it's active."}
    if SUCCESS_MARKERS.search(resp.text):
        return {"status": "subscribed", "message": "Subscribed."}
    return {"status": "subscribed", "message": "Submitted — the page didn't clearly confirm, so it's worth a glance."}
