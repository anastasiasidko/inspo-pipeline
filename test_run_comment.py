"""
Offline tests for comments that apply to one run only (look-back per channel + run-only instructions).
Run: python3 -m pytest test_run_comment.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

import engine
from pipeline import run_comment
from pipeline.competitor_map import build_map, format_map
from storage.fake_backend import InMemoryBackend
from test_engine_e2e import TEST_CONFIG, make_mock_anthropic_client
from test_rss_local import FAKE_FEED_XML

SOURCES = [{"name": "Val — Instagram", "kind": "instagram"}, {"name": "Val — LinkedIn", "kind": "linkedin"},
           {"name": "Ana — Instagram", "kind": "instagram"}]


def _reply(payload):
    return MagicMock(content=[MagicMock(type="text", text=json.dumps(payload))],
                     usage=MagicMock(input_tokens=300, output_tokens=60))


def _parse(payload):
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.create.return_value = _reply(payload)
        return run_comment.parse_run_comment("comment", SOURCES, 15)


def test_platform_limit_applies_to_every_channel_on_that_platform():
    got = _parse({"lookback_days": [{"match": "Instagram", "days": 7}], "instructions": ""})
    assert got["lookback"] == {"Val — Instagram": 7, "Ana — Instagram": 7}  # LinkedIn keeps the default
    assert got["warnings"] == [] and got["cost"] > 0
    assert "last 7 days (instead of 15)" in got["summary"][0]


def test_a_limit_for_one_person_and_other_instructions_are_separated():
    got = _parse({"lookback_days": [{"match": "Ana", "days": 30}], "instructions": "Ignore posts about hiring."})
    assert got["lookback"] == {"Ana — Instagram": 30}
    assert got["instructions"] == "Ignore posts about hiring."


def test_unmatched_or_silly_limits_are_reported_not_applied():
    got = _parse({"lookback_days": [{"match": "TikTok", "days": 5}, {"match": "Val", "days": 0},
                                    {"match": "Val", "days": "seven"}], "instructions": ""})
    assert got["lookback"] == {}
    assert len(got["warnings"]) == 3 and "TikTok" in got["warnings"][0]


def test_if_the_comment_cannot_be_read_all_of_it_becomes_instructions():
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.create.side_effect = RuntimeError("down")
        got = run_comment.parse_run_comment("Instagram only 7 days", SOURCES, 15)
    assert got["lookback"] == {} and got["instructions"] == "Instagram only 7 days" and got["warnings"]
    assert run_comment.parse_run_comment("  ", SOURCES, 15)["summary"] == []  # empty comment: nothing happens


def test_lookback_filter_uses_each_sources_own_window():
    now = datetime.now(timezone.utc)
    items = [{"source_name": "Val — Instagram", "published_date": (now - timedelta(days=10)).isoformat()},
             {"source_name": "Val — LinkedIn", "published_date": (now - timedelta(days=10)).isoformat()}]
    kept = engine._within_lookback(items, 15, {"Val — Instagram": 7})
    assert [i["source_name"] for i in kept] == ["Val — LinkedIn"]  # 10 days old is outside Instagram's 7


def test_map_shows_a_channels_own_window():
    rows = build_map([{"name": "Val — Instagram", "kind": "instagram", "lookback_days": 7},
                      {"name": "Val — LinkedIn", "kind": "linkedin"}],
                     [{"source_name": "Val — Instagram"}] * 7, 28, 30)
    text = format_map(rows, 28, 30)
    assert "Instagram: 7 posts, 7.0 per week [last 7 days]" in text and "LinkedIn: 0 posts, 0.0 per week" in text


def test_run_instructions_reach_the_ai_but_are_never_saved_to_rules():
    import feedparser
    storage = InMemoryBackend()
    storage.save_rules("ksenia", "general", "Flag ideas about work.", "test")
    parsed = {"lookback": {}, "instructions": "Ignore posts about hiring.", "summary": ["Instructions for this run: x"],
              "warnings": [], "cost": 0.0, "model": "m"}
    client = make_mock_anthropic_client()
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    with patch("feedparser.parse", return_value=feedparser.parse(FAKE_FEED_XML)), \
         patch("adapters.rss_adapter.RSSAdapter.fetch_full_text", return_value=None), \
         patch("anthropic.Anthropic", return_value=client), \
         patch("pipeline.run_comment.parse_run_comment", return_value=parsed):
        result = engine.run_stream(
            stream="competitor_monitoring", inputs=["Fake | http://fake-url.test/feed"], mode="typed_only",
            lookback_days=3650, started_by="test", storage=storage, config=dict(TEST_CONFIG),
            run_comment_text="Ignore posts about hiring.")
    sent = " ".join(str(c) for c in client.messages.create.call_args_list)
    assert "Ignore posts about hiring." in sent and "Flag ideas about work." in sent
    assert storage.get_rules("ksenia", "general")["rules_text"] == "Flag ideas about work."  # untouched
    assert result["comment_summary"] == ["Instructions for this run: x"]
    assert any(i.startswith("comment: ") for i in storage.get_run(result["run_id"])["inputs_used"])


# ---------------------------------------------------------------- #map trigger
def test_map_trigger_is_an_exact_keyword_and_is_removed_from_the_comment():
    assert run_comment.extract_map_trigger("For Instagram 7 days. #map") == (True, "For Instagram 7 days.")
    assert run_comment.extract_map_trigger("#MAP")[0] is True
    assert run_comment.extract_map_trigger("draw a map of it") == (False, "draw a map of it")
    assert run_comment.extract_map_trigger("#mapping later")[0] is False


def _search_reply(people, urls):
    result = MagicMock(type="web_search_tool_result", content=[MagicMock(url=u, title="t") for u in urls])
    text = MagicMock(type="text", text=json.dumps(people))
    usage = MagicMock(input_tokens=1000, output_tokens=200, server_tool_use=MagicMock(web_search_requests=2))
    return MagicMock(content=[result, text], stop_reason="end_turn", usage=usage)


def test_map_run_saves_cadence_and_two_similar_people_per_competitor_without_repeats():
    import feedparser
    storage = InMemoryBackend()
    storage.save_rules("ksenia", "general", "Thought leadership on work and burnout.", "test")
    people = {
        "Fake": [{"name": "Ann New", "why": "Writes on burnout.", "url": "https://ann.com", "reach": "50k readers",
                  "reach_source": "https://ann.com"},
                 {"name": "Bo New", "why": "Writes on pricing.", "url": "https://bo.com", "reach": None, "reach_source": None},
                 {"name": "Cy Extra", "why": "third, should be cut", "url": "https://cy.com", "reach": None, "reach_source": None}],
    }
    client = make_mock_anthropic_client()
    extraction = client.messages.create.return_value
    search = _search_reply(people["Fake"], ["https://ann.com", "https://bo.com", "https://cy.com"])
    client.messages.create.side_effect = lambda **kw: search if kw.get("tools") else extraction
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    with patch("feedparser.parse", return_value=feedparser.parse(FAKE_FEED_XML)), \
         patch("adapters.rss_adapter.RSSAdapter.fetch_full_text", return_value=None), \
         patch("anthropic.Anthropic", return_value=client):
        result = engine.run_stream(
            stream="competitor_monitoring", inputs=["Fake | http://fake-url.test/feed"], mode="typed_only",
            lookback_days=7, started_by="test", storage=storage, config=dict(TEST_CONFIG),
            run_comment_text="#map")
    assert [p["name"] for p in result["similar_voices"]["Fake"]] == ["Ann New", "Bo New"]  # exactly 2
    assert "Posting activity" in result["competitor_map"]  # shown although the run is only 7 days
    saved = storage.competitor_map
    assert any(r["channel"] == "Similar voice" and r["similar_person"] == "Ann New" and r["reach"] == "50k readers"
               for r in saved)
    assert any(r.get("posts") is not None and r["channel"] != "Similar voice" for r in saved)
    assert "similar-people search" in result["stats"]["cost_breakdown"]
    assert any("#map" in line for line in result["comment_summary"])


def test_without_the_trigger_a_short_run_saves_no_map():
    import feedparser
    storage = InMemoryBackend()
    storage.save_rules("ksenia", "general", "Rules.", "test")
    client = make_mock_anthropic_client()
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    with patch("feedparser.parse", return_value=feedparser.parse(FAKE_FEED_XML)), \
         patch("adapters.rss_adapter.RSSAdapter.fetch_full_text", return_value=None), \
         patch("anthropic.Anthropic", return_value=client):
        result = engine.run_stream(
            stream="competitor_monitoring", inputs=["Fake | http://fake-url.test/feed"], mode="typed_only",
            lookback_days=7, started_by="test", storage=storage, config=dict(TEST_CONFIG))
    assert result["competitor_map"] == "" and result["similar_voices"] == {} and not getattr(storage, "competitor_map", [])
