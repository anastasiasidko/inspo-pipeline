"""
Videos attached to posts. Nothing here runs by itself: after a run, posts whose text turned out
relevant and that carry a video are listed for approval (with the length). Only when the person
approves one is an Apify actor found and run (spend-capped) to get its transcript, which then goes
through the normal extraction, quote check and digest like any other text.
"""

import logging
import os
import re
from datetime import date
from typing import Callable, Dict, List, Optional

from adapters import scraper_registry as reg
from adapters.apify_linkedin_adapter import _run_actor

logger = logging.getLogger(__name__)

TRANSCRIPT_KEYS = ("transcript", "transcription", "text", "captions", "subtitles", "fullText")
REGISTRY_KEY = "video_transcript"
TASK = "returns the spoken-word transcript of ONE video, for example {url}"


def video_candidates(items: List[Dict], verified_urls) -> List[Dict]:
    """Posts that have a video AND produced at least one quote-verified finding this run."""
    keep = set(verified_urls or [])
    out = []
    for item in items:
        video = item.get("video")
        if not video or item.get("source_url") not in keep:
            continue
        secs = video.get("duration_seconds")
        out.append({
            "author": item.get("author") or item.get("source_name", "").split(" — ")[0],
            "source_name": item.get("source_name", ""), "post_url": item["source_url"],
            "video_url": video.get("url") or item["source_url"], "duration_seconds": secs,
            "length": f"{round(secs / 60)}-minute" if secs and secs >= 30 else ("short" if secs else "length unknown"),
            "published_date": item.get("published_date"),
        })
    return out


def _transcript_from(items: List[Dict]) -> str:
    """The transcript text from an actor's output; the longest text found under a transcript-like key."""
    best = ""
    def take(v):
        nonlocal best
        if isinstance(v, str):
            text = v
        elif isinstance(v, list):
            text = " ".join(str(seg.get("text", seg) if isinstance(seg, dict) else seg) for seg in v)
        else:
            return
        if len(text) > len(best):
            best = text
    for it in items:
        for k in TRANSCRIPT_KEYS:
            take(it.get(k))
    return re.sub(r"\s+", " ", best).strip()


def find_video_actors(token: str) -> List[Dict]:
    """Saved transcript actors, or search Apify's Store for cheap ones and save them (unverified)."""
    registry = reg.load_registry()
    if registry.get(REGISTRY_KEY, {}).get("actors"):
        return registry[REGISTRY_KEY]["actors"]
    found, seen = [], set()
    for query in ("video transcript", "linkedin video transcript"):
        for it in reg.pick_candidates(reg._search_store(query, token)):
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            schema = reg._input_schema(it["username"], it["name"], token)
            template = reg._write_template(schema, "https://example.com/video", "video", task=TASK) if schema else None
            if template:
                found.append({"id": f"{it['username']}/{it['name']}", "template": template, "verified": False,
                              "price_per_item_usd": it["_price"], "found_on": date.today().isoformat()})
    if found:
        registry[REGISTRY_KEY] = {"actors": found}
        reg.save_registry(registry)
    return found


def get_transcript(candidate: Dict, token: Optional[str] = None, runner: Callable = _run_actor,
                   finder: Callable = find_video_actors) -> Dict:
    """Never raises. -> {"text", "cost_usd", "actor", "notice"}; text is "" when nothing could be fetched."""
    token = token or os.environ.get("APIFY_TOKEN", "")
    out = {"text": "", "cost_usd": 0.0, "actor": None, "notice": None}
    if not token:
        out["notice"] = "APIFY_TOKEN is not set, so the video could not be fetched."
        return out
    actors = finder(token)
    if not actors:
        out["notice"] = "No suitable video-transcript scraper was found in Apify, so this video was skipped."
        return out
    tried = []
    for entry in actors:
        try:
            actor_input = reg._fill(entry["template"], candidate["video_url"], 1, 1)
            raw, cost = runner(entry["id"], actor_input, token, reg.DISCOVERED_MAX_CHARGE, 600)
            out["cost_usd"] += cost
            text = _transcript_from(raw)
            if not text:
                raise ValueError("no transcript in the output")
            out.update(text=text, actor=entry["id"])
            reg._mark_verified(REGISTRY_KEY, entry["id"])
            if tried:
                out["notice"] = f"{'; '.join(tried)} — used {entry['id']} instead."
            return out
        except Exception as e:
            tried.append(f"{entry['id']}: {e}")
            logger.warning(f"Video scraper failed, trying the next one — {entry['id']}: {e}")
    out["notice"] = "Every video scraper failed: " + "; ".join(tried)
    return out
