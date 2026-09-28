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

from pipeline import fetch, dedup, extract, score, synthesis
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
            name = row.get("competitor") or row["value"]
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


def _within_lookback(items: List[Dict], lookback_days: int) -> List[Dict]:
    """Drop items published before the look-back window (undated items are kept)."""
    from datetime import datetime, timedelta, timezone
    from dateutil import parser as date_parser
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    kept = []
    for item in items:
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
) -> Dict:
    """
    Run one stream end to end: collect, remove repeats, ask the AI, check its
    quotes, score novelty, write the digest.

    Returns: {"run_id": str, "digest": str, "stats": dict}
    """
    from config import load_local_config  # local import avoids a cycle with main.py's CLI use

    storage = storage or _default_storage()
    storage.init()

    config = {**(config or load_local_config(stream)), "digest_window_days": lookback_days}

    sources = _resolve_sources(storage, client_id, stream, inputs, mode)
    inputs_used = [s["feed_url"] for s in sources]

    # General rules apply to every stream; each stream adds its own on top.
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

    run_id = storage.start_run(client_id, stream, started_by, inputs_used, mode)
    logger.info(f"Run {run_id} started — stream={stream}, mode={mode}, sources={len(sources)}")

    errors = 0
    try:
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

        extract_result = extract.run_extraction_phase(
            config, storage, client_id, run_id, dedup_result["new_items"],
            limit=limit, dry_run=dry_run,
        )
        errors += extract_result["errors"]

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
    if not dry_run:
        cleanup_stats = storage.cleanup_old_data(retention_days=30)
        if cleanup_stats.get("collected_deleted") or cleanup_stats.get("ai_log_deleted"):
            logger.info(f"Retention cleanup: {cleanup_stats}")

    logger.info(
        f"Run {run_id} complete — collected={stats['collected']}, "
        f"new={stats['new_after_dedup']}, findings={stats['ai_findings']}, "
        f"verified={stats['quote_verified']}, errors={stats['errors']}"
    )

    return {"run_id": run_id, "digest": digest, "stats": stats, "notices": fetch_result.get("notices", [])}


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
