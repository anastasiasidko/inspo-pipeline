"""
Phase 4: Score findings

Input: findings with no novelty score yet (via the storage backend)
Output: novelty scores, written back in one batched update

Scoring (v1 — simple):
- confidence: already from extraction (model's judgment)
- novelty: has this entity + signal_type appeared in last 30 days? (0-1, high if novel)

Note: For social_listening v2, direction inverts (recurrence matters more).
`scoring_direction` is a config field so that flip needs no restructuring.
"""

import logging
from typing import List, Tuple

from storage.backend import StorageBackend

logger = logging.getLogger(__name__)

HIGH_NOVELTY = 0.9
LOW_NOVELTY = 0.2


def _days_ago_iso(days_back: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()


def compute_novelty_score(storage: StorageBackend, client_id: str, stream: str,
                           entity: str, signal_type: str,
                           lookback_days: int = 30,
                           scoring_direction: str = "novelty",
                           exclude_finding_id=None) -> float:
    """
    Compute novelty score: 0-1.

    scoring_direction="novelty" (default, e.g. competitor monitoring):
        high if this entity+signal_type hasn't appeared recently, low if it has.
    scoring_direction="recurrence" (e.g. social listening):
        inverted — repetition IS the signal, so a repeat scores high.

    Returns: float 0.0-1.0
    """
    since_iso = _days_ago_iso(lookback_days)
    recent_count = storage.count_recent_findings(
        client_id, stream, entity, signal_type, since_iso, exclude_finding_id=exclude_finding_id
    )
    seen_recently = recent_count > 0

    if scoring_direction == "recurrence":
        return HIGH_NOVELTY if seen_recently else LOW_NOVELTY
    return LOW_NOVELTY if seen_recently else HIGH_NOVELTY


def run_scoring_phase(storage: StorageBackend, client_id: str, stream: str,
                       lookback_days: int = 30,
                       scoring_direction: str = "novelty",
                       dry_run: bool = False) -> dict:
    """
    Score every finding that doesn't have a novelty score yet, then write all
    updates back in one batched call.

    Returns:
        dict: {signals_scored}
    """
    unscored = storage.get_findings_without_novelty(client_id, stream)
    updates: List[Tuple[str, float]] = []

    for finding in unscored:
        score = compute_novelty_score(
            storage, client_id, stream,
            entity=finding["entity"],
            signal_type=finding["signal_type"],
            lookback_days=lookback_days,
            scoring_direction=scoring_direction,
            exclude_finding_id=finding["finding_id"],
        )
        if dry_run:
            logger.info(
                f"[DRY RUN] Would score finding {finding['finding_id']} "
                f"({finding['entity']}/{finding['signal_type']}) = {score}"
            )
        else:
            updates.append((finding["finding_id"], score))

    if updates:
        storage.update_findings_novelty_batch(updates)

    signals_scored = len(unscored)
    logger.info(f"Scoring phase complete — {signals_scored} signals scored")
    return {"signals_scored": signals_scored}
