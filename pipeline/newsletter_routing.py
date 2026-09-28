"""
Tracks "subscribed, waiting for the first real email" between the moment
someone clicks Subscribe and the moment the routing rule can actually be
created (see adapters/gmail_client.py for why that can't happen in one step).

State lives in a local file (newsletter_subscriptions.json), same pattern as
scrapers.json — small, no secrets, safe to commit. The Google Sheet stays the
pipeline's real data; this is just bookkeeping for one UI feature.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse

from adapters.gmail_client import GmailNotConfigured, apply_label_to_existing, ensure_filter, ensure_label, find_sender_since, get_service

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent / "newsletter_subscriptions.json"
LABEL_NAME = "Competitors"


def _load() -> List[Dict]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(rows: List[Dict]) -> None:
    STATE_FILE.write_text(json.dumps(rows, indent=2, ensure_ascii=False))


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def record_subscription(competitor: str, signup_url: str) -> None:
    """Called right after a successful Subscribe click. Marks it pending routing."""
    rows = _load()
    rows.append({
        "competitor": competitor, "signup_url": signup_url, "domain_hint": domain_of(signup_url),
        "subscribed_at": datetime.now(timezone.utc).isoformat(), "status": "pending",
        "sender_address": None, "label_id": None,
    })
    _save(rows)


def pending_count() -> int:
    return sum(1 for r in _load() if r["status"] == "pending")


def try_finish_pending(label_name: str = LABEL_NAME) -> Dict:
    """
    For each pending subscription, check whether its first email has arrived yet.
    If so: ensure the folder exists, create the routing rule, move anything
    already delivered, and mark it done. Never raises for an unconfigured or
    still-quiet subscription — those just stay pending.

    -> {"routed": [...competitor names finished this call...],
        "still_pending": N, "not_configured": bool}
    """
    rows = _load()
    try:
        service = get_service()
    except GmailNotConfigured:
        return {"routed": [], "still_pending": pending_count(), "not_configured": True}

    label_id = None
    routed = []
    for row in rows:
        if row["status"] != "pending":
            continue
        after_ts = int(datetime.fromisoformat(row["subscribed_at"]).timestamp())
        sender = find_sender_since(service, row["domain_hint"], after_ts)
        if not sender:
            continue  # nothing arrived yet — normal, try again later
        if label_id is None:
            label_id = ensure_label(service, label_name)
        ensure_filter(service, sender, label_id)
        moved = apply_label_to_existing(service, sender, label_id)
        row.update(status="routed", sender_address=sender, label_id=label_id)
        routed.append(row["competitor"])
        logger.info(f"[{row['competitor']}] Routing finished: {sender} -> {label_name} "
                    f"(moved {moved} already-delivered message(s))")

    _save(rows)
    return {"routed": routed, "still_pending": pending_count(), "not_configured": False}
