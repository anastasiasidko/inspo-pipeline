"""
Phase 3: Extract signals from articles via LLM

Input: raw_items (new articles only, unless --force-extract)
Output: findings, via the storage backend (with quote verification)

Key logic:
- LLM call: article text + relevance ruleset → signal or empty
- Verbatim quote extraction (evidence_quote)
- Quote verification: exact substring check against source text (anti-hallucination gate)
- Cost control: --limit N caps articles processed, raw_items caching means
  re-running extraction never re-fetches

Error handling:
- One retry on malformed JSON (asks the model to return JSON only, nothing else)
- A finding whose quote doesn't verify is stored with quote_verified=False and
  excluded from the digest, never dropped silently
- A single article's failure never kills the run

Storage note: findings and AI_log entries accumulate in memory across the loop
and are written with ONE batched call each at the end, not one write per
article — same rate-limit discipline as the dedup phase. Every article gets an
AI_log row, including ones where the model found nothing (verdict="nothing"),
per the brief's audit requirement.
"""

import json
import logging
import os
from typing import Optional, List, Dict

from storage.backend import StorageBackend
from pipeline.costs import llm_cost_usd

logger = logging.getLogger(__name__)

MAX_JSON_RETRIES = 1


def verify_quote_substring(quote: str, text: str) -> bool:
    """
    Verify that quote appears as exact substring in text.
    This is the anti-hallucination gate.

    Returns: True if quote found, False otherwise
    """
    if not quote or not text:
        return False
    return _normalize_for_match(quote) in _normalize_for_match(text)


_QUOTE_MAP = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2026": "..."})


def _normalize_for_match(s: str) -> str:
    """Ignore only formatting: whitespace/paragraph breaks and curly-vs-straight
    quote marks. The words themselves must still match exactly, so an invented
    quote still fails."""
    return " ".join(s.translate(_QUOTE_MAP).split())


def build_extraction_prompt(article_text: str, source_name: str, source_url: str,
                             published_date: str, relevance_ruleset: str,
                             signal_types: List[str], author: str = "") -> str:
    """
    Build the extraction prompt for the LLM.

    Returns: Complete prompt as string
    """
    types_list = ", ".join(signal_types)
    other_hint = (
        "  - if \"other\", include a one-sentence \"note\" field explaining what "
        "it is and why it seemed to matter\n"
        if "other" in signal_types
        else ""
    )

    return f"""You are analyzing an article about a company we monitor competitively. Your job is to decide whether it contains anything meaningful, and if so, extract it as structured findings.

WHAT COUNTS AS MEANINGFUL:
{relevance_ruleset.strip()}

Do not flag: routine housekeeping (typo fixes, minor formatting), content with no company-specific information, or anything you can't support with a direct quote from the text.

ARTICLE:
Author: {author}
Source: {source_name}
URL: {source_url}
Published: {published_date}
Text:
{article_text}

FOR EACH FINDING, RETURN AN OBJECT WITH:
- entity: the person or publication that WROTE this text (the author). Never a company or product that is merely mentioned
- signal_type: one of [{types_list}]
{other_hint}- summary: one plain sentence saying only what the text says — no interpretation of emotions or motives, no advice.
- evidence_quote: ONE continuous passage copied WORD-FOR-WORD from the article text above — do not paraphrase, and never join separate sentences
- confidence: 0-1, how sure you are this is a real, meaningful finding
- themes: 2-4 short tags describing the topic (array of strings)
- value_claims: any claims of value/benefit made in the text relevant to this finding (array of strings, can be empty)
- target_audience: who this finding seems aimed at, if apparent (array of strings, can be empty)
- terminology: notable product/positioning terminology used (array of strings, can be empty)

RULES:
- If the article contains nothing meaningful, return an empty list. Do not invent a finding to be helpful — an empty result is a valid and expected outcome.
- evidence_quote must be an exact substring of the article text. If you cannot find a real quote supporting a finding, do not include that finding.
- Return a JSON array only. No preamble, no explanation outside the array structure above."""


def _extract_json_array(raw_text: str) -> Optional[List[Dict]]:
    """Try to parse a JSON array out of the model's raw response text."""
    raw_text = raw_text.strip()
    try:
        parsed = json.loads(raw_text)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        pass

    # Fallback: model wrapped the array in prose or a code fence — grab the
    # first '[' through the last ']' and try again.
    start = raw_text.find("[")
    end = raw_text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(raw_text[start:end + 1])
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            return None
    return None


def _call_llm(client, model: str, prompt: str) -> Dict:
    """Single call to the Anthropic API. Raises on API failure (caller catches).

    Returns {"text": str, "input_tokens": int, "output_tokens": int} so callers
    can track real spend instead of estimating it.
    """
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    usage = getattr(response, "usage", None)
    return {
        "text": text,
        "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
        "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
    }


def _extract_findings_with_retry(client, model: str, prompt: str, source_name: str) -> Dict:
    """Call the LLM and parse JSON, retrying once with a stricter instruction on failure.

    Returns {"findings": List[Dict], "raw_response": str, "input_tokens": int, "output_tokens": int}.
    """
    current_prompt = prompt
    total_input_tokens = 0
    total_output_tokens = 0
    last_raw_text = ""

    for attempt in range(MAX_JSON_RETRIES + 1):
        result = _call_llm(client, model, current_prompt)
        last_raw_text = result["text"]
        total_input_tokens += result["input_tokens"]
        total_output_tokens += result["output_tokens"]

        findings = _extract_json_array(last_raw_text)
        if findings is not None:
            return {
                "findings": findings, "raw_response": last_raw_text,
                "input_tokens": total_input_tokens, "output_tokens": total_output_tokens,
            }

        logger.warning(
            f"[{source_name}] Malformed JSON from model (attempt {attempt + 1}), retrying"
        )
        current_prompt = (
            prompt + "\n\nYour previous response was not valid JSON. "
            "Return ONLY a JSON array, with no other text."
        )

    logger.error(f"[{source_name}] Giving up on JSON parse after retry — skipping article")
    return {
        "findings": [], "raw_response": last_raw_text,
        "input_tokens": total_input_tokens, "output_tokens": total_output_tokens,
    }


def run_extraction_phase(config: dict, storage: StorageBackend, client_id: str, run_id: str,
                          raw_items: List[Dict],
                          limit: Optional[int] = None,
                          dry_run: bool = False,
                          force_extract: bool = False) -> Dict:
    """
    Extract structured findings from articles using LLM.

    Args:
        config: relevance_ruleset, signal_types, signal_category_map, extraction_model
        storage: storage backend
        client_id, run_id: this run's identity, stamped onto every finding/log row
        raw_items: newly collected items to process (each a dict from the fetch/
            dedup phases — has stream/source_url/source_name/text/etc.)
        limit: Cap articles to process (for testing)
        dry_run: If True, build and log the prompt but don't call the LLM or write
        force_extract: descoped for the Sheets backend (no cheap delete-by-item
            there yet) — logged and ignored rather than silently dropped

    Returns:
        dict: {articles_processed, signals_extracted, quote_verified_count,
               hallucinations_caught, empty_articles, errors, est_cost}
    """
    stats = {
        "articles_processed": 0,
        "signals_extracted": 0,
        "quote_verified_count": 0,
        "hallucinations_caught": 0,
        "empty_articles": 0,
        "errors": 0,
        "est_cost": 0.0,
    }

    items = list(raw_items)
    if limit is not None:
        items = items[:limit]

    if not items:
        logger.info("Extraction phase — no new items to process")
        return stats

    if force_extract:
        logger.warning(
            "--force-extract is not supported against the Sheets backend yet "
            "(no cheap delete-by-item) — ignoring and extracting normally"
        )

    relevance_ruleset = config["relevance_ruleset"]
    signal_types = config["signal_types"]
    signal_category_map = config.get("signal_category_map", {})
    model = config.get("extraction_model", "claude-haiku-4-5-20251001")

    client = None
    if not dry_run:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.error("ANTHROPIC_API_KEY not set — cannot run extraction (use --dry-run instead)")
            stats["errors"] += 1
            return stats
        client = anthropic.Anthropic(api_key=api_key)

    findings_to_save: List[Dict] = []
    ai_log_entries: List[Dict] = []

    for item in items:
        source_name = item["source_name"]
        source_url = item["source_url"]
        text = item["text"]

        stats["articles_processed"] += 1

        prompt = build_extraction_prompt(
            article_text=text,
            source_name=source_name,
            source_url=source_url,
            published_date=item["published_date"],
            relevance_ruleset=relevance_ruleset,
            signal_types=signal_types,
            author=item.get("author") or source_name.split(" — ")[0],
        )

        if dry_run:
            logger.info(f"[DRY RUN] Prompt built for {source_name} ({len(prompt)} chars) — no LLM call")
            continue

        try:
            result = _extract_findings_with_retry(client, model, prompt, source_name)
        except Exception as e:
            logger.error(f"[{source_name}] Extraction call failed: {e}")
            stats["errors"] += 1
            continue

        findings = result["findings"]
        stats["est_cost"] += llm_cost_usd(config, model, result["input_tokens"], result["output_tokens"])
        stats["model"] = model
        stats["llm_calls"] = stats.get("llm_calls", 0) + 1

        if not findings:
            stats["empty_articles"] += 1
            ai_log_entries.append({
                "item_url": source_url, "verdict": "nothing", "raw_ai_response": result["raw_response"],
            })
            continue

        finding_verdicts = []
        for finding in findings:
            try:
                signal_type = finding.get("signal_type", "other")
                if signal_type == "other" and not finding.get("note"):
                    logger.warning(
                        f"[{source_name}] 'other' finding missing required note — skipping"
                    )
                    continue

                quote = finding.get("evidence_quote", "")
                verified = verify_quote_substring(quote, text)
                if not verified:
                    stats["hallucinations_caught"] += 1
                    logger.warning(
                        f"[{source_name}] Quote failed verification, marking unverified: "
                        f"{quote[:80]!r}"
                    )

                findings_to_save.append({
                    # The author is known from the post itself, so use it rather than the AI's guess.
                    "entity": item.get("author") or (source_name.split(" — ")[0] if " — " in source_name else finding.get("entity", "unknown")),
                    "signal_type": signal_type,
                    "signal_category": signal_category_map.get(signal_type),
                    "summary": finding.get("summary", ""),
                    "evidence_quote": quote,
                    "confidence": finding.get("confidence", 0.0),
                    "quote_verified": verified,
                    "themes": finding.get("themes", []),
                    "value_claims": finding.get("value_claims", []),
                    "target_audience": finding.get("target_audience", []),
                    "terminology": finding.get("terminology", []),
                    "other_note": finding.get("note"),
                    "source": source_name,
                    "author": item.get("author"),
                    "url": source_url,
                    "date": item.get("published_date"),
                })
                stats["signals_extracted"] += 1
                if verified:
                    stats["quote_verified_count"] += 1
                finding_verdicts.append(f"{signal_type} (quote {'verified' if verified else 'FAILED'})")
            except Exception as e:
                logger.error(f"[{source_name}] Failed to process finding: {e}")
                stats["errors"] += 1

        ai_log_entries.append({
            "item_url": source_url,
            "verdict": f"{len(finding_verdicts)} finding(s): " + "; ".join(finding_verdicts),
            "raw_ai_response": result["raw_response"],
        })

    if not dry_run:
        if ai_log_entries:
            storage.save_ai_log_batch(run_id, ai_log_entries)
        if findings_to_save:
            storage.save_findings_batch(client_id, run_id, config["stream"], findings_to_save)

    logger.info(
        f"Extraction phase complete — {stats['articles_processed']} articles processed, "
        f"{stats['signals_extracted']} signals extracted "
        f"({stats['hallucinations_caught']} failed quote verification), "
        f"{stats['empty_articles']} articles with no findings, "
        f"est. cost ${stats['est_cost']:.4f}"
    )

    return stats
