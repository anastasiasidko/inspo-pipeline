"""
The app's own Gmail access — separate from any Gmail connection available to
Claude in chat. That one only works interactively, here; this one is what the
deployed app itself uses when a person clicks Subscribe, potentially long
after any chat session ends.

Auth is a standard installed-app OAuth flow: the FIRST time this runs, it opens
a browser tab asking the person to sign in and click Allow (that click has to
be a human, it needs their password). After that, a refresh token is cached
locally (GMAIL_TOKEN_FILE) and no further prompts happen.

Scopes: gmail.modify (read messages, create/apply labels) + gmail.settings.basic
(create filters). Nothing here ever runs except in response to a specific user
action (see pipeline/newsletter_routing.py) — no polling loop, no standing job.
"""

import base64
import logging
import os
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify",
          "https://www.googleapis.com/auth/gmail.settings.basic"]
TOKEN_FILE = Path(os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json"))


class GmailNotConfigured(Exception):
    """Raised when GOOGLE_GMAIL_OAUTH_CLIENT isn't set yet — the setup steps haven't been done."""


def get_service():
    """Returns an authorized Gmail API client, or raises GmailNotConfigured."""
    client_secret = os.environ.get("GOOGLE_GMAIL_OAUTH_CLIENT")
    if not client_secret:
        raise GmailNotConfigured(
            "GOOGLE_GMAIL_OAUTH_CLIENT is not set — the app's own Gmail login hasn't been set up yet."
        )
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
            creds = flow.run_local_server(port=0)  # opens the one-time browser consent tab
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------- labels
def ensure_label(service, name: str) -> str:
    """Idempotent — like init_db, creates the label only if it doesn't already exist. -> label id."""
    existing = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in existing:
        if label["name"] == name:
            return label["id"]
    created = service.users().labels().create(userId="me", body={
        "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show",
    }).execute()
    logger.info(f"Created Gmail label: {name}")
    return created["id"]


# ---------------------------------------------------------------- finding the first real email
def find_sender_since(service, domain_hint: str, after_ts: int) -> Optional[str]:
    """
    Look for the first inbox message received after `after_ts` (unix seconds) whose
    From address's domain contains `domain_hint` (e.g. "beehiiv.com" or the
    newsletter's own domain). Returns the exact From address, or None if nothing
    has arrived yet — that's the normal, expected case right after subscribing.
    """
    query = f"after:{after_ts}"
    resp = service.users().messages().list(userId="me", q=query, maxResults=25).execute()
    for item in resp.get("messages", []):
        msg = service.users().messages().get(userId="me", id=item["id"], format="metadata",
                                              metadataHeaders=["From"]).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        _, addr = parseaddr(headers.get("From", ""))
        if addr and domain_hint.lower() in addr.lower():
            return addr
    return None


# ---------------------------------------------------------------- filters
def ensure_filter(service, from_address: str, label_id: str) -> str:
    """
    Idempotent: a filter for this exact From address + label already existing is
    reused rather than duplicated. -> filter id. Skips the inbox (archives) by
    removing the INBOX label on matching mail.
    """
    existing = service.users().settings().filters().list(userId="me").execute().get("filter", [])
    for f in existing:
        if f.get("criteria", {}).get("from") == from_address and label_id in f.get("action", {}).get("addLabelIds", []):
            return f["id"]
    created = service.users().settings().filters().create(userId="me", body={
        "criteria": {"from": from_address},
        "action": {"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
    }).execute()
    logger.info(f"Created Gmail filter: {from_address} -> label {label_id}, skip inbox")
    return created["id"]


def apply_label_to_existing(service, from_address: str, label_id: str) -> int:
    """Retroactively move mail already delivered before the filter existed. -> count moved."""
    resp = service.users().messages().list(userId="me", q=f"from:{from_address} in:inbox").execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    if ids:
        service.users().messages().batchModify(userId="me", body={
            "ids": ids, "addLabelIds": [label_id], "removeLabelIds": ["INBOX"],
        }).execute()
    return len(ids)
