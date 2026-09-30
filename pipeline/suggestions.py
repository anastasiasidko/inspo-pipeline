"""
Suggest new people to follow: thought leaders who write closely on the topics of the digest and are
not already competitors. Claude with web search; every suggestion must cite a page that actually
appeared in the search results, and reach (followers / subscribers) is shown only when a search
result states it. Nothing is added or run automatically.
"""

import json
import logging
import os
import re
from typing import Dict, List

from adapters.feed_discovery import URL_RE
from pipeline import usage_log
from pipeline.channel_discovery import _norm

logger = logging.getLogger(__name__)

SUGGEST_MODEL = "claude-sonnet-5"
PROMPT = """Below is a digest of what several thought leaders published recently
(context: personal branding, freelancing, business growth for passion-driven professionals, career and burnout).

{digest}

Search the web and suggest {n} OTHER people who write closely about the same topics as this digest.
Do NOT suggest anyone in this list of people already followed: {existing}.

For each person give:
- "name"
- "why": one or two sentences saying which digest topics they write about and how, based on pages you found.
  Describe only what the pages show; do not add opinions.
- "url": the page (their site, profile or a page about them) that you found them on. It must appear in your search results.
- "reach": how many people follow or read them (for example "about 120,000 LinkedIn followers"), ONLY if a search
  result states it; otherwise null. Never estimate.
- "reach_source": the URL of the page that states the reach, or null.
Return JSON only, no other text:
[{{"name": "...", "why": "...", "url": "https://...", "reach": null, "reach_source": null}}]"""


TOPIC_PROMPT = """A digest for someone monitoring thought leadership said this:
\"\"\"{context}\"\"\"

The specific thing they want to explore further is: "{topic}"

The scope that matters (rules of the person doing this research):
{rules}

Search the web and find {n} people who write about essentially this same specific thing — not just the same
broad field, the same specific point — within that scope.
Do NOT suggest anyone in this list of people already followed or already suggested: {existing}.

For each person give:
- "name"
- "why": one or two sentences saying how what they write connects to "{topic}", based on pages you found.
  Describe only what the pages show; do not add opinions.
- "url": the page (their site, profile or a page about them) that you found them on. It must appear in your search results.
- "reach": how many people follow or read them (for example "about 120,000 LinkedIn followers"), ONLY if a search
  result states it; otherwise null. Never estimate.
- "reach_source": the URL of the page that states the reach, or null.
Return JSON only, no other text:
[{{"name": "...", "why": "...", "url": "https://...", "reach": null, "reach_source": null}}]"""


SIMILAR_PROMPT = """{name} is a thought leader / coach / consultant / creator.
What they have been writing about recently (from their own posts):
{about}

The scope that matters (rules of the person doing this research):
{rules}

Search the web and find {n} OTHER people who write about essentially the same things as {name}, within that scope.
Do NOT suggest anyone in this list of people already followed or already suggested: {existing}.

For each person give:
- "name"
- "why": one or two sentences saying which of the same topics they write about and how, based on pages you found.
  Describe only what the pages show; do not add opinions.
- "url": the page (their site, profile or a page about them) that you found them on. It must appear in your search results.
- "reach": how many people follow or read them (for example "about 120,000 LinkedIn followers"), ONLY if a search
  result states it; otherwise null. Never estimate.
- "reach_source": the URL of the page that states the reach, or null.
Return JSON only, no other text:
[{{"name": "...", "why": "...", "url": "https://...", "reach": null, "reach_source": null}}]"""


def _search_people(prompt: str, existing_names: List[str], n: int, model: str, usage_out: dict = None,
                   purpose: str = "Suggest people") -> List[Dict]:
    """Shared by both entry points. -> up to n verified {name, why, url, reach, reach_source}; [] on failure."""
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return []
    existing = {e.strip().lower() for e in existing_names}
    messages = [{"role": "user", "content": prompt}]
    results: Dict[str, str] = {}
    try:
        client = anthropic.Anthropic(api_key=key)
        for _ in range(4):
            resp = client.messages.create(
                model=model, max_tokens=6000, messages=messages,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}])
            usage_log.record_response(purpose, model, resp)
            usage = getattr(resp, "usage", None)
            if usage_out is not None and usage is not None:
                usage_out["input"] = usage_out.get("input", 0) + (getattr(usage, "input_tokens", 0) or 0)
                usage_out["output"] = usage_out.get("output", 0) + (getattr(usage, "output_tokens", 0) or 0)
                tools = getattr(usage, "server_tool_use", None)
                searches = getattr(tools, "web_search_requests", 0) if tools is not None else 0
                usage_out["searches"] = usage_out.get("searches", 0) + (searches if isinstance(searches, int) else 0)
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
        logger.error(f"Suggesting people failed: {e}")
        if usage_out is not None:
            usage_out["error"] = " ".join(str(getattr(e, "body", {}).get("error", {}).get("message", e)
                                              if isinstance(getattr(e, "body", None), dict) else e).split())[:300]
        return []
    out = []
    for it in items:
        name, url = str(it.get("name", "")).strip(), str(it.get("url", "")).strip()
        if not name or name.lower() in existing or not URL_RE.fullmatch(url) or _norm(url) not in results:
            continue  # unnamed, already followed, or the page was not in the search results
        reach, reach_src = it.get("reach"), str(it.get("reach_source") or "").strip()
        stated = bool(reach) and _norm(reach_src) in results
        out.append({
            "name": name, "why": str(it.get("why", "")).strip(), "url": url,
            "reach": str(reach).strip() if stated else None, "reach_source": reach_src if stated else None,
        })
        if len(out) == n:
            break
    return out


def suggest_new_voices(digest_text: str, existing_names: List[str], n: int = 5,
                       model: str = SUGGEST_MODEL, usage_out: dict = None) -> List[Dict]:
    """People who write closely on the topics of the whole digest. [] if the search fails."""
    if not digest_text.strip():
        return []
    return _search_people(PROMPT.format(digest=digest_text[:8000], n=n + 3,
                                        existing=", ".join(existing_names) or "none"), existing_names, n, model, usage_out)


def similar_voices(name: str, about: str, rules: str, existing_names: List[str], n: int = 2,
                   model: str = SUGGEST_MODEL, usage_out: dict = None) -> List[Dict]:
    """`n` people who write about essentially the same things as one competitor, within the rules' scope."""
    prompt = SIMILAR_PROMPT.format(
        name=name, about=(about or "no recent posts available")[:4000], rules=(rules or "none")[:4000],
        n=n + 2, existing=", ".join(existing_names) or "none")
    return _search_people(prompt, existing_names + [name], n, model, usage_out, purpose=f"Similar people: {name}")


def voices_on_topic(topic: str, context: str, rules: str, existing_names: List[str], n: int = 5,
                    model: str = SUGGEST_MODEL, usage_out: dict = None) -> List[Dict]:
    """`n` people who write about one specific thing — not a whole competitor's body of work, one point
    from the digest (`context` is the paragraph or text it came from, so the search stays anchored to
    what was actually said, not just the bare topic word)."""
    if not topic.strip():
        return []
    prompt = TOPIC_PROMPT.format(topic=topic.strip(), context=(context or "")[:4000], rules=(rules or "none")[:4000],
                                 n=n + 2, existing=", ".join(existing_names) or "none")
    return _search_people(prompt, existing_names, n, model, usage_out, purpose=f"Topic search: {topic.strip()}")
