"""
RSS Adapter — Fetch and normalize RSS feeds

Handles:
- Fetching RSS feeds via feedparser
- Extracting full article text if summary is truncated (using trafilatura)
- Normalizing output to {title, url, published_date, text, source_name, text_complete}

Design notes:
- Every network call is wrapped so one broken feed doesn't kill the whole run.
- If a feed entry's body is under ~800 chars, we assume it's a teaser and fetch
  the real article page instead. This is the #1 silent-failure mode in RSS
  pipelines — feeds that only give you a summary, not the full text.
- If full-text fetch fails, we keep the item anyway (better a short item than
  a missing one) and flag text_complete=False so downstream steps know.
"""

import logging
import hashlib
import os
from typing import List, Dict, Optional
from datetime import datetime, timezone

import certifi

# feedparser uses raw urllib under the hood, which reads its trusted-certificate
# bundle from SSL_CERT_FILE / the system default — NOT from certifi, unlike
# `requests` (which already bundles certifi's CA list itself). On macOS, the
# official python.org installer ships its own cert store that's often empty
# until "Install Certificates.command" is run manually, causing every feed
# fetch to fail with CERTIFICATE_VERIFY_FAILED even though the network and
# credentials are fine. Pointing urllib's default at certifi's bundle (already
# a dependency here) fixes this without touching the system Python at all.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import feedparser
import requests
import trafilatura
from dateutil import parser as date_parser

logger = logging.getLogger(__name__)

# Below this many characters, treat the RSS body as a truncated teaser
# and go fetch the full article instead.
FULL_TEXT_THRESHOLD_CHARS = 800

# Standard headers so we don't get blocked as an obvious bot.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; InspoPipelineBot/1.0; "
        "+https://example.com/bot-info)"
    )
}


class RSSAdapter:
    """Fetch and normalize RSS feeds."""

    def __init__(self, timeout_seconds: int = 30):
        self.timeout = timeout_seconds

    def fetch_feed(self, feed_url: str, source_name: str, since: Optional[datetime] = None) -> Dict:
        """
        Fetch a single RSS feed and normalize entries to a standard format.

        Returns a dict with:
            {
                "items": List[Dict] — normalized articles,
                "stats": {
                    "found": int,           # entries seen in the feed
                    "full_text_fetched": int,  # entries where we fetched full text
                    "full_text_failed": int,   # full-text fetch attempted but failed
                    "error": Optional[str],    # set if the whole feed failed
                }
            }

        Each item in "items" has:
            {title, url, published_date, text, source_name, text_complete,
             content_hash}

        Never raises — a broken feed returns items=[] and stats["error"] set,
        so the caller can log it and move on to the next source.
        """
        stats = {
            "found": 0,
            "full_text_fetched": 0,
            "full_text_failed": 0,
            "error": None,
        }

        try:
            parsed = feedparser.parse(feed_url, request_headers=DEFAULT_HEADERS)
        except Exception as e:
            logger.error(f"[{source_name}] Failed to parse feed {feed_url}: {e}")
            stats["error"] = str(e)
            return {"items": [], "stats": stats}

        # feedparser doesn't always raise on failure — it sets bozo=1 instead.
        if getattr(parsed, "bozo", 0) == 1 and not parsed.entries:
            error_msg = str(getattr(parsed, "bozo_exception", "unknown parse error"))
            logger.error(f"[{source_name}] Feed parse error, no entries: {error_msg}")
            stats["error"] = error_msg
            return {"items": [], "stats": stats}

        entries = parsed.entries or []
        stats["found"] = len(entries)

        if not entries:
            logger.warning(f"[{source_name}] Feed returned zero items: {feed_url}")
            return {"items": [], "stats": stats}

        items = []
        for entry in entries:
            if since is not None:
                # Skip entries older than the look-back window before spending
                # a full-text download on them (some feeds hold hundreds).
                published = self._parse_published_date(entry)
                if published:
                    d = date_parser.parse(published)
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=timezone.utc)
                    if d < since:
                        continue
            try:
                item = self._normalize_entry(entry, source_name, stats)
                if item:
                    items.append(item)
            except Exception as e:
                # One bad entry should never take down the whole feed.
                logger.warning(
                    f"[{source_name}] Skipping malformed entry: {e}"
                )
                continue

        logger.info(
            f"[{source_name}] found={stats['found']} "
            f"full_text_fetched={stats['full_text_fetched']} "
            f"full_text_failed={stats['full_text_failed']}"
        )

        return {"items": items, "stats": stats}

    def _normalize_entry(self, entry, source_name: str, stats: Dict) -> Optional[Dict]:
        """Normalize a single feedparser entry into our standard item shape."""

        url = entry.get("link")
        title = entry.get("title", "").strip()

        if not url:
            logger.warning(f"[{source_name}] Entry missing URL, skipping: {title!r}")
            return None

        # Get the best available body text from the feed itself.
        raw_text = self._extract_feed_body(entry)
        text_complete = True

        # If the feed body looks like a truncated teaser, go fetch the real article.
        if len(raw_text) < FULL_TEXT_THRESHOLD_CHARS:
            full_text = self.fetch_full_text(url)
            if full_text and len(full_text) > len(raw_text):
                raw_text = full_text
                stats["full_text_fetched"] += 1
            else:
                # Keep whatever we had rather than dropping the item.
                stats["full_text_failed"] += 1
                text_complete = False
                logger.warning(
                    f"[{source_name}] Full-text fetch failed or too short for: {url}"
                )

        published_date = self._parse_published_date(entry)
        content_hash = hashlib.sha256(
            (title + raw_text).encode("utf-8")
        ).hexdigest()

        return {
            "title": title,
            "url": url,
            "published_date": published_date,
            "text": raw_text,
            "source_name": source_name,
            "text_complete": text_complete,
            "content_hash": content_hash,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def _extract_feed_body(self, entry) -> str:
        """Pull the best available text out of a feedparser entry."""
        # Prefer full "content" field (some feeds include it), fall back to summary.
        if "content" in entry and entry["content"]:
            raw_html = entry["content"][0].get("value", "")
        else:
            raw_html = entry.get("summary", "") or entry.get("description", "")

        if not raw_html:
            return ""

        # Strip HTML tags to get plain text for length checks / downstream use.
        text = trafilatura.extract(f"<html><body>{raw_html}</body></html>") or raw_html
        return text.strip()

    def _parse_published_date(self, entry) -> Optional[str]:
        """Extract and normalize the published date to ISO format."""
        date_str = entry.get("published") or entry.get("updated")
        if not date_str:
            return None
        try:
            parsed = date_parser.parse(date_str)
            return parsed.isoformat()
        except (ValueError, TypeError):
            logger.warning(f"Could not parse date: {date_str!r}")
            return None

    def fetch_full_text(self, article_url: str) -> Optional[str]:
        """
        Fetch the full article page and extract main text with trafilatura.

        Returns: Full text, or None if the fetch/extraction fails.
        Never raises — network and parsing errors are caught and logged.
        """
        try:
            response = requests.get(
                article_url,
                headers=DEFAULT_HEADERS,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"Full-text fetch failed for {article_url}: {e}")
            return None

        try:
            extracted = trafilatura.extract(
                response.text,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
        except Exception as e:
            logger.warning(f"Trafilatura extraction failed for {article_url}: {e}")
            return None

        if not extracted:
            logger.warning(f"Trafilatura found no main content for {article_url}")
            return None

        return extracted.strip()
