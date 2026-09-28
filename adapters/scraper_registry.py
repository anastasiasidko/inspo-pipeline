"""
Which Apify scraper handles which platform — and what to do when there is none.

Rule (from the user): when a platform has no scraper yet, say so, find one in
Apify's Store, and use it. Every scraper found this way is:
  - listed in `notices` so the user is told,
  - saved in scrapers.json (marked unverified until it returns real data),
  - run with a small per-run spend cap.
Built-in platforms (LinkedIn, X) keep their hand-picked, ordered scraper lists.
"""

import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import certifi
import requests

from adapters.apify_linkedin_adapter import (ACTORS as LINKEDIN_ACTORS, X_ACTORS, ApifyProfileAdapter,
                                             handle_from_url)

logger = logging.getLogger(__name__)

REGISTRY_FILE = Path(__file__).resolve().parent.parent / "scrapers.json"
API = "https://api.apify.com/v2"
MAX_PRICE_PER_ITEM = 0.01      # never auto-pick a scraper dearer than this per result
DISCOVERED_MAX_CHARGE = 0.10   # spend cap per run for a scraper found automatically
MAX_CANDIDATES = 3

PLATFORM_HOSTS = {
    "linkedin.com": "linkedin", "x.com": "x", "twitter.com": "x", "instagram.com": "instagram",
    "facebook.com": "facebook", "tiktok.com": "tiktok", "threads.net": "threads",
    "pinterest.com": "pinterest", "soundcloud.com": "soundcloud", "spotify.com": "spotify",
}
BUILTIN = {"linkedin": LINKEDIN_ACTORS, "x": X_ACTORS}
PLATFORM_NAMES = {"x": "X (Twitter)", "linkedin": "LinkedIn"}


def platform_of(url: str) -> Optional[str]:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for suffix, platform in PLATFORM_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return platform
    return None


def pretty(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, platform.capitalize())


# ---------------------------------------------------------------- saved scrapers
def load_registry() -> Dict:
    try:
        return json.loads(REGISTRY_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_registry(reg: Dict) -> None:
    REGISTRY_FILE.write_text(json.dumps(reg, indent=2, ensure_ascii=False))


def has_scraper(platform: str) -> bool:
    return platform in BUILTIN or bool(load_registry().get(platform, {}).get("actors"))


def _fill(value, url: str, n: int, days: int):
    """Substitute {url} {handle} {max_items} {days} into a saved input template."""
    if isinstance(value, dict):
        return {k: _fill(v, url, n, days) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, url, n, days) for v in value]
    if isinstance(value, str):
        if value == "{max_items}":
            return n
        if value == "{days}":
            return days
        return value.replace("{url}", url).replace("{handle}", handle_from_url(url))
    return value


def _template_actor(entry: Dict) -> Dict:
    return {"id": entry["id"], "max_charge": DISCOVERED_MAX_CHARGE,
            "input": lambda url, n, days, t=entry["template"]: _fill(t, url, n, days)}


def _mark_verified(platform: str, actor_id: str) -> None:
    reg = load_registry()
    for entry in reg.get(platform, {}).get("actors", []):
        if entry["id"] == actor_id and not entry.get("verified"):
            entry["verified"] = True
            save_registry(reg)


# ---------------------------------------------------------------- finding a scraper
def _price_per_item(pricing: Optional[Dict]) -> Optional[float]:
    """Cheapest per-result event price, or None if pricing isn't per result."""
    if not pricing or pricing.get("pricingModel") not in ("PAY_PER_EVENT", "PRICE_PER_DATASET_ITEM"):
        return None
    if pricing["pricingModel"] == "PRICE_PER_DATASET_ITEM":
        return pricing.get("pricePerUnitUsd")
    events = (pricing.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
    prices = [e.get("eventPriceUsd") for k, e in events.items() if "start" not in k.lower() and e.get("eventPriceUsd")]
    return min(prices) if prices else None


def _search_store(platform: str, token: str) -> List[Dict]:
    headers = {"Authorization": f"Bearer {token}"}
    seen, out = set(), []
    for query in (f"{platform} posts", platform):
        try:
            r = requests.get(f"{API}/store", headers=headers, params={"search": query, "limit": 15},
                             timeout=30, verify=certifi.where())
            for it in r.json()["data"]["items"]:
                if it["id"] not in seen:
                    seen.add(it["id"])
                    out.append(it)
        except Exception as e:
            logger.warning(f"Apify store search failed for {query!r}: {e}")
    return out


def pick_candidates(items: List[Dict]) -> List[Dict]:
    """Most-used, well-rated, pay-per-result scrapers that need no login."""
    good = []
    for it in items:
        price = _price_per_item(it.get("currentPricingInfo"))
        text = f"{it.get('title', '')} {it.get('description', '')}".lower()
        if price is None or price > MAX_PRICE_PER_ITEM or it.get("isDeprecated"):
            continue
        needs_login = bool(re.search(r"cookie|login required|requires login|session id", text)) \
            and not re.search(r"no cookie|without cookie|no login|without login|no account", text)
        if needs_login:
            continue
        users = (it.get("stats") or {}).get("totalUsers") or 0
        rating = it.get("actorReviewRating") or 0
        good.append((users * (1 + rating / 5), price, it))
    good.sort(key=lambda t: -t[0])
    return [dict(it, _price=price) for _, price, it in good[:MAX_CANDIDATES]]


def _input_schema(username: str, name: str, token: str) -> Optional[str]:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        act = requests.get(f"{API}/acts/{username}~{name}", headers=headers, timeout=30,
                           verify=certifi.where()).json()["data"]
        build = act["taggedBuilds"]["latest"]["buildId"]
        return requests.get(f"{API}/actor-builds/{build}", headers=headers, timeout=30,
                            verify=certifi.where()).json()["data"].get("inputSchema")
    except Exception as e:
        logger.warning(f"Could not read the input schema of {username}/{name}: {e}")
        return None


def _write_template(schema: str, sample_url: str, platform: str) -> Optional[Dict]:
    """Ask Claude to turn the scraper's input schema into a reusable input template."""
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = f"""An Apify scraper has this input schema:
{schema[:6000]}

Write the JSON input that scrapes the most recent public posts of ONE {platform} profile, for example {sample_url}.
Use these placeholders exactly where the value belongs: "{{url}}" (profile URL), "{{handle}}" (username without @),
"{{max_items}}" (maximum posts) and "{{days}}" (look-back days). Always keep the placeholders inside double
quotes, even where the field is a number, so the output stays valid JSON.
Fill only fields the scraper needs; leave optional fields out. Never enable login, cookies or follower/comment scraping.
Return the JSON object only, no other text."""
    try:
        resp = anthropic.Anthropic(api_key=key).messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1500, messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        raw = re.sub(r'(?<!")\{(max_items|days)\}(?!")', r'"{\1}"', m.group(0)) if m else None  # tolerate unquoted numbers
        template = json.loads(raw) if raw else None
        return template if isinstance(template, dict) and template else None
    except Exception as e:
        logger.warning(f"Could not build an input for the scraper: {e}")
        return None


def find_scraper(platform: str, sample_url: str, token: str) -> Tuple[List[Dict], List[str]]:
    """Search Apify's Store for scrapers for `platform`. -> (registry entries, notices)."""
    entries, notices = [], []
    for it in pick_candidates(_search_store(platform, token)):
        actor_id = f"{it['username']}/{it['name']}"
        schema = _input_schema(it["username"], it["name"], token)
        template = _write_template(schema, sample_url, platform) if schema else None
        if not template:
            continue
        entries.append({"id": actor_id, "template": template, "verified": False,
                        "price_per_item_usd": it["_price"], "users": (it.get("stats") or {}).get("totalUsers"),
                        "found_on": date.today().isoformat()})
    if entries:
        first = entries[0]
        notices.append(f"{pretty(platform)}: no scraper existed yet. Found {first['id']} in Apify "
                       f"(about ${first['price_per_item_usd']:.4f} per result, {first['users']} users) and used it. "
                       f"It is unverified — please check the {pretty(platform)} results.")
    else:
        notices.append(f"{pretty(platform)}: no scraper existed and none suitable was found in Apify, "
                       f"so these links were skipped.")
    return entries, notices


# ---------------------------------------------------------------- entry point
def get_adapter(platform: str, sample_url: str, token: Optional[str] = None,
                finder: Callable = find_scraper) -> Tuple[Optional[ApifyProfileAdapter], List[str]]:
    """-> (adapter or None, notices). Finds and saves a scraper if the platform has none."""
    token = token or os.environ.get("APIFY_TOKEN", "")
    if platform in BUILTIN:
        return ApifyProfileAdapter(token=token, actors=BUILTIN[platform], platform=platform), []
    notices: List[str] = []
    reg = load_registry()
    if not reg.get(platform, {}).get("actors"):
        if not token:
            return None, [f"{pretty(platform)}: no scraper, and APIFY_TOKEN is not set to look one up."]
        entries, notices = finder(platform, sample_url, token)
        if not entries:
            return None, notices
        reg[platform] = {"actors": entries}
        save_registry(reg)
        logger.info(f"Saved {len(entries)} scraper(s) for {platform} to scrapers.json")
    actors = [_template_actor(e) for e in reg[platform]["actors"]]
    return ApifyProfileAdapter(token=token, actors=actors, platform=platform,
                               on_success=lambda aid, p=platform: _mark_verified(p, aid)), notices
