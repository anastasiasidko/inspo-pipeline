"""
Phase 2: Dedup raw items

Input: Freshly fetched items (in-memory, from the fetch phase)
Output: Genuinely new items get stored via the storage backend

Dedup strategy (v1): exact URL match, then normalized title match, then
content hash match — checked in that order, first match wins.
Semantic dedup (same story, many outlets) is a v2 feature.

Storage note: against a Google Sheet, checking one item at a time would be one
API call per article. Instead, get_dedup_index() bulk-reads everything already
collected for this client+stream ONCE per run, and every item is checked
in-memory against that index. New items accumulate and get written with a
single batched save_collected_items() call, not one write per item.
"""

import logging
from typing import Dict, List

from storage.backend import StorageBackend, normalize_title

logger = logging.getLogger(__name__)


def run_dedup_phase(storage: StorageBackend, client_id: str, stream: str, run_id: str,
                     items: List[Dict], dry_run: bool = False,
                     near_threshold: float = 0.8) -> Dict:
    """
    Given freshly fetched items, drop the ones already collected and store the
    genuinely new ones (unless dry_run) with one batched write.

    Returns:
        dict: {new_items: List[Dict], duplicates_found: int}
    """
    index = storage.get_dedup_index(client_id, stream)

    new_items: List[Dict] = []
    duplicates_found = 0
    near_duplicates_found = 0

    for item in items:
        if index.contains(item):
            duplicates_found += 1
            continue
        similar_to = index.find_near_duplicate(item, near_threshold)
        if similar_to is not None:
            near_duplicates_found += 1
            logger.info(f"[{item.get('source_name')}] Near-duplicate of {similar_to} — skipped before the AI step")
            continue
        new_items.append(item)
        index.add_text(item.get("source_url"), item.get("text"))
        # Guard against the same run's fetch returning near-duplicate entries
        # (e.g. two RSS entries pointing at the same URL) — update the index
        # in-memory as we go so within-run duplicates are caught too.
        if item.get("content_hash"):
            index.content_hashes.add(item["content_hash"])
        if item.get("source_url"):
            index.urls.add(item["source_url"])
        title = normalize_title(item.get("title"))
        if title:
            index.normalized_titles.add(title)

    if new_items and not dry_run:
        storage.save_collected_items(run_id, stream, new_items)

    logger.info(
        f"Dedup phase complete — {len(new_items)} new items, "
        f"{duplicates_found} exact duplicates and {near_duplicates_found} near-duplicates skipped"
    )

    return {"new_items": new_items, "duplicates_found": duplicates_found,
            "near_duplicates_found": near_duplicates_found}
