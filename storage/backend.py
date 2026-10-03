"""
Storage interface — the swappable boundary between the engine and wherever
data actually lives.

Nothing in pipeline/*.py or engine.py may know about Google Sheets, SQLite, or
any other storage detail. They only ever call methods on a StorageBackend.
Today the only real implementation is SheetsBackend (storage/sheets_backend.py).
InMemoryBackend (storage/fake_backend.py) exists only for tests.

Every method that touches client-scoped data takes client_id explicitly —
callers decide which client they're operating as; the interface never assumes.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


def normalize_title(title: Optional[str]) -> str:
    """Lowercase, collapse whitespace — shared by dedup and every backend's
    dedup-index builder so title matching is consistent everywhere."""
    if not title:
        return ""
    return re.sub(r"\s+", " ", title.strip().lower())


MIN_WORDS_FOR_NEAR_DUP = 30  # shorter texts only match exactly (avoids false positives on tiny posts)


def text_shingles(text: Optional[str], k: int = 3) -> frozenset:
    """Set of k-word shingles of the lower-cased text — the basis for similarity."""
    words = re.sub(r"[^\w\s]", " ", (text or "").lower()).split()
    if len(words) < MIN_WORDS_FOR_NEAR_DUP:
        return frozenset()
    return frozenset(" ".join(words[i:i + k]) for i in range(len(words) - k + 1))


@dataclass
class DedupIndex:
    """Bulk-loaded once per run so dedup never does one lookup per item."""
    urls: Set[str] = field(default_factory=set)
    content_hashes: Set[str] = field(default_factory=set)
    normalized_titles: Set[str] = field(default_factory=set)
    shingle_sets: List[Tuple[str, frozenset]] = field(default_factory=list)  # (url, shingles) of collected texts

    def add_text(self, url: str, text: Optional[str]) -> None:
        shingles = text_shingles(text)
        if shingles:
            self.shingle_sets.append((url or "", shingles))

    def find_near_duplicate(self, item: Dict, threshold: float = 0.8) -> Optional[str]:
        """URL of an already-seen item whose text is >= threshold similar (Jaccard), else None."""
        mine = text_shingles(item.get("text"))
        if not mine:
            return None
        for url, other in self.shingle_sets:
            small, big = (mine, other) if len(mine) <= len(other) else (other, mine)
            if len(small) / len(big) < threshold:
                continue  # sizes too different to reach the threshold
            if len(mine & other) / len(mine | other) >= threshold:
                return url
        return None

    def contains(self, item: Dict) -> bool:
        content_hash = item.get("content_hash")
        if content_hash and content_hash in self.content_hashes:
            return True
        url = item.get("source_url") or item.get("url")
        if url and url in self.urls:
            return True
        title = normalize_title(item.get("title"))
        if title and title in self.normalized_titles:
            return True
        return False


class StorageBackend(ABC):
    # ---- lifecycle ----

    @abstractmethod
    def init(self) -> None:
        """Idempotent setup — create whatever structure is needed if missing."""

    # ---- Settings (replaces config.yaml's `sources`) ----

    @abstractmethod
    def get_saved_items(self, client_id: str, stream: str, active_only: bool = True) -> List[Dict]:
        ...

    @abstractmethod
    def add_saved_item(self, client_id: str, stream: str, kind: str, value: str,
                        added_by: str, notes: str = "") -> None:
        ...

    @abstractmethod
    def set_saved_item_active(self, client_id: str, stream: str, value: str, active: bool) -> None:
        ...

    @abstractmethod
    def upsert_competitor(self, client_id: str, stream: str, name: str, channels_text: str,
                           frequency: str = "weekly") -> None:
        """Create the competitor's Settings row or update its channels; mark it active."""

    # ---- Rules (replaces config.yaml's `relevance_ruleset`) ----

    @abstractmethod
    def get_rules(self, client_id: str, stream: str) -> Optional[Dict]:
        ...

    @abstractmethod
    def save_rules(self, client_id: str, stream: str, rules_text: str, edited_by: str) -> None:
        ...

    # ---- Runs ----

    @abstractmethod
    def start_run(self, client_id: str, stream: str, started_by: str,
                   inputs_used: List[str], mode: str) -> str:
        """Returns run_id."""

    @abstractmethod
    def finish_run(self, run_id: str, stats: Dict) -> None:
        ...

    @abstractmethod
    def get_run(self, run_id: str) -> Optional[Dict]:
        ...

    @abstractmethod
    def list_runs(self, client_id: str, stream: Optional[str] = None) -> List[Dict]:
        ...

    # ---- Collected (raw items, written BEFORE the AI step) ----

    @abstractmethod
    def get_dedup_index(self, client_id: str, stream: str) -> DedupIndex:
        """One bulk read per run — never one lookup per item."""

    @abstractmethod
    def save_collected_items(self, run_id: str, stream: str, items: List[Dict]) -> None:
        """One batched write for the whole list, never one call per item."""

    @abstractmethod
    def get_collected_for_run(self, run_id: str) -> List[Dict]:
        ...

    @abstractmethod
    def get_collected_items(self, client_id: str, stream: str, limit: int = 50) -> List[Dict]:
        """Get unextracted items from Collected (items without successful AI_log entries)."""
        ...

    # ---- AI_log ----

    @abstractmethod
    def save_ai_log_batch(self, run_id: str, entries: List[Dict]) -> None:
        """entries: [{item_url, verdict, raw_ai_response}]. One item = one entry,
        including items where the model found nothing (verdict="nothing")."""

    # ---- Findings ----

    @abstractmethod
    def save_findings_batch(self, client_id: str, run_id: str, stream: str,
                             findings: List[Dict]) -> List[str]:
        """Returns the finding_id assigned to each finding, in order."""

    @abstractmethod
    def get_findings_without_novelty(self, client_id: str, stream: str) -> List[Dict]:
        ...

    @abstractmethod
    def update_findings_novelty_batch(self, updates: List[Tuple[str, float]]) -> None:
        """updates: [(finding_id, novelty_score), ...]"""

    @abstractmethod
    def count_recent_findings(self, client_id: str, stream: str, entity: str, signal_type: str,
                               since_iso: str, exclude_finding_id: Optional[str] = None) -> int:
        ...

    @abstractmethod
    def get_findings_for_digest(self, client_id: str, stream: str, days_back: int,
                                 min_confidence: float = 0.0) -> List[Dict]:
        ...

    # ---- Digests ----

    @abstractmethod
    def save_digest(self, client_id: str, run_id: str, stream: str,
                     date_range: str, digest_text: str) -> None:
        ...

    @abstractmethod
    def get_digest(self, run_id: str) -> Optional[Dict]:
        ...

    @abstractmethod
    def save_api_usage(self, rows: List[Dict]) -> None:
        """One row per Anthropic call the app made (see pipeline/usage_log.py)."""
        ...

    @abstractmethod
    def save_competitor_map(self, client_id: str, run_id: str, window_days: int, rows: List[Dict]) -> None:
        """One row per competitor and channel: posts and posts per week in the window."""
        ...

    # ---- Headlines (the headline-approval gate: scan first, approve, then extract) ----

    @abstractmethod
    def save_headlines(self, client_id: str, run_id: str, items: List[Dict]) -> None:
        """One row per scraped item, status="pending" — written before any AI cost is spent.
        items: the same shape Collected rows use (source, url, title, published_at, ...)."""

    @abstractmethod
    def update_headline_status(self, run_id: str, url: str, status: str) -> None:
        """Find the row for (run_id, url) and set its status ("approved"/"rejected") + decided_at."""

    # ---- Troubleshooting / Results drill-down ----

    @abstractmethod
    def get_run_details(self, run_id: str) -> Dict:
        """
        Returns:
            {
              "run": {...Runs row...},
              "funnel": {collected, new_after_dedup, ai_findings, quote_verified, in_digest},
              "by_source": [{source, items, zero_items, zero_twice_in_a_row}, ...],
              "ai_log": [{item_url, verdict, quote_check_pass}, ...],
              "findings": [...Findings rows for this run...],
              "digest": {...Digests row...} or None,
            }
        """

    # ---- Retention ----

    @abstractmethod
    def cleanup_old_data(self, retention_days: int = 30) -> Dict:
        """Purges Collected + AI_log rows older than retention_days.
        Findings/Digests/Runs/Settings/Rules are never touched.
        Returns {"collected_deleted": N, "ai_log_deleted": N}."""
