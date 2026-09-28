"""
Local test for SheetsBackend — no real Google account or network needed.

Mocks gspread entirely (service_account, the Spreadsheet, and every Worksheet)
so the Sheets-specific CONTRACT gets proven without ever touching a real
sheet: idempotent tab creation, batched writes (the single most important
property given Google's rate limits), dedup-index
round-tripping, and retention cleanup.

Run: python3 test_sheets_backend.py
"""

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(0, ".")

from storage.sheets_backend import SheetsBackend, TAB_HEADERS


class FakeWorksheet:
    """A tiny in-memory stand-in for a gspread Worksheet."""

    def __init__(self, title, headers):
        self.title = title
        self._headers = list(headers)
        self._rows = []  # list of value-lists, one per data row (no header)
        self.append_rows_calls = []
        self.append_row_calls = []
        self.clear_calls = 0

    def row_values(self, row_num):
        assert row_num == 1
        return self._headers

    def append_row(self, values):
        self.append_row_calls.append(values)
        if not self._headers:
            self._headers = list(values)

    def append_rows(self, values, value_input_option="RAW"):
        self.append_rows_calls.append(values)
        self._rows.extend(values)

    def get_all_records(self):
        return [dict(zip(self._headers, row)) for row in self._rows]

    def get_all_values(self):
        return [self._headers] + self._rows

    def update_cell(self, row, col, value):
        self._rows[row - 2][col - 1] = value

    def batch_update(self, updates):
        for update in updates:
            col_letter, row_num = _split_a1(update["range"])
            col_idx = _col_index(col_letter)
            self._rows[row_num - 2][col_idx] = update["values"][0][0]

    def clear(self):
        self.clear_calls += 1
        self._rows = []


def _split_a1(a1_range: str):
    i = 0
    while a1_range[i].isalpha():
        i += 1
    return a1_range[:i], int(a1_range[i:])


def _col_index(col_letter: str) -> int:
    n = 0
    for ch in col_letter:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


class FakeSpreadsheet:
    def __init__(self, existing_tabs=None):
        self._tabs = dict(existing_tabs or {})

    def worksheets(self):
        return [FakeWorksheetHandle(name, ws) for name, ws in self._tabs.items()]

    def worksheet(self, name):
        return self._tabs[name]

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(title, [])
        self._tabs[title] = ws
        return ws


class FakeWorksheetHandle:
    """worksheets() only needs .title in init()'s existence check."""
    def __init__(self, title, ws):
        self.title = title
        self._ws = ws


def make_backend(existing_tabs=None):
    fake_spreadsheet = FakeSpreadsheet(existing_tabs)
    mock_client = MagicMock()
    mock_client.open_by_key.return_value = fake_spreadsheet

    with patch("gspread.service_account", return_value=mock_client):
        backend = SheetsBackend(sheet_id="fake-sheet-id", credentials_path="fake.json")
    return backend, fake_spreadsheet


def test_init_creates_missing_tabs_idempotently():
    print("TEST: init() creates all the tabs when none exist, skips when they do")

    backend, spreadsheet = make_backend(existing_tabs={})
    backend.init()

    assert set(spreadsheet._tabs.keys()) == set(TAB_HEADERS.keys()), spreadsheet._tabs.keys()
    for tab_name, headers in TAB_HEADERS.items():
        ws = spreadsheet._tabs[tab_name]
        assert ws.append_row_calls == [headers], f"{tab_name} header mismatch: {ws.append_row_calls}"
    print(f"  PASS — created {len(spreadsheet._tabs)} tabs with correct headers")

    # Second init() call: tabs already exist with headers — nothing new written
    for ws in spreadsheet._tabs.values():
        ws.append_row_calls = []
    backend._headers_cache = {}
    backend.init()
    for tab_name, ws in spreadsheet._tabs.items():
        assert ws.append_row_calls == [], f"{tab_name} got an unexpected extra header write"
    print("  PASS — re-running init() on an already-set-up sheet writes nothing")
    print()


def test_save_collected_items_is_one_batched_call():
    print("TEST: saving 50 collected items makes exactly 1 append_rows call")

    backend, spreadsheet = make_backend(existing_tabs={})
    backend.init()

    items = [
        {
            "source_name": "Fake Blog", "source_url": f"https://fake.test/{i}",
            "title": f"Article {i}", "text": "some text", "text_complete": True,
            "content_hash": f"hash{i}", "published_date": "2026-01-01", "fetched_at": "2026-01-01",
        }
        for i in range(50)
    ]
    backend.save_collected_items("run-1", "competitor_monitoring", items)

    ws = spreadsheet._tabs["Collected"]
    assert len(ws.append_rows_calls) == 1, f"Expected 1 append_rows call, got {len(ws.append_rows_calls)}"
    assert len(ws.append_rows_calls[0]) == 50, "Expected all 50 rows in that one call"
    print(f"  PASS — {len(ws.append_rows_calls[0])} rows written in {len(ws.append_rows_calls)} API call")
    print()


def test_settings_and_rules_in_a_single_app_sheet():
    print("TEST: saved competitors and the two rules cells work with no stream/client columns")

    backend, spreadsheet = make_backend(existing_tabs={})
    backend.init()
    assert "stream" not in spreadsheet._tabs["Settings"]._headers
    assert all("stream" not in ws._headers and "client_id" not in ws._headers for ws in spreadsheet._tabs.values())

    backend.upsert_competitor("ksenia", "competitor_monitoring", "Val Nelson", "LinkedIn: https://x/val")
    backend.upsert_competitor("ksenia", "competitor_monitoring", "Val Nelson", "LinkedIn: https://x/val2")  # in place
    backend.upsert_competitor("ksenia", "competitor_monitoring", "Ana", "X: https://x.com/ana")
    backend.set_saved_item_active("ksenia", "competitor_monitoring", "Ana", False)
    active = backend.get_saved_items("ksenia", "competitor_monitoring")
    assert [r["competitor"] for r in active] == ["Val Nelson"] and active[0]["channels"] == "LinkedIn: https://x/val2"
    assert len(backend.get_saved_items("ksenia", "competitor_monitoring", active_only=False)) == 2
    print("  PASS — competitors are upserted by name and can be switched off")

    backend.save_rules("ksenia", "general", "Three circles", "anastasia")
    backend.save_rules("ksenia", "competitor_monitoring", "Flag pricing changes", "anastasia")
    backend.save_rules("ksenia", "competitor_monitoring", "Flag pricing changes v2", "ksenia")
    rules = backend.get_rules("ksenia", "competitor_monitoring")
    assert rules["rules_text"] == "Flag pricing changes v2" and rules["last_edited_by"] == "ksenia"
    assert backend.get_rules("ksenia", "general")["rules_text"] == "Three circles"
    assert backend.get_rules("ksenia", "industry_trends") is None
    rows = spreadsheet._tabs["Rules"].get_all_records()
    assert [r["part"] for r in rows] == ["General", "Competitors"], "one row per part, edited in place"
    print("  PASS — the General and Competitors rules are one cell each")
    print()


def test_dedup_index_round_trips():
    print("TEST: get_dedup_index correctly flags a known duplicate and passes a new item")

    backend, spreadsheet = make_backend(existing_tabs={})
    backend.init()

    run_id = backend.start_run("ksenia", "competitor_monitoring", "anastasia", [], "saved_only")
    backend.save_collected_items(run_id, "competitor_monitoring", [{
        "source_name": "Fake Blog", "source_url": "https://fake.test/seen",
        "title": "Already Seen Article", "text": "...", "text_complete": True,
        "content_hash": "abc123", "published_date": "2026-01-01", "fetched_at": "2026-01-01",
    }])

    index = backend.get_dedup_index("ksenia", "competitor_monitoring")
    assert index.contains({"source_url": "https://fake.test/seen"}), "Known URL should be flagged as duplicate"
    assert index.contains({"content_hash": "abc123"}), "Known content hash should be flagged as duplicate"
    assert not index.contains({"source_url": "https://fake.test/brand-new"}), "New URL should NOT be flagged"
    print("  PASS — dedup index correctly distinguishes seen vs. new items")
    print()


def test_cleanup_purges_only_collected_and_ai_log():
    print("TEST: cleanup_old_data purges old Collected/AI_log rows, leaves everything else")

    backend, spreadsheet = make_backend(existing_tabs={})
    backend.init()

    old_ts = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    new_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

    collected_ws = spreadsheet._tabs["Collected"]
    collected_ws.append_rows([
        ["run-old", "src", "tool", "url1", "d", "a", "t", "text", "Y", "h1", old_ts],
        ["run-new", "src", "tool", "url2", "d", "a", "t", "text", "Y", "h2", new_ts],
    ])
    ai_log_ws = spreadsheet._tabs["AI_log"]
    ai_log_ws.append_rows([
        ["run-old", "url1", "nothing", "raw", old_ts],
        ["run-new", "url2", "nothing", "raw", new_ts],
    ])
    findings_ws = spreadsheet._tabs["Findings"]
    findings_ws.append_rows([[
        "f1", "run-old", "Entity", "type", "cat", "summary", "quote",
        "Y", "0.9", "0.5", "src", "auth", "url1", "date", "[]", "[]", "[]", "[]", "", old_ts,
    ]])

    result = backend.cleanup_old_data(retention_days=30)

    assert result["collected_deleted"] == 1, result
    assert result["ai_log_deleted"] == 1, result
    assert len(collected_ws.get_all_records()) == 1
    assert collected_ws.get_all_records()[0]["url"] == "url2"
    assert len(ai_log_ws.get_all_records()) == 1
    assert len(findings_ws.get_all_records()) == 1, "Findings must never be purged by retention cleanup"
    print(f"  PASS — purged {result['collected_deleted']} old Collected row(s), "
          f"{result['ai_log_deleted']} old AI_log row(s), Findings untouched")
    print()


def test_get_run_details_funnel_and_zero_twice_in_a_row():
    print("TEST: get_run_details assembles the funnel and flags a source zero two runs running")

    backend, spreadsheet = make_backend(existing_tabs={})
    backend.init()

    run1 = backend.start_run("ksenia", "competitor_monitoring", "anastasia", ["feed"], "saved_only")
    backend.finish_run(run1, {
        "sources_checked": 1, "collected": 0, "new_after_dedup": 0, "ai_findings": 0,
        "quote_verified": 0, "in_digest": 0, "zero_result_sources": ["Dead Blog"], "errors": 0,
    })

    run2 = backend.start_run("ksenia", "competitor_monitoring", "anastasia", ["feed"], "saved_only")
    backend.finish_run(run2, {
        "sources_checked": 1, "collected": 0, "new_after_dedup": 0, "ai_findings": 0,
        "quote_verified": 0, "in_digest": 0, "zero_result_sources": ["Dead Blog"], "errors": 0,
    })

    details = backend.get_run_details(run2)
    assert details["funnel"]["collected"] == 0
    zero_flagged = [s for s in details["by_source"] if s["source"] == "Dead Blog"]
    assert zero_flagged, details["by_source"]
    assert zero_flagged[0]["zero_twice_in_a_row"] is True
    print("  PASS — second consecutive zero-result run is flagged zero_twice_in_a_row")
    print()


if __name__ == "__main__":
    print("=" * 70)
    print("SheetsBackend — Local Contract Tests (gspread fully mocked)")
    print("=" * 70)
    print()

    test_init_creates_missing_tabs_idempotently()
    test_save_collected_items_is_one_batched_call()
    test_settings_and_rules_in_a_single_app_sheet()
    test_dedup_index_round_trips()
    test_cleanup_purges_only_collected_and_ai_log()
    test_get_run_details_funnel_and_zero_twice_in_a_row()

    print("=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)
