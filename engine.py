"""
Engine — the only thing a screen (CLI, Streamlit app, anything else) ever
calls. It knows about the pipeline phases and the storage backend; it knows
nothing about how it's being displayed.

Two entry points, per the brief:
    run_stream(...)       — collect, analyze, score, synthesize; one run.
    get_run_details(...)  — everything a Results/Troubleshooting screen needs
                             to show about one past run.
"""

import logging
from typing import Dict, List, Optional

from pipeline import fetch, dedup, extract, score, synthesis, competitor_map, media, run_comment, usage_log
from storage.backend import StorageBackend

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_ID = "ksenia"


def _default_storage() -> StorageBackend:
    from storage.sheets_backend import SheetsBackend
    return SheetsBackend.from_env()


def _resolve_sources(storage: StorageBackend, client_id: str, stream: str,
                      inputs: List[str], mode: str) -> List[Dict]:
    """
    mode: "typed_only" | "saved_plus_typed" | "saved_only" — matches the
    three radio options in the app mockup.

    Typed inputs are raw strings the screen collected (one per line) — for
    competitor_monitoring today these are RSS feed URLs. Each becomes a
    {"name": value, "feed_url": value} source, same shape saved items use.
    """
    def typed_as_sources(raw_inputs: List[str]) -> List[Dict]:
        out = []
        for line in raw_inputs:
            if not line.strip():
                continue
            label, _, url = line.partition("|") if "|" in line else ("", "", line)
            url = url.strip()
            out.append({"name": label.strip() or url, "feed_url": url,
                        "kind": _kind_of(url)})
        return out

    def saved_as_sources() -> List[Dict]:
        from adapters.feed_discovery import parse_channel_lines, sources_for_competitor
        out = []
        for row in storage.get_saved_items(client_id, stream, active_only=True):
            name = row.get("competitor") or row.get("value", "")
            channels = parse_channel_lines(row.get("channels", ""))
            if not channels:
                logger.warning(f"[{name}] no channel links saved — skipped")
                continue
            sources, skipped = sources_for_competitor(name, channels)
            for line in skipped:
                logger.info(f"Not collected: {line}")
            out.extend(sources)
        return out

    if mode == "typed_only":
        return typed_as_sources(inputs)
    if mode == "saved_only":
        return saved_as_sources()
    if mode == "saved_plus_typed":
        combined = saved_as_sources() + typed_as_sources(inputs)
        seen = set()
        deduped = []
        for source in combined:
            if source["feed_url"] not in seen:
                seen.add(source["feed_url"])
                deduped.append(source)
        return deduped
    raise ValueError(f"Unknown mode: {mode!r} (expected typed_only / saved_plus_typed / saved_only)")


def _kind_of(url: str) -> str:
    from adapters.scraper_registry import platform_of
    return platform_of(url) or "rss"


def _within_lookback(items: List[Dict], lookback_days: int, by_source: Optional[Dict[str, int]] = None) -> List[Dict]:
    """Drop items published before the look-back window (undated items are kept). `by_source` gives a
    different window for particular sources (from the run comment)."""
    from datetime import datetime, timedelta, timezone
    from dateutil import parser as date_parser
    now = datetime.now(timezone.utc)
    kept = []
    for item in items:
        cutoff = now - timedelta(days=(by_source or {}).get(item.get("source_name"), lookback_days))
        published = item.get("published_date")
        try:
            d = date_parser.parse(published) if published else None
            if d is not None and d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            d = None
        if d is None or d >= cutoff:
            kept.append(item)
    return kept


def _config_with_rules(storage: StorageBackend, client_id: str, stream: str, lookback_days: int,
                        config: Optional[dict] = None) -> dict:
    """Local config plus the rules from the Rules tab: general rules first, then the stream's own."""
    from config import load_local_config  # local import avoids a cycle with main.py's CLI use
    config = {**(config or load_local_config(stream)), "digest_window_days": lookback_days}
    rule_parts = [
        (r["rules_text"] or "").strip()
        for r in (storage.get_rules(client_id, "general"), storage.get_rules(client_id, stream))
        if r
    ]
    rule_parts = [p for p in rule_parts if p]
    if rule_parts:
        config = {**config, "relevance_ruleset": "\n\n".join(rule_parts)}
    elif "relevance_ruleset" not in config:
        raise ValueError(
            f"No rules found for stream={stream!r} — save one in the Rules tab "
            f"first, or set relevance_ruleset in the local config for testing."
        )
    return config


SIMILAR_PER_COMPETITOR = 2
WEB_SEARCH_USD = 0.01  # Anthropic's price per web search request


def _find_similar_voices(storage: StorageBackend, client_id: str, stream: str, run_id: str, competitors: List[str],
                          lookback_days: int, config: dict):
    """For each competitor in this run, 2 other people who write about essentially the same things (by web
    search, within the scope of the rules). -> ({competitor: [people]}, cost_usd, [error messages])."""
    from pipeline import suggestions
    saved = {r.get("competitor"): r for r in storage.get_saved_items(client_id, stream, active_only=False)}
    followed = [n for n in saved if n] + list(competitors)
    findings = [f for f in storage.get_findings_for_digest(client_id, stream, days_back=lookback_days)
                if f.get("run_id") == run_id]
    out, usage, seen = {}, {}, list(followed)
    for name in competitors:
        about = [f"Positioning: {saved[name].get('positioning')}" for _ in [0] if saved.get(name, {}).get("positioning")]
        about += [f"Keywords: {saved[name].get('keywords')}" for _ in [0] if saved.get(name, {}).get("keywords")]
        about += [f"- {f['summary']}" for f in findings if f.get("entity") == name][:6]
        people = suggestions.similar_voices(name, "\n".join(about), config.get("relevance_ruleset", ""), seen,
                                            n=SIMILAR_PER_COMPETITOR, usage_out=usage)
        out[name] = people
        seen += [p["name"] for p in people]
    cost = 0.0
    if usage:
        from pipeline.costs import llm_cost_usd
        cost = (llm_cost_usd(config, suggestions.SUGGEST_MODEL, usage.get("input", 0), usage.get("output", 0))
                + WEB_SEARCH_USD * usage.get("searches", 0))
    return out, cost, ([usage["error"]] if usage.get("error") else [])


def run_stream(
    stream: str,
    inputs: List[str],
    mode: str,
    lookback_days: int,
    started_by: str,
    client_id: str = DEFAULT_CLIENT_ID,
    storage: Optional[StorageBackend] = None,
    config: Optional[dict] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    run_comment_text: str = "",
) -> Dict:
    """
    Run one stream end to end: collect, remove repeats, ask the AI, check its
    quotes, score novelty, write the digest.

    run_comment_text: free text for THIS run only (e.g. "for Instagram only look back 7 days"). Never saved
    to the Rules tab; see pipeline/run_comment.py.

    Returns: {"run_id": str, "digest": str, "stats": dict, ...}
    """
    storage = storage or _default_storage()
    storage.init()

    config = _config_with_rules(storage, client_id, stream, lookback_days, config)

    sources = _resolve_sources(storage, client_id, stream, inputs, mode)
    inputs_used = [s["feed_url"] for s in sources]

    map_requested, run_comment_text = run_comment.extract_map_trigger(run_comment_text)
    if map_requested:
        inputs_used.append(f"comment: {run_comment.MAP_TRIGGER}")
    comment = {"lookback": {}, "instructions": "", "summary": [], "warnings": [], "cost": 0.0}
    if run_comment_text.strip():
        inputs_used.append(f"comment: {run_comment_text.strip()}")
        if dry_run:
            logger.info("[DRY RUN] run comment not read (no LLM calls in a dry run)")
        else:
            comment = run_comment.parse_run_comment(run_comment_text, sources, lookback_days, config)
            for s in sources:
                if s["name"] in comment["lookback"]:
                    s["lookback_days"] = comment["lookback"][s["name"]]
            config = {**config, "relevance_ruleset": run_comment.add_instructions(
                config["relevance_ruleset"], comment["instructions"]),
                "digest_window_days": max([lookback_days] + list(comment["lookback"].values()))}

    run_id = storage.start_run(client_id, stream, started_by, inputs_used, mode)
    logger.info(f"Run {run_id} started — stream={stream}, mode={mode}, sources={len(sources)}")

    errors = 0
    try:
        fetch_result = fetch.run_fetch_phase(
            sources, stream,
            request_timeout_seconds=config.get("request_timeout_seconds", 30),
            limit=limit, dry_run=dry_run, lookback_days=lookback_days,
        )
        fetch_result["items"] = _within_lookback(fetch_result["items"], lookback_days, comment["lookback"])

        dedup_result = dedup.run_dedup_phase(
            storage, client_id, stream, run_id, fetch_result["items"], dry_run=dry_run,
            near_threshold=config.get("near_duplicate_threshold", 0.8),
        )

        extract_result = extract.run_extraction_phase(
            config, storage, client_id, run_id, dedup_result["new_items"],
            limit=limit, dry_run=dry_run,
        )
        errors += extract_result["errors"]

        from adapters.apify_linkedin_adapter import DEFAULT_MAX_POSTS
        map_rows = competitor_map.build_map(sources, fetch_result["items"], lookback_days, DEFAULT_MAX_POSTS)
        map_text = competitor_map.format_map(map_rows, lookback_days, DEFAULT_MAX_POSTS)
        show_map = lookback_days >= competitor_map.MONTHLY_MIN_DAYS or map_requested
        similar, similar_cost, similar_errors = {}, 0.0, []
        if map_requested and not dry_run and not extract_result.get("fatal_error"):
            competitors = list(dict.fromkeys(s["name"].partition(" — ")[0] for s in sources))
            similar, similar_cost, similar_errors = _find_similar_voices(
                storage, client_id, stream, run_id, competitors, lookback_days, config)
            comment["summary"].append(
                f"{run_comment.MAP_TRIGGER}: posting activity and {SIMILAR_PER_COMPETITOR} similar people per "
                f"competitor were added to the Competitor_map tab")
            map_rows = map_rows + [
                {"competitor": comp, "channel": "Similar voice", "posts": "", "per_week": "", "days": "",
                 "capped": False, "similar_person": p["name"], "why_similar": p["why"], "reach": p["reach"] or "",
                 "source_url": p["url"]}
                for comp, people in similar.items() for p in people]
        if show_map and not dry_run:
            storage.save_competitor_map(client_id, run_id, lookback_days, map_rows)
        videos = media.video_candidates(dedup_result["new_items"], extract_result.get("verified_urls"))

        score_result = score.run_scoring_phase(
            storage, client_id, stream,
            scoring_direction=config.get("scoring_direction", "novelty"),
            dry_run=dry_run,
        )

        run_coverage = {
            "sources_checked": len(sources),
            "sources_returning_zero": fetch_result["sources_returning_zero"],
            "items_collected": len(fetch_result["items"]),
            "findings_extracted": extract_result["signals_extracted"],
            "ai_failed": extract_result.get("failed", 0), "ai_error": extract_result.get("first_error"),
        }
        digest_usage: dict = {}
        digest_topics: dict = {}
        digest = synthesis.run_synthesis_phase(
            config, storage, client_id, run_id, stream,
            run_coverage=run_coverage, dry_run=dry_run, usage_out=digest_usage, topics_out=digest_topics,
        )
        cost_parts = []
        if extract_result.get("est_cost"):
            cost_parts.append((f"Anthropic extraction ({extract_result.get('model')}, "
                               f"{extract_result.get('llm_calls', 0)} calls)", extract_result["est_cost"]))
        if similar_cost:
            cost_parts.append((f"Anthropic similar-people search ({SIMILAR_PER_COMPETITOR} per competitor)", similar_cost))
        if comment.get("cost"):
            cost_parts.append((f"Anthropic run comment ({comment['model']})", comment["cost"]))
        if digest_usage.get("cost"):
            cost_parts.append((f"Anthropic digest ({digest_usage['model']})", digest_usage["cost"]))
        for tool, usd in fetch_result.get("apify_by_tool", {}).items():
            cost_parts.append((tool, usd))
        cost_breakdown = " | ".join(f"{name}: ${usd:.4f}" for name, usd in cost_parts) or "no paid calls"

        stats = {
            "sources_checked": len(sources),
            "collected": len(fetch_result["items"]),
            "new_after_dedup": len(dedup_result["new_items"]),
            "ai_findings": extract_result["signals_extracted"],
            "quote_verified": extract_result["quote_verified_count"],
            # This run's own contribution to the digest — the digest itself
            # covers a rolling window across many runs, so this is "how many
            # of THIS run's findings qualified," not the digest's total count.
            "in_digest": extract_result["quote_verified_count"],
            "zero_result_sources": fetch_result["sources_returning_zero"],
            "errors": errors,
            "est_cost": round(sum(usd for _, usd in cost_parts), 4),
            "cost_breakdown": cost_breakdown,
            "status": "dry_run" if dry_run else "done",
        }
    except Exception:
        # Always close out the Runs row, even on failure or in dry-run mode —
        # otherwise it's stuck at status="running" forever, which later looks
        # indistinguishable from a crashed run that never got investigated.
        storage.finish_run(run_id, {"errors": errors + 1, "status": "failed"})
        raise

    storage.finish_run(run_id, stats)
    usage_log.flush(storage)
    if not dry_run:
        cleanup_stats = storage.cleanup_old_data(retention_days=30)
        if cleanup_stats.get("collected_deleted") or cleanup_stats.get("ai_log_deleted"):
            logger.info(f"Retention cleanup: {cleanup_stats}")

    logger.info(
        f"Run {run_id} complete — collected={stats['collected']}, "
        f"new={stats['new_after_dedup']}, findings={stats['ai_findings']}, "
        f"verified={stats['quote_verified']}, errors={stats['errors']}"
    )

    notices = list(fetch_result.get("notices", []))
    if extract_result.get("failed"):
        notices.insert(0, f"The AI could not analyse {extract_result['failed']} post(s): "
                          f"{extract_result.get('first_error')} Nothing was lost — the posts are kept and will "
                          f"be analysed the next time you run them. Each one is listed in the AI_log tab.")
    if similar_errors:
        notices.append(f"The search for similar people failed: {similar_errors[0]}")
    return {"run_id": run_id, "digest": digest, "stats": stats, "notices": notices,
            "competitor_map": map_text if show_map else "", "similar_voices": similar, "video_candidates": videos,
            "comment_summary": comment["summary"], "comment_warnings": comment["warnings"],
            "topics_by_entity": digest_topics}


def run_headline_scan(
    stream: str,
    inputs: List[str],
    mode: str,
    lookback_days: int,
    started_by: str,
    client_id: str = DEFAULT_CLIENT_ID,
    storage: Optional[StorageBackend] = None,
    config: Optional[dict] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> Dict:
    """
    Phase 1 of the headline-approval flow (Trend research): Fetch + Dedup only —
    no AI call is made, so this costs nothing. Returns the run_id and the scraped
    items (title/source/url/published_date) for the screen to show for approval;
    nothing is scored or synthesized until run_digest_from_headlines() is called
    for the approved subset.

    Returns: {"run_id": str, "headlines": List[Dict], "notices": List[str], "sources_checked": int}
    """
    storage = storage or _default_storage()
    storage.init()

    config = _config_with_rules(storage, client_id, stream, lookback_days, config)
    sources = _resolve_sources(storage, client_id, stream, inputs, mode)
    inputs_used = [s["feed_url"] for s in sources]

    run_id = storage.start_run(client_id, stream, started_by, inputs_used, mode)
    logger.info(f"Headline scan {run_id} started — stream={stream}, mode={mode}, sources={len(sources)}")

    fetch_result = fetch.run_fetch_phase(
        sources, stream,
        request_timeout_seconds=config.get("request_timeout_seconds", 30),
        limit=limit, dry_run=dry_run, lookback_days=lookback_days,
    )
    fetch_result["items"] = _within_lookback(fetch_result["items"], lookback_days)

    dedup_result = dedup.run_dedup_phase(
        storage, client_id, stream, run_id, fetch_result["items"], dry_run=dry_run,
        near_threshold=config.get("near_duplicate_threshold", 0.8),
    )
    if not dry_run:
        storage.save_headlines(client_id, run_id, dedup_result["new_items"])

    return {
        "run_id": run_id,
        "headlines": dedup_result["new_items"],
        "notices": fetch_result.get("notices", []),
        "sources_checked": len(sources),
    }


def run_digest_from_headlines(
    run_id: str,
    approved_items: List[Dict],
    stream: str,
    lookback_days: int,
    started_by: str,
    client_id: str = DEFAULT_CLIENT_ID,
    storage: Optional[StorageBackend] = None,
    config: Optional[dict] = None,
    dry_run: bool = False,
) -> Dict:
    """
    Phase 2 of the headline-approval flow: Extract -> Score -> Synthesize on just
    the approved items (already fetched by run_headline_scan, not re-fetched),
    then closes out the Runs row that run_headline_scan opened. Mirrors the
    second half of run_stream().

    Returns: {"run_id": str, "digest": str, "stats": dict}
    """
    storage = storage or _default_storage()
    config = _config_with_rules(storage, client_id, stream, lookback_days, config)

    for item in approved_items:
        if item.get("source_url"):
            storage.update_headline_status(run_id, item["source_url"], "approved")

    try:
        extract_result = extract.run_extraction_phase(
            config, storage, client_id, run_id, approved_items, dry_run=dry_run,
        )
        score.run_scoring_phase(
            storage, client_id, stream,
            scoring_direction=config.get("scoring_direction", "novelty"),
            dry_run=dry_run,
        )
        run_coverage = {
            "sources_checked": len(approved_items),
            "items_collected": len(approved_items),
            "findings_extracted": extract_result["signals_extracted"],
            "ai_failed": extract_result.get("failed", 0), "ai_error": extract_result.get("first_error"),
        }
        digest_usage: dict = {}
        digest = synthesis.run_synthesis_phase(
            config, storage, client_id, run_id, stream,
            run_coverage=run_coverage, dry_run=dry_run, usage_out=digest_usage,
        )
        cost_parts = []
        if extract_result.get("est_cost"):
            cost_parts.append((f"Anthropic extraction ({extract_result.get('model')}, "
                               f"{extract_result.get('llm_calls', 0)} calls)", extract_result["est_cost"]))
        if digest_usage.get("cost"):
            cost_parts.append((f"Anthropic digest ({digest_usage['model']})", digest_usage["cost"]))
        cost_breakdown = " | ".join(f"{name}: ${usd:.4f}" for name, usd in cost_parts) or "no paid calls"

        stats = {
            "sources_checked": len(approved_items),
            "collected": len(approved_items),
            "new_after_dedup": len(approved_items),
            "ai_findings": extract_result["signals_extracted"],
            "quote_verified": extract_result["quote_verified_count"],
            "in_digest": extract_result["quote_verified_count"],
            "errors": extract_result["errors"],
            "est_cost": round(sum(usd for _, usd in cost_parts), 4),
            "cost_breakdown": cost_breakdown,
            "status": "dry_run" if dry_run else "done",
        }
    except Exception:
        storage.finish_run(run_id, {"errors": 1, "status": "failed"})
        raise

    storage.finish_run(run_id, stats)
    usage_log.flush(storage)
    if not dry_run:
        cleanup_stats = storage.cleanup_old_data(retention_days=30)
        if cleanup_stats.get("collected_deleted") or cleanup_stats.get("ai_log_deleted"):
            logger.info(f"Retention cleanup: {cleanup_stats}")

    return {"run_id": run_id, "digest": digest, "stats": stats}


def get_run_details(run_id: str, storage: Optional[StorageBackend] = None) -> Dict:
    """
    Everything a Results/Troubleshooting screen needs about one past run:
    the funnel (collected → new → AI found something → quote check passed →
    in digest), per-source counts, the AI's verdict on each item, the
    findings, and the digest. The Sheets-specific aggregation lives in
    storage/sheets_backend.py — this just calls it.
    """
    storage = storage or _default_storage()
    return storage.get_run_details(run_id)



def run_video(candidate: Dict, transcript: str, stream: str, lookback_days: int, started_by: str,
              client_id: str = DEFAULT_CLIENT_ID, storage: Optional[StorageBackend] = None,
              config: Optional[dict] = None) -> Dict:
    """
    An approved video's transcript goes through the same extraction, quote check and digest as any
    other text, filed under the post's author. -> {"run_id", "digest", "stats"}. The digest covers the
    whole window again, now including what was said in the video.
    """
    storage = storage or _default_storage()
    storage.init()
    config = _config_with_rules(storage, client_id, stream, lookback_days, config)
    run_id = storage.start_run(client_id, stream, started_by, [candidate["video_url"]], "video")
    try:
        item = {
            "title": f"Video by {candidate['author']}", "source_url": candidate["video_url"],
            "text": transcript, "source_name": f"{candidate['author']} — video",
            "author": candidate["author"], "published_date": candidate.get("published_date") or "",
            "text_complete": True, "tool": "Apify: video transcript",
        }
        extract_result = extract.run_extraction_phase(config, storage, client_id, run_id, [item])
        score.run_scoring_phase(storage, client_id, stream,
                                scoring_direction=config.get("scoring_direction", "novelty"))
        digest_usage: dict = {}
        digest = synthesis.run_synthesis_phase(
            config, storage, client_id, run_id, stream,
            run_coverage={"sources_checked": 1, "items_collected": 1,
                          "findings_extracted": extract_result["signals_extracted"]},
            usage_out=digest_usage)
        stats = {"sources_checked": 1, "collected": 1, "new_after_dedup": 1,
                 "ai_findings": extract_result["signals_extracted"],
                 "quote_verified": extract_result["quote_verified_count"],
                 "in_digest": extract_result["quote_verified_count"], "errors": extract_result["errors"],
                 "est_cost": round(extract_result.get("est_cost", 0.0) + digest_usage.get("cost", 0.0), 4),
                 "status": "done"}
    except Exception:
        storage.finish_run(run_id, {"errors": 1, "status": "failed"})
        raise
    storage.finish_run(run_id, stats)
    return {"run_id": run_id, "digest": digest, "stats": stats}


def run_collect_only(
    sources: List[Dict],
    stream: str,
    lookback_days: int,
    started_by: str,
    client_id: str = DEFAULT_CLIENT_ID,
    storage: Optional[StorageBackend] = None,
) -> Dict:
    """
    One-off: fetch + dedup only, then stop — no extraction, scoring, digest, or AI call of any
    kind. Real (not dry-run) Apify scraping, so it costs real Apify money and writes genuinely new
    items to the Collected tab. Used when spending Apify credit is the goal and Anthropic calls
    must be avoided entirely (e.g. the account is over its usage limit).

    `sources` are already-resolved {"name", "feed_url", "kind"} dicts — see
    adapters/feed_discovery.sources_for_competitor — not raw "Label: url" text.

    Returns: {"run_id", "stats", "notices", "collected_count"}
    """
    storage = storage or _default_storage()
    storage.init()

    inputs_used = [s["feed_url"] for s in sources]
    run_id = storage.start_run(client_id, stream, started_by, inputs_used, "typed_only")
    logger.info(f"Collect-only run {run_id} started — stream={stream}, sources={len(sources)}, no AI")

    try:
        fetch_result = fetch.run_fetch_phase(sources, stream, dry_run=False, lookback_days=lookback_days)
        items = _within_lookback(fetch_result["items"], lookback_days)
        dedup_result = dedup.run_dedup_phase(storage, client_id, stream, run_id, items, dry_run=False)

        apify_cost = round(sum(fetch_result.get("apify_by_tool", {}).values()), 4)
        cost_breakdown = " | ".join(f"{tool}: ${usd:.4f}" for tool, usd in
                                    fetch_result.get("apify_by_tool", {}).items()) or "no paid calls"
        stats = {
            "sources_checked": len(sources), "collected": len(items),
            "new_after_dedup": len(dedup_result["new_items"]),
            "ai_findings": 0, "quote_verified": 0, "in_digest": 0,
            "zero_result_sources": fetch_result["sources_returning_zero"], "errors": 0,
            "est_cost": apify_cost, "cost_breakdown": cost_breakdown, "status": "collected_only",
        }
    except Exception:
        storage.finish_run(run_id, {"errors": 1, "status": "failed"})
        raise

    storage.finish_run(run_id, stats)
    logger.info(f"Collect-only run {run_id} complete — collected={stats['collected']}, "
               f"new={stats['new_after_dedup']}, cost=${stats['est_cost']}")
    return {"run_id": run_id, "stats": stats, "notices": fetch_result.get("notices", []),
            "collected_count": len(dedup_result["new_items"])}


def run_from_collected(
    stream: str,
    max_items: int,
    lookback_days: int,
    started_by: str,
    client_id: str = DEFAULT_CLIENT_ID,
    storage: Optional[StorageBackend] = None,
    config: Optional[dict] = None,
    dry_run: bool = False,
) -> Dict:
    """
    Process items already in the Collected tab through extraction → scoring → synthesis → digest
    pipeline, skipping fetch and dedup entirely. Used when you have raw-collected items that need
    AI analysis and competitor mapping.

    Reads the first `max_items` rows from the Collected tab, processes them through AI extraction
    and novelty scoring, synthesizes a digest, and builds the competitor map (posting cadence +
    similar voices).

    Returns: {"run_id": str, "digest": str, "stats": dict, "competitor_map": str, "similar_voices": dict}
    """
    storage = storage or _default_storage()
    storage.init()
    config = _config_with_rules(storage, client_id, stream, lookback_days, config)

    # Read unextracted items from Collected tab
    collected_items = storage.get_collected_items(client_id, stream, limit=max_items)
    logger.info(f"Processing {len(collected_items)} items from Collected tab")

    run_id = storage.start_run(client_id, stream, started_by, [f"from_collected: {len(collected_items)}"], "from_collected")
    errors = 0

    try:
        # Extract
        extract_result = extract.run_extraction_phase(
            config, storage, client_id, run_id, collected_items, dry_run=dry_run,
        )
        errors += extract_result["errors"]

        # Score
        score.run_scoring_phase(
            storage, client_id, stream,
            scoring_direction=config.get("scoring_direction", "novelty"),
            dry_run=dry_run,
        )

        # Build map (posting cadence)
        from adapters.apify_linkedin_adapter import DEFAULT_MAX_POSTS
        competitors = list(dict.fromkeys(
            item.get("source_name", "").partition(" — ")[0] for item in collected_items
            if item.get("source_name")
        ))
        map_rows = competitor_map.build_map([], collected_items, lookback_days, DEFAULT_MAX_POSTS)

        # Find similar voices for each competitor
        similar, similar_cost, similar_errors = {}, 0.0, []
        saved = {r.get("competitor"): r for r in storage.get_saved_items(client_id, stream, active_only=False)}
        followed = [n for n in saved if n] + list(competitors)
        findings = [f for f in storage.get_findings_for_digest(client_id, stream, days_back=lookback_days)
                    if f.get("run_id") == run_id]

        if not dry_run and not extract_result.get("fatal_error"):
            from pipeline import suggestions
            out, usage, seen = {}, {}, list(followed)
            for name in competitors:
                if name:
                    about = [f"Positioning: {saved[name].get('positioning')}" for _ in [0] if saved.get(name, {}).get("positioning")]
                    about += [f"Keywords: {saved[name].get('keywords')}" for _ in [0] if saved.get(name, {}).get("keywords")]
                    about += [f"- {f['summary']}" for f in findings if f.get("entity") == name][:6]
                    people = suggestions.similar_voices(name, "\n".join(about), config.get("relevance_ruleset", ""), seen,
                                                        n=SIMILAR_PER_COMPETITOR, usage_out=usage)
                    out[name] = people
                    seen += [p["name"] for p in people]
            similar = out
            if usage:
                from pipeline.costs import llm_cost_usd
                similar_cost = (llm_cost_usd(config, suggestions.SUGGEST_MODEL, usage.get("input", 0), usage.get("output", 0))
                                + WEB_SEARCH_USD * usage.get("searches", 0))
            if usage.get("error"):
                similar_errors = [usage["error"]]

        # Add similar voices to map
        if similar and not dry_run:
            map_rows = map_rows + [
                {"competitor": comp, "channel": "Similar voice", "posts": "", "per_week": "", "days": "",
                 "capped": False, "similar_person": p["name"], "why_similar": p["why"], "reach": p["reach"] or "",
                 "source_url": p["url"]}
                for comp, people in similar.items() for p in people]

        if not dry_run:
            storage.save_competitor_map(client_id, run_id, lookback_days, map_rows)

        # Synthesize digest
        run_coverage = {
            "sources_checked": len(collected_items),
            "items_collected": len(collected_items),
            "findings_extracted": extract_result["signals_extracted"],
            "ai_failed": extract_result.get("failed", 0), "ai_error": extract_result.get("first_error"),
        }
        digest_usage: dict = {}
        digest_topics: dict = {}
        digest = synthesis.run_synthesis_phase(
            config, storage, client_id, run_id, stream,
            run_coverage=run_coverage, dry_run=dry_run, usage_out=digest_usage, topics_out=digest_topics,
        )

        # Cost breakdown
        cost_parts = []
        if extract_result.get("est_cost"):
            cost_parts.append((f"Anthropic extraction ({extract_result.get('model')}, "
                               f"{extract_result.get('llm_calls', 0)} calls)", extract_result["est_cost"]))
        if similar_cost:
            cost_parts.append((f"Anthropic similar-people search ({SIMILAR_PER_COMPETITOR} per competitor)", similar_cost))
        if digest_usage.get("cost"):
            cost_parts.append((f"Anthropic digest ({digest_usage['model']})", digest_usage["cost"]))
        cost_breakdown = " | ".join(f"{name}: ${usd:.4f}" for name, usd in cost_parts) or "no paid calls"

        map_text = competitor_map.format_map(map_rows, lookback_days, DEFAULT_MAX_POSTS)

        stats = {
            "items_processed": len(collected_items),
            "ai_findings": extract_result["signals_extracted"],
            "quote_verified": extract_result["quote_verified_count"],
            "in_digest": extract_result["quote_verified_count"],
            "errors": errors,
            "est_cost": round(sum(usd for _, usd in cost_parts), 4),
            "cost_breakdown": cost_breakdown,
            "status": "dry_run" if dry_run else "done",
        }
    except Exception:
        storage.finish_run(run_id, {"errors": errors + 1, "status": "failed"})
        raise

    storage.finish_run(run_id, stats)
    usage_log.flush(storage)
    if not dry_run:
        cleanup_stats = storage.cleanup_old_data(retention_days=30)
        if cleanup_stats.get("collected_deleted") or cleanup_stats.get("ai_log_deleted"):
            logger.info(f"Retention cleanup: {cleanup_stats}")

    logger.info(f"From-collected run {run_id} complete — processed={stats['items_processed']}, "
                f"findings={stats['ai_findings']}, verified={stats['quote_verified']}")

    notices = []
    if extract_result.get("failed"):
        notices.insert(0, f"The AI could not analyse {extract_result['failed']} post(s): "
                          f"{extract_result.get('first_error')} Nothing was lost — the posts are kept and will "
                          f"be analysed the next time you run them. Each one is listed in the AI_log tab.")
    if similar_errors:
        notices.append(f"The search for similar people failed: {similar_errors[0]}")

    return {
        "run_id": run_id, "digest": digest, "stats": stats, "notices": notices,
        "competitor_map": map_text, "similar_voices": similar, "topics_by_entity": digest_topics
    }
