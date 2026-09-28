"""
InMemoryBackend — a plain-Python implementation of StorageBackend used ONLY in
tests. It exists for two reasons:

1. It lets pipeline/engine tests run with zero network and no gspread mocking.
2. It proves the StorageBackend interface is actually implementable a second
   way, independent of Sheets — directly de-risking "swap to a real database
   later" from the brief.

This is not a shipped feature. It is not exposed as a CLI storage option.
"""

import itertools
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from storage.backend import StorageBackend, DedupIndex, normalize_title


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryBackend(StorageBackend):
    def __init__(self):
        self.settings: List[Dict] = []
        self.rules: Dict[Tuple[str, str], Dict] = {}
        self.runs: Dict[str, Dict] = {}
        self.collected: List[Dict] = []
        self.ai_log: List[Dict] = []
        self.findings: List[Dict] = []
        self.digests: List[Dict] = []
        self._run_counter = itertools.count(1)
        self._finding_counter = itertools.count(1)

    def init(self) -> None:
        pass  # nothing to create — everything is already in-memory lists/dicts

    # ---- Settings ----

    def get_saved_items(self, client_id: str, stream: str, active_only: bool = True) -> List[Dict]:
        return [
            row for row in self.settings
            if row["client_id"] == client_id and row["stream"] == stream
            and (not active_only or row["active"])
        ]

    def add_saved_item(self, client_id: str, stream: str, kind: str, value: str,
                        added_by: str, notes: str = "") -> None:
        self.settings.append({
            "client_id": client_id, "stream": stream, "kind": kind, "value": value,
            "active": True, "competitor": notes,
        })

    def set_saved_item_active(self, client_id: str, stream: str, value: str, active: bool) -> None:
        for row in self.settings:
            if row["client_id"] == client_id and row["stream"] == stream and row["value"] == value:
                row["active"] = active

    def upsert_competitor(self, client_id: str, stream: str, name: str, channels_text: str,
                           frequency: str = "weekly") -> None:
        for row in self.settings:
            if row["client_id"] == client_id and row["stream"] == stream and row.get("competitor") == name:
                row.update(channels=channels_text, frequency=frequency, active=True)
                return
        self.settings.append({"client_id": client_id, "stream": stream, "kind": "competitor", "value": name,
                              "active": True, "competitor": name, "frequency": frequency,
                              "channels": channels_text, "positioning": "", "keywords": ""})

    # ---- Rules ----

    def get_rules(self, client_id: str, stream: str) -> Optional[Dict]:
        return self.rules.get((client_id, stream))

    def save_rules(self, client_id: str, stream: str, rules_text: str, edited_by: str) -> None:
        self.rules[(client_id, stream)] = {
            "client_id": client_id, "stream": stream, "rules_text": rules_text,
            "last_edited_by": edited_by, "last_edited_at": _now_iso(),
        }

    # ---- Runs ----

    def start_run(self, client_id: str, stream: str, started_by: str,
                   inputs_used: List[str], mode: str) -> str:
        run_id = f"{stream}-{next(self._run_counter)}"
        self.runs[run_id] = {
            "run_id": run_id, "client_id": client_id, "stream": stream,
            "started_by": started_by, "started_at": _now_iso(),
            "inputs_used": inputs_used, "mode": mode,
            "sources_checked": None, "collected": None, "new_after_dedup": None,
            "ai_findings": None, "quote_verified": None, "in_digest": None,
            "zero_result_sources": [], "errors": None, "est_cost": None,
            "finished_at": None, "status": "running",
        }
        return run_id

    def finish_run(self, run_id: str, stats: Dict) -> None:
        self.runs[run_id].update(stats)
        self.runs[run_id]["finished_at"] = _now_iso()
        self.runs[run_id]["status"] = "done"

    def get_run(self, run_id: str) -> Optional[Dict]:
        return self.runs.get(run_id)

    def list_runs(self, client_id: str, stream: Optional[str] = None) -> List[Dict]:
        rows = [r for r in self.runs.values() if r["client_id"] == client_id]
        if stream is not None:
            rows = [r for r in rows if r["stream"] == stream]
        return sorted(rows, key=lambda r: r["started_at"])

    # ---- Collected ----

    def get_dedup_index(self, client_id: str, stream: str) -> DedupIndex:
        run_ids_for_client = {
            r["run_id"] for r in self.runs.values()
            if r["client_id"] == client_id and r["stream"] == stream
        }
        analysed = {r["item_url"] for r in self.ai_log if not str(r.get("verdict", "")).startswith("failed")}
        index = DedupIndex()
        for item in self.collected:
            if item["run_id"] not in run_ids_for_client or item.get("source_url") not in analysed:
                continue
            if item.get("content_hash"):
                index.content_hashes.add(item["content_hash"])
            if item.get("source_url"):
                index.urls.add(item["source_url"])
            title = normalize_title(item.get("title"))
            if title:
                index.normalized_titles.add(title)
            index.add_text(item.get("source_url"), item.get("text"))
        return index

    def save_collected_items(self, run_id: str, stream: str, items: List[Dict]) -> None:
        for item in items:
            row = dict(item)
            row["run_id"] = run_id
            row["stream"] = stream
            row.setdefault("collected_at", _now_iso())
            self.collected.append(row)

    def get_collected_for_run(self, run_id: str) -> List[Dict]:
        return [row for row in self.collected if row["run_id"] == run_id]

    # ---- AI_log ----

    def save_ai_log_batch(self, run_id: str, entries: List[Dict]) -> None:
        for entry in entries:
            row = dict(entry)
            row["run_id"] = run_id
            row.setdefault("logged_at", _now_iso())
            self.ai_log.append(row)

    # ---- Findings ----

    def save_findings_batch(self, client_id: str, run_id: str, stream: str,
                             findings: List[Dict]) -> List[str]:
        ids = []
        for finding in findings:
            finding_id = f"{run_id}-{next(self._finding_counter)}"
            row = dict(finding)
            row.update({
                "finding_id": finding_id, "run_id": run_id,
                "client_id": client_id, "stream": stream,
            })
            row.setdefault("created_at", _now_iso())
            self.findings.append(row)
            ids.append(finding_id)
        return ids

    def get_findings_without_novelty(self, client_id: str, stream: str) -> List[Dict]:
        return [
            row for row in self.findings
            if row["client_id"] == client_id and row["stream"] == stream
            and row.get("novelty") is None
        ]

    def update_findings_novelty_batch(self, updates: List[Tuple[str, float]]) -> None:
        by_id = {row["finding_id"]: row for row in self.findings}
        for finding_id, score in updates:
            if finding_id in by_id:
                by_id[finding_id]["novelty"] = score

    def count_recent_findings(self, client_id: str, stream: str, entity: str, signal_type: str,
                               since_iso: str, exclude_finding_id: Optional[str] = None) -> int:
        return sum(
            1 for row in self.findings
            if row["client_id"] == client_id and row["stream"] == stream
            and row["entity"] == entity and row["signal_type"] == signal_type
            and row.get("created_at", "") >= since_iso
            and row["finding_id"] != exclude_finding_id
        )

    def get_findings_for_digest(self, client_id: str, stream: str, days_back: int,
                                 min_confidence: float = 0.0) -> List[Dict]:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        rows = [
            row for row in self.findings
            if row["client_id"] == client_id and row["stream"] == stream
            and row.get("quote_verified")
            and row.get("created_at", "") >= cutoff
            and row.get("confidence", 0) >= min_confidence
        ]
        return sorted(rows, key=lambda r: (r["entity"], -(r.get("novelty") or 0)))

    # ---- API usage ----

    def save_api_usage(self, rows: List[Dict]) -> None:
        self.api_usage = getattr(self, "api_usage", []) + list(rows)

    # ---- Competitor map ----

    def save_competitor_map(self, client_id: str, run_id: str, window_days: int, rows: List[Dict]) -> None:
        self.competitor_map = getattr(self, "competitor_map", []) + [
            {**r, "run_id": run_id, "client_id": client_id, "window_days": window_days} for r in rows]

    # ---- Digests ----

    def save_digest(self, client_id: str, run_id: str, stream: str,
                     date_range: str, digest_text: str) -> None:
        self.digests.append({
            "run_id": run_id, "client_id": client_id, "stream": stream,
            "date_range": date_range, "digest_text": digest_text, "created_at": _now_iso(),
        })

    def get_digest(self, run_id: str) -> Optional[Dict]:
        for row in self.digests:
            if row["run_id"] == run_id:
                return row
        return None

    # ---- Troubleshooting ----

    def get_run_details(self, run_id: str) -> Dict:
        run = self.get_run(run_id)
        if run is None:
            return {}

        collected = self.get_collected_for_run(run_id)
        ai_log_rows = [row for row in self.ai_log if row["run_id"] == run_id]
        findings_rows = [row for row in self.findings if row["run_id"] == run_id]

        by_source: Dict[str, List[Dict]] = {}
        for item in collected:
            by_source.setdefault(item.get("source", "unknown"), []).append(item)

        prior_runs = [
            r for r in self.list_runs(run["client_id"], run["stream"])
            if r["started_at"] < run["started_at"]
        ]
        prior_zero = set(prior_runs[-1]["zero_result_sources"]) if prior_runs else set()

        by_source_summary = [
            {
                "source": source,
                "items": len(items),
                "zero_items": len(items) == 0,
                "zero_twice_in_a_row": len(items) == 0 and source in prior_zero,
            }
            for source, items in by_source.items()
        ]
        for source in run.get("zero_result_sources", []):
            if source not in by_source:
                by_source_summary.append({
                    "source": source, "items": 0, "zero_items": True,
                    "zero_twice_in_a_row": source in prior_zero,
                })

        return {
            "run": run,
            "funnel": {
                "collected": run.get("collected"),
                "new_after_dedup": run.get("new_after_dedup"),
                "ai_findings": run.get("ai_findings"),
                "quote_verified": run.get("quote_verified"),
                "in_digest": run.get("in_digest"),
            },
            "by_source": by_source_summary,
            "ai_log": [
                {
                    "item_url": row["item_url"],
                    "verdict": row["verdict"],
                    "quote_check_pass": row.get("quote_check_pass"),
                }
                for row in ai_log_rows
            ],
            "findings": findings_rows,
            "digest": self.get_digest(run_id),
        }

    # ---- Retention ----

    def cleanup_old_data(self, retention_days: int = 30) -> Dict:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

        before_collected = len(self.collected)
        self.collected = [row for row in self.collected if row.get("collected_at", "") >= cutoff]
        collected_deleted = before_collected - len(self.collected)

        before_ai_log = len(self.ai_log)
        self.ai_log = [row for row in self.ai_log if row.get("logged_at", "") >= cutoff]
        ai_log_deleted = before_ai_log - len(self.ai_log)

        return {"collected_deleted": collected_deleted, "ai_log_deleted": ai_log_deleted}
