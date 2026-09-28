"""
Google Sheets implementation of StorageBackend.

Field-name note: the pipeline's internal item/finding dicts use names like
source_url/source_name/published_date (carried over from before this backend
existed). The Sheet's own column names, per the brief, are shorter (url,
source, published_at). Translation between the two happens ONLY in this file
— nothing upstream needs to know the Sheet's column names.

Deviations from the brief's literal column list, called out explicitly rather
than done silently — worth confirming once you're looking at the real sheet:
- Runs gets `finished_at`/`status`; Collected/AI_log/Digests get their own
  `*_at` timestamp — needed so cleanup_old_data() knows row age.
- Collected gets `content_hash` — needed for the dedup index's hash-match path.
- Findings gets `signal_category`, `themes`, `value_claims`, `target_audience`,
  `terminology`, `other_note`, `created_at` — these are the "banked fields"
  from the original SQLite schema, kept because they're explicitly meant to
  seed future messaging-drift analysis (see project-context-compiled.md).

Rate-limit discipline (Google's default quota is 100 read + 100 write
requests per 100s per user):
- One gspread.Client per process; worksheets are opened once and cached.
- Every multi-row write is ONE append_rows() call, never a loop of append_row().
- Every multi-row read is ONE get_all_records() call, filtered in memory.
- get_dedup_index() reads the whole Collected tab once per run, not once per item.
- All gspread calls go through _with_backoff(), which retries on 429s.
"""

import functools
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from storage.backend import StorageBackend, DedupIndex

logger = logging.getLogger(__name__)

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TAB_HEADERS = {
    # First five columns are for people; the last five are technical (the
    # Sheet hides them) — the pipeline filters on client_id/stream/active and
    # collects from `value` (a feed or profile URL, or the name until one is set).
    "Settings": [
        "competitor", "frequency", "channels", "positioning", "keywords",
        "client_id", "stream", "kind", "value", "active",
    ],
    "Rules": ["stream", "rules_text", "last_edited_by", "last_edited_at", "client_id"],
    "Runs": [
        "run_id", "stream", "started_by", "started_at", "inputs_used", "mode",
        "sources_checked", "collected", "new_after_dedup", "ai_findings", "quote_verified",
        "in_digest", "zero_result_sources", "errors", "est_cost", "finished_at", "status",
        "cost_breakdown",
    ],
    "Collected": [
        "run_id", "stream", "source", "tool", "url", "published_at", "author", "title",
        "text", "text_complete", "content_hash", "collected_at",
    ],
    "AI_log": ["run_id", "item_url", "verdict", "raw_ai_response", "logged_at"],
    "Findings": [
        "finding_id", "run_id", "stream", "entity", "signal_type",
        "signal_category", "summary", "evidence_quote", "quote_verified", "confidence",
        "novelty", "source", "author", "url", "date", "themes", "value_claims",
        "target_audience", "terminology", "other_note", "created_at",
    ],
    "Digests": ["run_id", "stream", "date_range", "digest_text", "created_at"],
    "Competitor_map": ["run_id", "created_at", "competitor", "channel", "posts", "posts_per_week",
                       "window_days", "at_post_limit"],
}

DEFAULT_CLIENT = "ksenia"


def _same_client(row: Dict, client_id: str) -> bool:
    """Runs/Findings/Digests may have no client_id column (there is one client);
    a missing column counts as a match."""
    return row.get("client_id", client_id) in ("", client_id)


MAX_TEXT_CHARS = 45_000  # per the brief: Collected.text is cut at 45,000 chars


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _with_backoff(fn):
    """Retry a gspread call up to 3 times on rate-limit errors, exponential backoff."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        import gspread
        attempts = 3
        for attempt in range(attempts):
            try:
                return fn(*args, **kwargs)
            except gspread.exceptions.APIError as e:
                is_rate_limit = "RATE_LIMIT_EXCEEDED" in str(e) or "429" in str(e)
                if not is_rate_limit or attempt == attempts - 1:
                    raise
                wait = 2 ** (attempt + 1)
                logger.warning(f"Sheets API rate limited, retrying in {wait}s ({fn.__name__})")
                time.sleep(wait)
    return wrapper


def _as_bool(value) -> bool:
    return str(value).strip().upper() in ("Y", "YES", "TRUE", "1")


def _bool_as_cell(value: bool) -> str:
    return "Y" if value else "N"


def _as_json_cell(value) -> str:
    return json.dumps(value) if value else ""


def _from_json_cell(value):
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


class SheetsBackend(StorageBackend):
    def __init__(self, sheet_id: str, credentials_path: Optional[str] = None,
                 credentials_json: Optional[str] = None):
        if not credentials_path and not credentials_json:
            raise ValueError("Provide either credentials_path or credentials_json")

        import gspread

        if credentials_json:
            creds_dict = json.loads(credentials_json)
            self._client = gspread.service_account_from_dict(creds_dict, scopes=SHEETS_SCOPES)
        else:
            self._client = gspread.service_account(filename=credentials_path, scopes=SHEETS_SCOPES)

        self._spreadsheet = self._client.open_by_key(sheet_id)
        self._worksheets: Dict[str, "gspread.Worksheet"] = {}
        self._headers_cache: Dict[str, List[str]] = {}

    @classmethod
    def from_env(cls) -> "SheetsBackend":
        sheet_id = os.environ.get("GOOGLE_SHEET_ID")
        if not sheet_id:
            raise ValueError("GOOGLE_SHEET_ID is not set")
        return cls(
            sheet_id=sheet_id,
            credentials_path=os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
            credentials_json=os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT"),
        )

    # ---- worksheet access (cached, never re-opened per call) ----

    def _ws(self, tab_name: str):
        if tab_name not in self._worksheets:
            self._worksheets[tab_name] = self._spreadsheet.worksheet(tab_name)
        return self._worksheets[tab_name]

    def _headers(self, tab_name: str) -> List[str]:
        if tab_name not in self._headers_cache:
            self._headers_cache[tab_name] = _with_backoff(self._ws(tab_name).row_values)(1)
        return self._headers_cache[tab_name]

    def _read_rows(self, tab_name: str) -> List[Dict]:
        return _with_backoff(self._ws(tab_name).get_all_records)()

    def _append_rows(self, tab_name: str, rows: List[Dict]):
        if not rows:
            return
        headers = self._headers(tab_name)
        values = [[row.get(h, "") for h in headers] for row in rows]
        _with_backoff(self._ws(tab_name).append_rows)(values, value_input_option="RAW")

    # ---- lifecycle ----

    def init(self) -> None:
        existing = {ws.title for ws in _with_backoff(self._spreadsheet.worksheets)()}
        for tab_name, headers in TAB_HEADERS.items():
            if tab_name not in existing:
                ws = _with_backoff(self._spreadsheet.add_worksheet)(
                    title=tab_name, rows=1000, cols=len(headers)
                )
                _with_backoff(ws.append_row)(headers)
                logger.info(f"Created missing tab: {tab_name}")
            else:
                ws = self._ws(tab_name)
                if not _with_backoff(ws.row_values)(1):
                    _with_backoff(ws.append_row)(headers)
                    logger.info(f"Wrote missing header row: {tab_name}")

    # ---- Settings ----

    def get_saved_items(self, client_id: str, stream: str, active_only: bool = True) -> List[Dict]:
        rows = self._read_rows("Settings")
        return [
            row for row in rows
            if row["client_id"] == client_id and row["stream"] == stream
            and (not active_only or _as_bool(row.get("active")))
        ]

    def add_saved_item(self, client_id: str, stream: str, kind: str, value: str,
                        added_by: str, notes: str = "") -> None:
        self._append_rows("Settings", [{
            "client_id": client_id, "stream": stream, "kind": kind, "value": value,
            "active": _bool_as_cell(True), "competitor": notes,
        }])

    def upsert_competitor(self, client_id: str, stream: str, name: str, channels_text: str,
                           frequency: str = "weekly") -> None:
        ws = self._ws("Settings")
        col = {h: i for i, h in enumerate(self._headers("Settings"))}
        for row_num, row in enumerate(_with_backoff(ws.get_all_values)()[1:], start=2):
            if (row[col["client_id"]] == client_id and row[col["stream"]] == stream
                    and row[col["competitor"]] == name):
                _with_backoff(ws.batch_update)([
                    {"range": f"{_col_letter(col['channels'])}{row_num}", "values": [[channels_text]]},
                    {"range": f"{_col_letter(col['frequency'])}{row_num}", "values": [[frequency]]},
                    {"range": f"{_col_letter(col['active'])}{row_num}", "values": [["Y"]]},
                ])
                return
        self._append_rows("Settings", [{
            "competitor": name, "frequency": frequency, "channels": channels_text,
            "client_id": client_id, "stream": stream, "kind": "competitor", "value": name, "active": "Y"}])

    def set_saved_item_active(self, client_id: str, stream: str, value: str, active: bool) -> None:
        ws = self._ws("Settings")
        headers = self._headers("Settings")
        rows = _with_backoff(ws.get_all_values)()
        col_idx = {h: i for i, h in enumerate(headers)}
        for row_num, row in enumerate(rows[1:], start=2):
            if (row[col_idx["client_id"]] == client_id and row[col_idx["stream"]] == stream
                    and row[col_idx["value"]] == value):
                _with_backoff(ws.update_cell)(row_num, col_idx["active"] + 1, _bool_as_cell(active))
                return

    # ---- Rules ----

    def get_rules(self, client_id: str, stream: str) -> Optional[Dict]:
        rows = self._read_rows("Rules")
        matches = [r for r in rows if r["client_id"] == client_id and r["stream"] == stream]
        return matches[-1] if matches else None  # most recently appended wins

    def save_rules(self, client_id: str, stream: str, rules_text: str, edited_by: str) -> None:
        # One row per stream, edited in place, so a stream never has two cells.
        # Older text stays recoverable through Google Sheets' own version history.
        new_values = {"rules_text": rules_text, "last_edited_by": edited_by,
                      "last_edited_at": _now_iso()}
        ws = self._ws("Rules")
        headers = self._headers("Rules")
        col = {h: i for i, h in enumerate(headers)}
        for row_num, row in enumerate(_with_backoff(ws.get_all_values)()[1:], start=2):
            if row[col["client_id"]] == client_id and row[col["stream"]] == stream:
                _with_backoff(ws.batch_update)([
                    {"range": f"{_col_letter(col[k])}{row_num}", "values": [[v]]}
                    for k, v in new_values.items()])
                return
        self._append_rows("Rules", [{"client_id": client_id, "stream": stream, **new_values}])

    # ---- Runs ----

    def start_run(self, client_id: str, stream: str, started_by: str,
                   inputs_used: List[str], mode: str) -> str:
        run_id = f"{stream}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        self._append_rows("Runs", [{
            "run_id": run_id, "client_id": client_id, "stream": stream,
            "started_by": started_by, "started_at": _now_iso(),
            "inputs_used": _as_json_cell(inputs_used), "mode": mode,
            "sources_checked": "", "collected": "", "new_after_dedup": "",
            "ai_findings": "", "quote_verified": "", "in_digest": "",
            "zero_result_sources": _as_json_cell([]), "errors": "", "est_cost": "",
            "finished_at": "", "status": "running",
        }])
        return run_id

    def finish_run(self, run_id: str, stats: Dict) -> None:
        ws = self._ws("Runs")
        headers = self._headers("Runs")
        rows = _with_backoff(ws.get_all_values)()
        col_idx = {h: i for i, h in enumerate(headers)}
        run_col = col_idx["run_id"]

        for row_num, row in enumerate(rows[1:], start=2):
            if row[run_col] == run_id:
                updates = dict(stats)
                updates.setdefault("finished_at", _now_iso())
                updates.setdefault("status", "done")
                if "zero_result_sources" in updates:
                    updates["zero_result_sources"] = _as_json_cell(updates["zero_result_sources"])

                cell_updates = []
                for field, value in updates.items():
                    if field in col_idx:
                        cell_updates.append({
                            "range": f"{_col_letter(col_idx[field])}{row_num}",
                            "values": [[value]],
                        })
                if cell_updates:
                    _with_backoff(ws.batch_update)(cell_updates)
                return

        logger.warning(f"finish_run: run_id {run_id} not found in Runs tab")

    def get_run(self, run_id: str) -> Optional[Dict]:
        rows = self._read_rows("Runs")
        for row in rows:
            if row["run_id"] == run_id:
                row = dict(row)
                row["zero_result_sources"] = _from_json_cell(row.get("zero_result_sources"))
                row["inputs_used"] = _from_json_cell(row.get("inputs_used"))
                return row
        return None

    def list_runs(self, client_id: str, stream: Optional[str] = None) -> List[Dict]:
        rows = self._read_rows("Runs")
        matches = [r for r in rows if _same_client(r, client_id)]
        if stream is not None:
            matches = [r for r in matches if r["stream"] == stream]
        for row in matches:
            row["zero_result_sources"] = _from_json_cell(row.get("zero_result_sources"))
        return sorted(matches, key=lambda r: r["started_at"])

    # ---- Collected ----

    def get_dedup_index(self, client_id: str, stream: str) -> DedupIndex:
        from storage.backend import normalize_title

        run_ids = {
            r["run_id"] for r in self._read_rows("Runs")
            if _same_client(r, client_id) and r["stream"] == stream
        }
        index = DedupIndex()
        for row in self._read_rows("Collected"):
            if row["run_id"] not in run_ids:
                continue
            if row.get("content_hash"):
                index.content_hashes.add(row["content_hash"])
            if row.get("url"):
                index.urls.add(row["url"])
            title = normalize_title(row.get("title"))
            if title:
                index.normalized_titles.add(title)
            index.add_text(row.get("url"), row.get("text"))
        return index

    def save_collected_items(self, run_id: str, stream: str, items: List[Dict]) -> None:
        rows = []
        for item in items:
            text = item.get("text", "")
            rows.append({
                "run_id": run_id, "stream": stream,
                "source": item.get("source_name", ""),
                "tool": item.get("tool", "RSS reader"),
                "url": item.get("source_url", ""),
                "published_at": item.get("published_date", ""),
                "author": item.get("author", ""),
                "title": item.get("title", ""),
                "text": text[:MAX_TEXT_CHARS],
                "text_complete": _bool_as_cell(item.get("text_complete", True)),
                "content_hash": item.get("content_hash", ""),
                "collected_at": item.get("fetched_at") or _now_iso(),
            })
        self._append_rows("Collected", rows)

    def get_collected_for_run(self, run_id: str) -> List[Dict]:
        return [row for row in self._read_rows("Collected") if row["run_id"] == run_id]

    # ---- AI_log ----

    def save_ai_log_batch(self, run_id: str, entries: List[Dict]) -> None:
        rows = [{
            "run_id": run_id,
            "item_url": e.get("item_url", ""),
            "verdict": e.get("verdict", ""),
            "raw_ai_response": e.get("raw_ai_response", "")[:MAX_TEXT_CHARS],
            "logged_at": _now_iso(),
        } for e in entries]
        self._append_rows("AI_log", rows)

    # ---- Findings ----

    def save_findings_batch(self, client_id: str, run_id: str, stream: str,
                             findings: List[Dict]) -> List[str]:
        rows = []
        ids = []
        for i, finding in enumerate(findings):
            finding_id = f"{run_id}-{i + 1}"
            ids.append(finding_id)
            rows.append({
                "finding_id": finding_id, "run_id": run_id, "client_id": client_id, "stream": stream,
                "entity": finding.get("entity", ""),
                "signal_type": finding.get("signal_type", ""),
                "signal_category": finding.get("signal_category") or "",
                "summary": finding.get("summary", ""),
                "evidence_quote": finding.get("evidence_quote", ""),
                "quote_verified": _bool_as_cell(finding.get("quote_verified", False)),
                "confidence": finding.get("confidence", 0),
                "novelty": "",  # filled in later by the scoring phase
                "source": finding.get("source", ""),
                "author": finding.get("author", ""),
                "url": finding.get("url", ""),
                "date": finding.get("date", ""),
                "themes": _as_json_cell(finding.get("themes")),
                "value_claims": _as_json_cell(finding.get("value_claims")),
                "target_audience": _as_json_cell(finding.get("target_audience")),
                "terminology": _as_json_cell(finding.get("terminology")),
                "other_note": finding.get("other_note") or "",
                "created_at": _now_iso(),
            })
        self._append_rows("Findings", rows)
        return ids

    def get_findings_without_novelty(self, client_id: str, stream: str) -> List[Dict]:
        rows = self._read_rows("Findings")
        return [
            row for row in rows
            if _same_client(row, client_id) and row["stream"] == stream
            and (row.get("novelty") == "" or row.get("novelty") is None)
        ]

    def update_findings_novelty_batch(self, updates: List[Tuple[str, float]]) -> None:
        if not updates:
            return
        ws = self._ws("Findings")
        headers = self._headers("Findings")
        col_idx = {h: i for i, h in enumerate(headers)}
        novelty_col = col_idx["novelty"]
        rows = _with_backoff(ws.get_all_values)()

        row_num_by_id = {
            row[col_idx["finding_id"]]: row_num
            for row_num, row in enumerate(rows[1:], start=2)
        }

        cell_updates = []
        for finding_id, score in updates:
            row_num = row_num_by_id.get(finding_id)
            if row_num is None:
                logger.warning(f"update_findings_novelty_batch: finding_id {finding_id} not found")
                continue
            cell_updates.append({
                "range": f"{_col_letter(novelty_col)}{row_num}",
                "values": [[score]],
            })
        if cell_updates:
            _with_backoff(ws.batch_update)(cell_updates)

    def count_recent_findings(self, client_id: str, stream: str, entity: str, signal_type: str,
                               since_iso: str, exclude_finding_id: Optional[str] = None) -> int:
        rows = self._read_rows("Findings")
        return sum(
            1 for row in rows
            if _same_client(row, client_id) and row["stream"] == stream
            and row["entity"] == entity and row["signal_type"] == signal_type
            and row.get("created_at", "") >= since_iso
            and row["finding_id"] != exclude_finding_id
        )

    def get_findings_for_digest(self, client_id: str, stream: str, days_back: int,
                                 min_confidence: float = 0.0) -> List[Dict]:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        rows = self._read_rows("Findings")
        matches = [
            row for row in rows
            if _same_client(row, client_id) and row["stream"] == stream
            and _as_bool(row.get("quote_verified"))
            and row.get("created_at", "") >= cutoff
            and float(row.get("confidence") or 0) >= min_confidence
        ]
        return sorted(matches, key=lambda r: (r["entity"], -(float(r.get("novelty") or 0))))

    # ---- Competitor map ----

    def save_competitor_map(self, client_id: str, run_id: str, window_days: int, rows: List[Dict]) -> None:
        now = _now_iso()
        self._append_rows("Competitor_map", [{
            "run_id": run_id, "created_at": now, "competitor": r["competitor"], "channel": r["channel"],
            "posts": r["posts"], "posts_per_week": r["per_week"], "window_days": window_days,
            "at_post_limit": "Y" if r.get("capped") else "",
        } for r in rows])

    # ---- Digests ----

    def save_digest(self, client_id: str, run_id: str, stream: str,
                     date_range: str, digest_text: str) -> None:
        self._append_rows("Digests", [{
            "run_id": run_id, "client_id": client_id, "stream": stream,
            "date_range": date_range, "digest_text": digest_text, "created_at": _now_iso(),
        }])

    def get_digest(self, run_id: str) -> Optional[Dict]:
        for row in self._read_rows("Digests"):
            if row["run_id"] == run_id:
                return row
        return None

    # ---- Troubleshooting ----

    def get_run_details(self, run_id: str) -> Dict:
        run = self.get_run(run_id)
        if run is None:
            return {}

        collected = self.get_collected_for_run(run_id)
        ai_log_rows = [row for row in self._read_rows("AI_log") if row["run_id"] == run_id]
        findings_rows = [row for row in self._read_rows("Findings") if row["run_id"] == run_id]

        by_source: Dict[str, List[Dict]] = {}
        for item in collected:
            by_source.setdefault(item.get("source", "unknown"), []).append(item)

        prior_runs = [
            r for r in self.list_runs(run.get("client_id", DEFAULT_CLIENT), run["stream"])
            if r["started_at"] < run["started_at"]
        ]
        prior_zero = set(prior_runs[-1]["zero_result_sources"]) if prior_runs else set()

        by_source_summary = [
            {
                "source": source, "items": len(items),
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
                    "quote_check_pass": "verified" in row.get("verdict", "").lower()
                    if row.get("verdict") != "nothing" else None,
                }
                for row in ai_log_rows
            ],
            "findings": findings_rows,
            "digest": self.get_digest(run_id),
        }

    # ---- Retention ----

    def cleanup_old_data(self, retention_days: int = 30) -> Dict:
        """Rebuilds Collected/AI_log with only rows newer than the cutoff —
        simpler and more reliable than row-by-row deletes, which shift indices
        as they happen. Findings/Digests/Runs/Settings/Rules are untouched."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

        collected_deleted = self._rewrite_tab_excluding_old("Collected", "collected_at", cutoff, retention_days)
        ai_log_deleted = self._rewrite_tab_excluding_old("AI_log", "logged_at", cutoff, retention_days)

        return {"collected_deleted": collected_deleted, "ai_log_deleted": ai_log_deleted}

    def _rewrite_tab_excluding_old(self, tab_name: str, timestamp_field: str,
                                    cutoff: str, retention_days: int) -> int:
        rows = self._read_rows(tab_name)
        kept = [row for row in rows if row.get(timestamp_field, "") >= cutoff]
        deleted = len(rows) - len(kept)
        if deleted == 0:
            return 0

        ws = self._ws(tab_name)
        headers = self._headers(tab_name)
        _with_backoff(ws.clear)()
        _with_backoff(ws.append_row)(headers)
        if kept:
            self._append_rows(tab_name, kept)
        logger.info(f"Cleanup: removed {deleted} row(s) from {tab_name} older than {retention_days} days")
        return deleted


def _col_letter(col_idx_zero_based: int) -> str:
    """0 -> A, 1 -> B, ... 26 -> AA, matching gspread's A1 notation."""
    letters = ""
    n = col_idx_zero_based + 1
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
