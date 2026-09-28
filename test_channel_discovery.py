"""Offline test: channel discovery marks where each link came from, and drops nothing silently."""
import sys, json, os
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0, ".")
os.environ["ANTHROPIC_API_KEY"] = "test"
from pipeline.channel_discovery import discover_channels

def test_sources():
    text = json.dumps([
        {"label": "LinkedIn", "url": "https://www.linkedin.com/in/x/", "source": "search"},
        {"label": "Blog", "url": "https://x.com/blog", "source": "linked from her About page"},
        {"label": "bad", "url": "not a url", "source": ""}])
    resp = NS(stop_reason="end_turn", content=[
        NS(type="web_search_tool_result", content=[NS(url="https://www.linkedin.com/in/x", title="X on LinkedIn")]),
        NS(type="text", text=text)])
    with patch("anthropic.Anthropic") as A:
        A.return_value.messages.create.return_value = resp
        out = discover_channels("X")
    assert [c["label"] for c in out] == ["LinkedIn", "Blog"], out
    assert out[0]["source"] == "search result: X on LinkedIn"
    assert out[1]["source"].startswith("not a search result, check it") and "About page" in out[1]["source"]
    print("  PASS — matched links cite the search result; unmatched ones are flagged")

if __name__ == "__main__":
    print("Channel discovery — offline test"); test_sources(); print("ALL TESTS PASSED")
