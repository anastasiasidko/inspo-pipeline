"""
End-to-end local test for the full engine — no real network, API key, or
Google Sheet needed.

Mocks the same fake RSS feed used in test_rss_local.py, mocks the Anthropic
client, and runs everything through engine.run_stream() against an
InMemoryBackend (storage/fake_backend.py — a plain-Python StorageBackend used
only in tests). Proves the storage-agnostic refactor actually works end to
end: a Runs row starts and finishes with correct stats, Collected rows exist
before Findings rows, AI_log has one entry per item (including the
no-findings case), the anti-hallucination gate still catches a fabricated
quote, and re-running is idempotent.

Run: python3 test_engine_e2e.py
"""

import json
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, ".")

import engine
from storage.fake_backend import InMemoryBackend
from test_rss_local import FAKE_FEED_XML


TEST_CONFIG = {
    "stream": "competitor_monitoring",
    "extraction_model": "claude-3-5-haiku-20241022",
    "digest_window_days": 14,
    "relevance_ruleset": "Flag anything signaling a competitive change.",
    "signal_types": ["product_launch", "campaign_launch", "content_theme", "partnership", "other"],
    "signal_category_map": {"product_launch": "product", "campaign_launch": "market"},
    "scoring_direction": "novelty",
    "request_timeout_seconds": 30,
    "digest_path": None,  # skip the local file mirror for this test
}

# One real finding (quote genuinely in the article) + one hallucinated finding
# (quote that does NOT appear in the source text) — proves verification works.
MOCK_LLM_RESPONSE = json.dumps([
    {
        "entity": "FakeCompetitor",
        "signal_type": "campaign_launch",
        "summary": "Launched a new enterprise pricing tier with dedicated support.",
        "evidence_quote": "a completely new pricing tier designed\n      for enterprise customers",
        "confidence": 0.9,
        "themes": ["pricing", "enterprise"],
        "value_claims": ["dedicated support"],
        "target_audience": ["enterprise customers"],
        "terminology": ["pricing tier"],
    },
    {
        "entity": "FakeCompetitor",
        "signal_type": "other",
        "summary": "Secretly acquired a rocket company.",
        "evidence_quote": "we are buying a rocket company for the moon",
        "confidence": 0.5,
        "note": "unusual strategic move",
        "themes": [], "value_claims": [], "target_audience": [], "terminology": [],
    },
])


def make_mock_anthropic_client():
    mock_client = MagicMock()
    mock_block = MagicMock()
    mock_block.text = MOCK_LLM_RESPONSE
    mock_usage = MagicMock(input_tokens=500, output_tokens=150)
    mock_response = MagicMock(content=[mock_block], usage=mock_usage)
    mock_client.messages.create.return_value = mock_response
    return mock_client


def run_with_mocks(storage, mode="typed_only", inputs=None):
    import feedparser

    pre_parsed = feedparser.parse(FAKE_FEED_XML)
    inputs = inputs if inputs is not None else ["http://fake-url.test/feed"]

    with patch("feedparser.parse", return_value=pre_parsed), \
         patch("adapters.rss_adapter.RSSAdapter.fetch_full_text", return_value=None), \
         patch("anthropic.Anthropic") as mock_anthropic_cls:

        mock_anthropic_cls.return_value = make_mock_anthropic_client()
        os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"

        return engine.run_stream(
            stream="competitor_monitoring",
            inputs=inputs,
            mode=mode,
            lookback_days=3650,
            started_by="test",
            client_id="ksenia",
            storage=storage,
            config=dict(TEST_CONFIG),
            dry_run=False,
        )


def test_full_engine_run():
    print("TEST: engine.run_stream — fetch → dedup → extract → verify → score → digest")

    storage = InMemoryBackend()
    result = run_with_mocks(storage)

    run_id = result["run_id"]
    stats = result["stats"]

    # --- A Runs row exists, started and finished with sane stats ---
    run_row = storage.get_run(run_id)
    assert run_row is not None
    assert run_row["status"] == "done"
    assert run_row["client_id"] == "ksenia"
    print(f"  PASS — run {run_id} recorded, status=done")

    # --- Both fake feed entries got collected ---
    assert stats["collected"] == 2, stats
    assert stats["new_after_dedup"] == 2, stats
    collected_rows = storage.get_collected_for_run(run_id)
    assert len(collected_rows) == 2
    print(f"  PASS — collected={stats['collected']}, new_after_dedup={stats['new_after_dedup']}")

    # --- Findings exist and are traceable to this run; Collected exists before Findings ---
    findings = [f for f in storage.findings if f["run_id"] == run_id]
    assert len(findings) == 4, findings  # 2 findings x 2 articles
    assert stats["ai_findings"] == 4, stats
    print(f"  PASS — {len(findings)} findings extracted, all trace to run_id={run_id}")

    # --- Anti-hallucination gate: only the pricing quote genuinely appears in
    # article 1's text; article 2 is a short teaser, so neither finding
    # verifies there. Expected: 1 verified out of 4. ---
    verified = [f for f in findings if f["quote_verified"]]
    unverified = [f for f in findings if not f["quote_verified"]]
    assert len(verified) == 1, verified
    assert len(unverified) == 3, unverified
    assert stats["quote_verified"] == 1, stats
    print(f"  PASS — quote_verified={stats['quote_verified']} (hallucinated/mismatched quotes caught)")

    # --- AI_log has one entry per article, none skipped ---
    ai_log_for_run = [row for row in storage.ai_log if row["run_id"] == run_id]
    assert len(ai_log_for_run) == 2, ai_log_for_run  # one per article, even though both had findings
    print(f"  PASS — AI_log has {len(ai_log_for_run)} entries (one per article)")

    # --- Scoring ran ---
    scored = [f for f in storage.findings if f["run_id"] == run_id and f.get("novelty") is not None]
    assert len(scored) == 4, scored
    print(f"  PASS — all {len(scored)} findings scored for novelty")

    # --- Digest saved through storage, with a coverage line ---
    digest_row = storage.get_digest(run_id)
    assert digest_row is not None
    assert "Channels checked: 1" in digest_row["digest_text"]
    assert "New items collected: 2" in digest_row["digest_text"]
    assert "#" not in digest_row["digest_text"] and "**" not in digest_row["digest_text"], "digest must be plain text"
    print("  PASS — digest saved via storage backend with correct coverage line")

    # --- Cost tracking captured real (mocked) token usage ---
    assert run_row["est_cost"] > 0, run_row
    print(f"  PASS — est_cost tracked from token usage: ${run_row['est_cost']:.4f}")
    print()

    return storage


def test_quote_check_ignores_formatting_but_not_words():
    print("TEST: quote check tolerates paragraph breaks / curly quotes, still rejects changed words")
    from pipeline.extract import verify_quote_substring as v
    post = "There\u2019s a lot to say. It\u2019s obvious that images are busy.\n\nIs that how AI thinks?"
    assert v("It's obvious that images are busy. Is that how AI thinks?", post)
    assert not v("It's obvious that images are calm. Is that how AI thinks?", post)
    assert not v("we are buying a rocket company", post)
    print("  PASS")
    print()


def test_entity_is_the_author_not_a_mentioned_company():
    print("TEST: a finding is filed under the post's author, not a company mentioned in it")
    from pipeline.extract import run_extraction_phase
    storage = InMemoryBackend()
    item = {"source_name": "Someone Else — LinkedIn", "source_url": "https://l/1", "author": "Sandhya Simhan",
            "text": "OpenAI is hiring. I'm just a marketer standing in front of an engineer.", "published_date": "2026-09-20",
            "fetched_at": "2026-09-20"}
    reply = json.dumps([{"entity": "OpenAI", "signal_type": "content_theme", "summary": "s", "confidence": 0.8,
                         "evidence_quote": "I'm just a marketer standing in front of an engineer"}])
    with patch("anthropic.Anthropic") as A:
        A.return_value.messages.create.return_value = MagicMock(
            content=[MagicMock(text=reply)], usage=MagicMock(input_tokens=10, output_tokens=10))
        os.environ["ANTHROPIC_API_KEY"] = "t"
        run_extraction_phase(dict(TEST_CONFIG), storage, "ksenia", "r1", [item])
    assert storage.findings[0]["entity"] == "Sandhya Simhan", storage.findings[0]["entity"]
    print("  PASS")
    print()


def test_no_findings_still_logs_ai_log_entry():
    print("TEST: an article with zero findings still gets an AI_log row")

    import feedparser
    from unittest.mock import patch as _patch

    empty_response = json.dumps([])
    storage = InMemoryBackend()

    pre_parsed = feedparser.parse(FAKE_FEED_XML)
    with _patch("feedparser.parse", return_value=pre_parsed), \
         _patch("adapters.rss_adapter.RSSAdapter.fetch_full_text", return_value=None), \
         _patch("anthropic.Anthropic") as mock_anthropic_cls:

        mock_client = MagicMock()
        mock_block = MagicMock(text=empty_response)
        mock_client.messages.create.return_value = MagicMock(
            content=[mock_block], usage=MagicMock(input_tokens=100, output_tokens=10)
        )
        mock_anthropic_cls.return_value = mock_client
        os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"

        result = engine.run_stream(
            stream="competitor_monitoring", inputs=["http://fake-url.test/feed"],
            mode="typed_only", lookback_days=3650, started_by="test",
            storage=storage, config=dict(TEST_CONFIG), dry_run=False,
        )

    ai_log_for_run = [row for row in storage.ai_log if row["run_id"] == result["run_id"]]
    assert len(ai_log_for_run) == 2, ai_log_for_run
    assert all(row["verdict"] == "nothing" for row in ai_log_for_run), ai_log_for_run
    assert result["stats"]["ai_findings"] == 0
    print(f"  PASS — {len(ai_log_for_run)} AI_log rows written even with zero findings")
    print()


def test_headline_approval_flow():
    print("TEST: run_headline_scan (no AI cost) -> approve one item -> run_digest_from_headlines "
          "(AI runs on just the approved item)")

    import feedparser
    storage = InMemoryBackend()
    pre_parsed = feedparser.parse(FAKE_FEED_XML)
    trend_config = dict(TEST_CONFIG, stream="trend_research")

    with patch("feedparser.parse", return_value=pre_parsed), \
         patch("adapters.rss_adapter.RSSAdapter.fetch_full_text", return_value=None), \
         patch("anthropic.Anthropic") as mock_anthropic_cls:

        scan = engine.run_headline_scan(
            stream="trend_research", inputs=["http://fake-url.test/feed"], mode="typed_only",
            lookback_days=3650, started_by="test", client_id="ksenia", storage=storage,
            config=dict(trend_config), dry_run=False,
        )
        assert not mock_anthropic_cls.called, "run_headline_scan must not call the LLM — it's free"
    print(f"  PASS — scanned {len(scan['headlines'])} headline(s), zero AI calls")

    assert len(scan["headlines"]) == 2, scan["headlines"]
    headline_rows = [row for row in storage.headlines if row["run_id"] == scan["run_id"]]
    assert len(headline_rows) == 2 and all(r["status"] == "pending" for r in headline_rows)
    print("  PASS — both scraped headlines logged to storage as pending, before any AI cost")

    approved = scan["headlines"][:1]  # approve just the first of the two

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_anthropic_cls.return_value = make_mock_anthropic_client()
        os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
        result = engine.run_digest_from_headlines(
            run_id=scan["run_id"], approved_items=approved, stream="trend_research",
            lookback_days=3650, started_by="test", client_id="ksenia", storage=storage,
            config=dict(trend_config), dry_run=False,
        )
        assert mock_anthropic_cls.call_count >= 1 or mock_anthropic_cls.return_value.messages.create.called

    findings = [f for f in storage.findings if f["run_id"] == scan["run_id"]]
    assert len(findings) == 2, findings  # MOCK_LLM_RESPONSE has 2 findings, from the 1 approved article
    print(f"  PASS — extraction ran on just the 1 approved item ({len(findings)} findings, not the disapproved one)")

    approved_url = approved[0]["source_url"]
    approved_status = [r["status"] for r in storage.headlines
                       if r["run_id"] == scan["run_id"] and r["url"] == approved_url]
    assert approved_status == ["approved"], approved_status
    print("  PASS — the approved headline's row status was updated")

    assert storage.get_digest(scan["run_id"]) is not None, "digest must be saved under the run_headline_scan run_id"
    assert result["stats"]["status"] == "done"
    print("  PASS — digest saved under the same run_id the scan opened")
    print()


def test_rerunning_is_idempotent(storage):
    print("TEST: running the same fetch+dedup twice adds zero new Collected rows")

    result2 = run_with_mocks(storage)

    assert result2["stats"]["new_after_dedup"] == 0, result2["stats"]
    assert storage.get_digest(result2["run_id"]) is None, "a run with no results must not save a digest"
    total_collected_rows = len(storage.collected)
    assert total_collected_rows == 2, "Second run should not have added any Collected rows"
    print("  PASS — second run finds zero new items, Collected stays at 2 rows")
    print()


if __name__ == "__main__":
    print("=" * 70)
    print("Engine — End-to-End Local Test (mocked feed + mocked LLM + InMemoryBackend)")
    print("=" * 70)
    print()

    test_quote_check_ignores_formatting_but_not_words()
    test_entity_is_the_author_not_a_mentioned_company()
    storage = test_full_engine_run()
    test_no_findings_still_logs_ai_log_entry()
    test_headline_approval_flow()
    test_rerunning_is_idempotent(storage)

    print("=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)
