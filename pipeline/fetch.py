"""
Phase 1: Fetch raw items from RSS feeds

Input: a resolved source list (see engine.py's _resolve_sources)
Output: List of normalized raw items, ready for the dedup phase to store
Side effects: Logs per-source stats (found / full-text-retrieved / failed)

Error handling: Network failures are wrapped inside the adapter; one broken
source never kills the run. A source returning zero items warns loudly
instead of passing silently.
"""

import logging
from typing import Optional, List, Dict

from adapters.rss_adapter import RSSAdapter
from adapters.scraper_registry import get_adapter

logger = logging.getLogger(__name__)


def run_fetch_phase(
    sources: List[Dict],
    stream: str,
    request_timeout_seconds: int = 30,
    limit: Optional[int] = None,
    dry_run: bool = False,
    lookback_days: int = 14,
) -> Dict:
    """
    Fetch new articles from every configured source.

    Args:
        sources: resolved source list for this run — each item is a dict with
            at least {"name", "feed_url"} (today's only adapter is RSS; other
            adapters land here later without changing this phase's shape).
            Resolved by engine.py from Settings + typed input + mode — this
            phase doesn't know or care where the list came from.
        stream: stream identifier, tagged onto every item
        request_timeout_seconds: network call timeout
        limit: Cap total articles returned across all sources (for testing)
        dry_run: If True, still fetches (fetching is free/no LLM calls),
                 but caller should skip writing to storage when this is set.

    Returns:
        {
            "items": List[Dict]  — normalized articles ready for storage,
            "source_stats": {source_name: {found, full_text_fetched,
                                            full_text_failed, error}},
            "sources_returning_zero": List[str],
        }
    """
    adapter = RSSAdapter(timeout_seconds=request_timeout_seconds)
    platform_adapters: Dict[str, object] = {}
    notices: List[str] = []
    apify_cost = 0.0
    apify_by_tool: Dict[str, float] = {}

    all_items: List[Dict] = []
    source_stats: Dict[str, Dict] = {}
    sources_returning_zero: List[str] = []

    logger.info(f"Fetch phase starting — {len(sources)} sources configured")

    for source in sources:
        name = source["name"]
        feed_url = source["feed_url"]

        logger.info(f"Fetching: {name} ({feed_url})")

        kind = source.get("kind", "rss")
        if kind != "rss":
            if dry_run:
                logger.info(f"[{name}] DRY RUN — skipping paid Apify scrape")
                source_stats[name] = {"found": 0, "error": None}
                continue
            if kind not in platform_adapters:
                platform_adapters[kind], found_notices = get_adapter(kind, feed_url)
                notices += found_notices
            scraper = platform_adapters[kind]
            if scraper is None:
                source_stats[name] = {"found": 0, "error": f"no scraper available for {kind}"}
                sources_returning_zero.append(name)
                continue
            result = scraper.fetch_profile(feed_url, name, lookback_days=lookback_days)
            notices += result["stats"].get("notices", [])
            apify_cost += result["stats"].get("cost_usd", 0.0)
            tool = result["stats"].get("tool") or "Apify (all scrapers failed)"
            apify_by_tool[tool] = apify_by_tool.get(tool, 0.0) + result["stats"].get("cost_usd", 0.0)
        else:
            from datetime import datetime, timedelta, timezone
            result = adapter.fetch_feed(
                feed_url, name, since=datetime.now(timezone.utc) - timedelta(days=lookback_days))
        items = result["items"]
        stats = result["stats"]
        source_stats[name] = stats

        if stats.get("error"):
            logger.error(f"[{name}] Source failed: {stats['error']}")
            sources_returning_zero.append(name)
            continue

        if stats["found"] == 0:
            logger.warning(f"[{name}] Returned ZERO items — dead feed or genuinely quiet?")
            sources_returning_zero.append(name)
            continue

        # Tag every item with which stream it belongs to, and normalize the
        # adapter's "url" field to "source_url" — the name every downstream
        # phase (storage, extraction, synthesis) expects.
        for item in items:
            item["stream"] = stream
            item["source_type"] = kind
            if "url" in item:
                item["source_url"] = item.pop("url")

        all_items.extend(items)

    # Apply the overall limit across all sources combined (for testing).
    if limit is not None and len(all_items) > limit:
        logger.info(f"Applying --limit {limit}: truncating {len(all_items)} items")
        all_items = all_items[:limit]

    logger.info(
        f"Fetch phase complete — {len(all_items)} items fetched "
        f"from {len(sources) - len(sources_returning_zero)}/"
        f"{len(sources)} healthy sources"
    )

    if sources_returning_zero:
        logger.warning(
            f"Sources returning zero: {', '.join(sources_returning_zero)}"
        )

    return {
        "items": all_items,
        "source_stats": source_stats,
        "sources_returning_zero": sources_returning_zero,
        "apify_cost_usd": apify_cost,
        "apify_by_tool": apify_by_tool,
        "notices": notices,
    }
