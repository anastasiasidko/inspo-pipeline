"""
Phase 5: Synthesis — Generate the digest

Input: verified findings (via the storage backend)
Output: digest text, saved through the storage backend (and mirrored to a
local digest.md for convenience reading from the terminal)

Structure:
- # Competitor Monitoring — [date range]
- ## Top conclusions
  - [1 sentence, actionable] (evidence: N signals)
- ## By entity
  - ### [Company X]
    - [signal summary] — [source link]
- ## Coverage
  - Sources checked: N
  - Items collected: N
  - Findings extracted: N
  - Sources returning zero: [list]

Key points:
- Every conclusion traces to findings (nothing unsupported)
- Coverage block makes broken feeds visible
- LLM call: synthesize conclusions from grouped findings (ONE call, cheap)
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from storage.backend import StorageBackend
from pipeline import usage_log
from pipeline.costs import llm_cost_usd

logger = logging.getLogger(__name__)


def _group_by_entity(findings: List[Dict]) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = defaultdict(list)
    for finding in findings:
        grouped[finding["entity"]].append(finding)
    return grouped


def build_synthesis_prompt(grouped_findings: Dict[str, List[Dict]], rules: str = "") -> str:
    """
    Build LLM prompt to synthesize conclusions from grouped findings.

    Args:
        grouped_findings: {entity: [finding dicts]}

    Returns: Prompt as string
    """
    lines = [
        "You are writing a short weekly digest for a consultant who builds thought "
        "leadership (personal brand, business growth for passion-driven professionals, "
        "wellbeing and burnout in careers). Below are verified findings from monitoring "
        "other thought leaders, grouped by person.",
        "",
        "For each person with findings, write one short descriptive paragraph of what "
        "they said in this period, strictly from the findings below. Describe, do not "
        "interpret: no guessing at emotions or motives, no advice, and no implications "
        "for marketers, audiences or anyone else. If something is not in the findings, "
        "leave it out.",
        "",
        "RULES (follow these, especially how to write):",
        rules.strip() or "(none)",
        "",
        "FINDINGS BY PERSON:",
    ]

    for entity, findings in grouped_findings.items():
        lines.append(f"\n## {entity}")
        for f in findings:
            lines.append(
                f"- [{f['signal_type']}] {f['summary']} "
                f"(confidence: {f['confidence']}, source: {f['url']})"
            )

    lines.append(
        "\n\nReturn a JSON object mapping each person's name to a list with one "
        "paragraph string, e.g. {\"Person X\": [\"She argues ...\"]}. "
        "Return JSON only, no other text."
    )

    return "\n".join(lines)


def _call_llm_for_conclusions(grouped_findings: Dict[str, List[Dict]], model: str,
                               config: Optional[dict] = None, usage_out: Optional[dict] = None) -> Dict[str, List[str]]:
    """Call the LLM to synthesize conclusions. Falls back to a flat listing on failure."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — falling back to un-synthesized finding summaries")
        return {
            entity: [f["summary"] for f in findings]
            for entity, findings in grouped_findings.items()
        }

    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_synthesis_prompt(grouped_findings, (config or {}).get("relevance_ruleset", ""))

    try:
        response = client.messages.create(
            model=model,
            max_tokens=8000,  # room for the model's own reasoning as well as the answer
            messages=[{"role": "user", "content": prompt}],
        )
        usage_log.record_response("Write digest", model, response)
        usage = getattr(response, "usage", None)
        if usage_out is not None and usage is not None:
            usage_out.update(model=model, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                             cost=llm_cost_usd(config or {}, model, usage.input_tokens, usage.output_tokens))
        raw_text = "".join(block.text for block in response.content if hasattr(block, "text"))
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start:end + 1])
        return json.loads(raw_text)
    except Exception as e:
        logger.error(f"Synthesis LLM call failed, falling back to raw summaries: {e}")
        return {
            entity: [f["summary"] for f in findings]
            for entity, findings in grouped_findings.items()
        }


def _format_date_range(days_back: int) -> str:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)
    return f"{start.strftime('%d %b')} to {end.strftime('%d %b %Y')}"


def run_synthesis_phase(config: dict, storage: StorageBackend, client_id: str, run_id: str,
                         stream: str,
                         run_coverage: Optional[Dict] = None,
                         dry_run: bool = False,
                         usage_out: Optional[dict] = None) -> str:
    """
    Generate the digest from verified findings in the window, save it through
    the storage backend, and mirror it to a local file for convenience.

    Args:
        config: digest_window_days, extraction_model, digest_path
        storage: storage backend
        client_id, run_id, stream: this run's identity
        run_coverage: coverage info accumulated by earlier phases this run —
            {"sources_checked", "sources_returning_zero", "items_collected",
            "findings_extracted"}. Sheets has no cheap aggregate COUNT(*), so
            coverage numbers come from what the earlier phases already counted,
            not a fresh query.
        dry_run: If True, return report but don't write/save it

    Returns:
        str: digest text
    """
    days_back = config.get("digest_window_days", 14)
    model = config.get("synthesis_model") or config.get("extraction_model", "claude-haiku-4-5-20251001")

    findings = storage.get_findings_for_digest(client_id, stream, days_back=days_back)
    grouped = _group_by_entity(findings)

    date_range = _format_date_range(days_back)
    lines = [f"Digest, {date_range}", ""]

    if not findings and (run_coverage or {}).get("ai_failed"):
        lines += [f"Nothing to report yet: the AI could not analyse {run_coverage['ai_failed']} collected post(s) "
                  f"({run_coverage.get('ai_error')}). They will be analysed on the next run.", ""]
    elif not findings:
        lines += ["No new content found from the selected channels in this period.", ""]
    else:
        conclusions = _call_llm_for_conclusions(grouped, model, config, usage_out) if not dry_run else {}
        if dry_run:
            lines += ["[DRY RUN] Digest text skipped.", ""]
        for entity, paragraphs in conclusions.items():
            urls = list(dict.fromkeys(f["url"] for f in grouped.get(entity, [])))
            lines += [entity, " ".join(paragraphs), "Sources: " + ", ".join(urls), ""]

    run_coverage = run_coverage or {}
    quiet = run_coverage.get("sources_returning_zero", [])
    coverage = (f"Channels checked: {run_coverage.get('sources_checked', 'n/a')}. "
                f"New items collected: {run_coverage.get('items_collected', 'n/a')}.")
    if quiet:
        coverage += " Nothing new from: " + ", ".join(quiet) + "."
    lines.append(coverage)

    report = "\n".join(lines) + "\n"

    has_results = any(f.get("run_id") == run_id for f in findings)
    if not dry_run and not has_results:
        logger.info("No verified findings from this run — digest not saved")
    elif not dry_run:
        storage.save_digest(client_id, run_id, stream, date_range, report)
        digest_path = config.get("digest_path")
        if digest_path:
            with open(digest_path, "w") as f:
                f.write(report)
            logger.info(f"Digest saved and mirrored to {digest_path}")
        else:
            logger.info("Digest saved")
    else:
        logger.info("[DRY RUN] Digest built but not saved")

    return report
