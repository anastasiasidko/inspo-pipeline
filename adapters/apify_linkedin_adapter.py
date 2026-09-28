"""
Apify LinkedIn adapter — public profile posts, no login, no cookies.

Rule from the user: a broken scraper never stops the run. Actors are tried in
order; if one errors, times out or returns nothing usable, the next similar
actor is tried, and every switch is logged. If all fail, the source returns
zero items with an error (the run itself continues).

Every run is capped by maxTotalChargeUsd so a scraper can't overspend.
"""

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import certifi
import requests
from dateutil import parser as date_parser

logger = logging.getLogger(__name__)

API = "https://api.apify.com/v2"
DEFAULT_MAX_POSTS = 30
DEFAULT_MAX_CHARGE_USD = 0.50
WAIT_SECONDS = 300


def _public_id(url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url)
    return m.group(1) if m else url


def _posted_limit(days: int) -> str:
    return "24h" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "3months"


# Ordered: primary first, then similar actors (all no-login).
ACTORS: List[Dict] = [
    {"id": "harvestapi/linkedin-profile-posts",
     "input": lambda url, n, days: {"targetUrls": [url], "maxPosts": n, "postedLimit": _posted_limit(days)}},
    {"id": "apimaestro/linkedin-profile-posts",
     "input": lambda url, n, days: {"username": _public_id(url), "limit": min(n, 100), "total_posts": n}},
    {"id": "datadoping/linkedin-profile-posts-scraper",
     "input": lambda url, n, days: {"profiles": [url], "max_posts": n}},
    {"id": "unseenuser/LinkedIn-Content",
     "input": lambda url, n, days: {"profileUrls": [url], "totalPostsPerProfile": n,
                                     "postedLimit": _posted_limit(days)}},
]

def handle_from_url(url: str) -> str:
    """https://x.com/olgandrienko -> olgandrienko"""
    parts = [p for p in re.sub(r"[?#].*$", "", url).split("/")[3:] if p]
    return (parts[0] if parts else url).lstrip("@")


# X / Twitter, cheapest reliable first (prices per tweet at Apify's free tier).
X_ACTORS: List[Dict] = [
    {"id": "apidojo/tweet-scraper",
     "input": lambda url, n, days: {"twitterHandles": [handle_from_url(url)], "maxItems": n, "sort": "Latest"}},
    {"id": "xquik/x-tweet-scraper",
     "input": lambda url, n, days: {"twitterHandles": [handle_from_url(url)], "maxItems": n}},
    {"id": "danek/twitter-scraper",
     "input": lambda url, n, days: {"username": handle_from_url(url), "max_posts": n}},
    {"id": "apidojo/twitter-scraper-lite",
     "input": lambda url, n, days: {"twitterHandles": [handle_from_url(url)], "maxItems": n}},
]

TEXT_KEYS = ["content", "text", "fullText", "tweetText", "postText", "post_text", "commentary", "caption", "description"]
URL_KEYS = ["linkedinUrl", "postUrl", "post_url", "twitterUrl", "tweetUrl", "url", "link"]
DATE_KEYS = ["postedAt", "createdAt", "created_at", "taken_at", "timestamp", "posted_at", "postedDate", "posted_date", "publishedAt", "createdAt", "date", "timestamp"]


def _first(item: Dict, keys: List[str]):
    for k in keys:
        v = item.get(k)
        if v not in (None, "", {}, []):
            return v
    return None


def _as_text(v) -> Optional[str]:
    if isinstance(v, dict):
        v = _first(v, ["text", "content"])
    return v.strip() if isinstance(v, str) and v.strip() else None


def _as_iso(v) -> Optional[str]:
    try:
        if isinstance(v, dict):
            v = _first(v, ["date", "timestamp", "iso", "value"])
        if isinstance(v, (int, float)):
            v = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        if isinstance(v, str):
            return date_parser.parse(v).isoformat()
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def normalize_post(item: Dict, source_name: str, actor_id: str) -> Optional[Dict]:
    text = _as_text(_first(item, TEXT_KEYS))
    url = _first(item, URL_KEYS)
    if not text or not isinstance(url, str):
        return None
    author = item.get("author")
    author_name = author.get("name") if isinstance(author, dict) else (author if isinstance(author, str) else None)
    title = text.split("\n", 1)[0][:90]
    return {
        "title": title, "source_url": url, "text": text, "source_name": source_name,
        "published_date": _as_iso(_first(item, DATE_KEYS)), "author": author_name or "",
        "text_complete": True, "tool": f"Apify: {actor_id}",
        "content_hash": hashlib.sha256((title + text).encode("utf-8")).hexdigest(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_actor(actor_id: str, actor_input: Dict, token: str, max_charge: float, timeout: int):
    """Start an actor run and wait. Returns (items, cost_usd). Raises on failure."""
    headers = {"Authorization": f"Bearer {token}"}
    slug = actor_id.replace("/", "~")
    r = requests.post(f"{API}/acts/{slug}/runs", headers=headers, json=actor_input,
                      params={"waitForFinish": 60, "maxTotalChargeUsd": max_charge},
                      timeout=90, verify=certifi.where())
    r.raise_for_status()
    run = r.json()["data"]
    deadline = time.time() + timeout
    while run["status"] in ("READY", "RUNNING") and time.time() < deadline:
        time.sleep(5)
        run = requests.get(f"{API}/actor-runs/{run['id']}", headers=headers,
                           timeout=30, verify=certifi.where()).json()["data"]
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"run ended with status {run['status']}")
    items = requests.get(f"{API}/datasets/{run['defaultDatasetId']}/items", headers=headers,
                         params={"clean": "true"}, timeout=60, verify=certifi.where()).json()
    final = requests.get(f"{API}/actor-runs/{run['id']}", headers=headers,
                         timeout=30, verify=certifi.where()).json()["data"]
    return items, float(final.get("usageTotalUsd") or 0.0)


class ApifyProfileAdapter:
    def __init__(self, token: Optional[str] = None, max_posts: int = DEFAULT_MAX_POSTS,
                 max_charge_usd: float = DEFAULT_MAX_CHARGE_USD, wait_seconds: int = WAIT_SECONDS,
                 actors: Optional[List[Dict]] = None, runner: Callable = _run_actor,
                 platform: str = "linkedin", on_success: Optional[Callable] = None):
        self.token = token or os.environ.get("APIFY_TOKEN", "")
        self.max_posts = max_posts
        self.max_charge = max_charge_usd
        self.wait_seconds = wait_seconds
        self.actors = actors or ACTORS
        self.runner = runner
        self.platform = platform
        self.on_success = on_success

    def fetch_profile(self, profile_url: str, source_name: str, lookback_days: int = 7) -> Dict:
        """Never raises. Returns {"items", "stats"} like the RSS adapter, plus
        stats["cost_usd"], stats["tool"], stats["fallbacks"] (what failed first)."""
        stats = {"found": 0, "full_text_fetched": 0, "full_text_failed": 0, "error": None,
                 "cost_usd": 0.0, "tool": None, "fallbacks": [], "notices": []}
        if not self.token:
            stats["error"] = "APIFY_TOKEN not set"
            return {"items": [], "stats": stats}

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        for actor in self.actors:
            actor_id = actor["id"]
            try:
                raw, cost = self.runner(actor_id, actor["input"](profile_url, self.max_posts, lookback_days),
                                        self.token, actor.get("max_charge", self.max_charge), self.wait_seconds)
                stats["cost_usd"] += cost
                posts = [p for p in (normalize_post(i, source_name, actor_id) for i in raw) if p]
                if raw and not posts:
                    raise ValueError("output format not recognised (no text/url fields)")
                recent = []
                for p in posts:
                    d = date_parser.parse(p["published_date"]) if p["published_date"] else None
                    if d is not None and d.tzinfo is None:
                        d = d.replace(tzinfo=timezone.utc)
                    if d is None or d >= cutoff:
                        recent.append(p)
                stats.update(found=len(recent), tool=f"Apify: {actor_id}")
                if stats["fallbacks"]:
                    stats["notices"].append(
                        f"{self.platform}: a scraper failed, so {actor_id} was used instead "
                        f"({'; '.join(stats['fallbacks'])})")
                if posts and self.on_success:
                    self.on_success(actor_id)
                logger.info(f"[{source_name}] {actor_id}: {len(raw)} posts returned, "
                            f"{len(recent)} in the last {lookback_days} days, cost ${cost:.4f}")
                return {"items": recent, "stats": stats}
            except Exception as e:
                msg = f"{actor_id}: {e}"
                stats["fallbacks"].append(msg)
                logger.warning(f"[{source_name}] Scraper failed, trying the next one — {msg}")
        stats["error"] = "all scrapers failed: " + "; ".join(stats["fallbacks"])
        stats["notices"].append(f"{self.platform} ({source_name}): every scraper failed — {'; '.join(stats['fallbacks'])}")
        logger.error(f"[{source_name}] {stats['error']}")
        return {"items": [], "stats": stats}


ApifyLinkedInAdapter = ApifyProfileAdapter  # LinkedIn is the default platform
