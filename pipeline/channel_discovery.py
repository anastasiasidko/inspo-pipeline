"""
Find a competitor's official channels (website, newsletter, podcast, YouTube,
LinkedIn, …) using Claude with web search. The person reviews the result before
anything is collected, so wrong guesses cost nothing but a correction.
"""

import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Tuple

from adapters.feed_discovery import URL_RE
from pipeline import usage_log

logger = logging.getLogger(__name__)

DISCOVERY_MODEL = "claude-haiku-4-5-20251001"  # web search also works on Haiku, at about a third of the price
PROMPT = """Find the official online channels of "{name}", a thought leader / coach / consultant / creator
(context: personal branding, freelancing, business growth for passion-driven professionals, career and burnout).

Search the web. Return ONLY channels you found evidence for: a URL that appears in the search results or on the
person's own official website. Never guess or construct a URL. Look for: website/blog, newsletter, podcast,
YouTube, LinkedIn (public profile URL), Substack, Medium, Instagram, X, Facebook, TikTok. Label a standalone email newsletter exactly "Newsletter" (not "Website") if it has its own signup page.
Do NOT include sales pages: courses, workshops, coaching or consulting offers, booking or checkout pages, landing
pages. Include a personal website only if it carries content (articles, blog, about page), not just an offer.
If several people share the name, prefer the one that fits the context. If you cannot tell who it is, return [].

For each channel also say where you found the link ("source": the title or address of the page).
Return JSON only, no other text:
[{{"label": "Website", "url": "https://...", "source": "..."}}, {{"label": "LinkedIn", "url": "https://www.linkedin.com/in/...", "source": "..."}}]"""


def _norm(url: str) -> str:
    return url.strip().lower().rstrip("/")


# Each search is paid (several web searches, and the results are fed back to the model: roughly $0.15 a name).
# A person can trigger the same search twice — a double click, a page reload while it is running — so the same
# name is searched once at a time and the answer is reused for a day. `refresh` asks for a new search but never
# repeats one that finished less than a minute ago.
CACHE_SECONDS = 24 * 3600
REFRESH_MIN_AGE = 60
_cache: Dict[str, Tuple[float, List[Dict]]] = {}
_locks: Dict[str, threading.Lock] = {}
_guard = threading.Lock()


def discover_channels(name: str, model: str = DISCOVERY_MODEL, refresh: bool = False) -> List[Dict]:
    key = name.strip().lower()
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        hit = _cache.get(key)
        if hit:
            age = time.time() - hit[0]
            if age < CACHE_SECONDS and not (refresh and age >= REFRESH_MIN_AGE):
                logger.info(f"Channel search for {name!r}: reusing the answer from {int(age)}s ago (no new charge)")
                return hit[1]
        found = _search_channels(name, model)
        if found:
            _cache[key] = (time.time(), found)
        return found


def _search_channels(name: str, model: str) -> List[Dict]:
    """-> [{"label", "url", "source"}]. `source` says where the link came from: the search
    result it matched, or a warning if it was not in the search results.
    Returns [] if the search fails (the person can add links by hand)."""
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.error("ANTHROPIC_API_KEY not set — cannot search for channels")
        return []
    client = anthropic.Anthropic(api_key=key)
    messages = [{"role": "user", "content": PROMPT.format(name=name)}]
    results: Dict[str, str] = {}  # normalised url -> title, from the actual search results
    try:
        for _ in range(4):  # a long search can pause; continue it a few times at most
            resp = client.messages.create(
                model=model, max_tokens=4000, messages=messages,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            )
            usage_log.record_response("Find channels", model, resp, detail=name)
            for block in resp.content:
                if getattr(block, "type", "") == "web_search_tool_result" and isinstance(block.content, list):
                    for r in block.content:
                        results[_norm(r.url)] = r.title
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"\[.*\]", text, re.S)
        items = json.loads(m.group(0)) if m else []
    except Exception as e:
        logger.error(f"Channel search failed for {name}: {e}")
        return []
    out, seen = [], set()
    for it in items:
        url = str(it.get("url", "")).strip()
        if URL_RE.fullmatch(url) and url not in seen:
            seen.add(url)
            hit = results.get(_norm(url))
            source = (f"search result: {hit}" if hit
                      else f"not a search result, check it (Claude says: {it.get('source') or 'no source given'})")
            out.append({"label": str(it.get("label") or "Link").strip(), "url": url, "source": source})
    return out
