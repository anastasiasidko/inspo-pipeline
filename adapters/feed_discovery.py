"""
Turn a channel link (website, Substack, Medium, YouTube, podcast page, LinkedIn…)
into something the pipeline can collect from.

- LinkedIn profile  -> collected through Apify ("linkedin")
- anything with an RSS/Atom feed (found on the page or at common paths) -> "rss"
- everything else (Instagram, X, Facebook, TikTok, Spotify…) -> not collectable yet

Never raises: a link that can't be resolved comes back with kind=None and a note.
"""

import logging
import os
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import feedparser
import requests

from adapters.scraper_registry import has_scraper, platform_of, pretty

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; InspoPipelineBot/1.0)"}
COMMON_FEED_PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/blog/feed", "/blog/feed/", "/index.xml"]
NOT_COLLECTABLE = ("insighttimer.com",)


def parse_channel_lines(text: str) -> List[Tuple[str, str]]:
    """'Label: https://url' lines -> [(label, url)]. Lines with no URL are ignored."""
    out = []
    for line in (text or "").splitlines():
        m = URL_RE.search(line)
        if not m:
            continue
        url = m.group(0).rstrip(".,);")
        label = line[:m.start()].strip().rstrip(":-—–").strip() or urlparse(url).netloc
        out.append((label, url))
    return out


def format_channel_lines(channels: List[Tuple[str, str]]) -> str:
    return "\n".join(f"{label}: {url}" for label, url in channels)


def _has_entries(url: str) -> bool:
    try:
        return len(feedparser.parse(url).entries) > 0
    except Exception:
        return False


def _find_feed(page_url: str) -> Optional[str]:
    """Look for a feed the page advertises, then try common feed paths."""
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15, verify=certifi.where())
        for m in re.finditer(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', r.text, re.I):
            href = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.I)
            if href:
                feed = urljoin(page_url, href.group(1).replace("&amp;", "&"))
                if _has_entries(feed):
                    return feed
    except Exception as e:
        logger.info(f"Could not read {page_url}: {e}")
    parts = urlparse(page_url)
    base = f"{parts.scheme}://{parts.netloc}"
    for path in COMMON_FEED_PATHS:
        for root in (page_url.rstrip("/"), base):
            if _has_entries(root + path):
                return root + path
    return None


def resolve_channel(url: str) -> Dict:
    """-> {"kind": "linkedin"|"rss"|None, "feed_url": str|None, "note": str}"""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    platform = platform_of(url)
    if platform == "linkedin":
        if "/in/" in url:
            return {"kind": "linkedin", "feed_url": url, "note": "LinkedIn profile (Apify)"}
        return {"kind": None, "feed_url": None, "note": "only LinkedIn personal profiles (/in/…) are supported"}
    if platform:  # X, Instagram, Facebook, TikTok… collected through an Apify scraper
        extra = "" if has_scraper(platform) else " — no scraper yet, one will be looked up automatically"
        return {"kind": platform, "feed_url": url, "note": f"{pretty(platform)} (Apify){extra}"}
    if any(host.endswith(d) for d in NOT_COLLECTABLE):
        return {"kind": None, "feed_url": None, "note": "no feed available for this platform yet"}
    if host == "medium.com":
        m = re.match(r"https?://(?:www\.)?medium\.com/(@[^/?#]+)", url)
        if m and _has_entries(f"https://medium.com/feed/{m.group(1)}"):
            return {"kind": "rss", "feed_url": f"https://medium.com/feed/{m.group(1)}", "note": "Medium feed"}
    if host == "podcasts.apple.com":
        m = re.search(r"/id(\d+)", url)
        if m:
            try:
                feed = requests.get(f"https://itunes.apple.com/lookup?id={m.group(1)}", timeout=15,
                                    verify=certifi.where()).json()["results"][0].get("feedUrl")
                if feed and _has_entries(feed):
                    return {"kind": "rss", "feed_url": feed, "note": "podcast feed"}
            except Exception:
                pass
    feed = _find_feed(url)
    if feed:
        return {"kind": "rss", "feed_url": feed, "note": "RSS feed"}
    return {"kind": None, "feed_url": None, "note": "no feed found on this page"}


def newsletter_needing_subscription(channels: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    """
    (label, url) of a channel labeled "Newsletter" that has NO public feed — i.e. the
    only way to collect it is to actually subscribe. Most Substack/beehiiv/Ghost
    newsletters DO have a public feed and never reach this — this is only the ones
    that gate content behind a real signup.
    """
    for label, url in channels:
        if label.strip().lower() != "newsletter":
            continue
        r = resolve_channel(url)
        if r["kind"] is None:
            return label, url
        return None  # it already has a feed — collectable with no subscription
    return None


def sources_for_competitor(name: str, channels: List[Tuple[str, str]]) -> Tuple[List[Dict], List[str]]:
    """-> (sources the pipeline can collect, human-readable list of links skipped and why)."""
    sources, skipped, seen = [], [], set()
    for label, url in channels:
        r = resolve_channel(url)
        if r["kind"] is None:
            skipped.append(f"{name} — {label}: {r['note']}")
            continue
        if r["feed_url"] in seen:
            continue
        seen.add(r["feed_url"])
        sources.append({"name": f"{name} — {label}", "feed_url": r["feed_url"], "kind": r["kind"]})
    return sources, skipped
