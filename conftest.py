"""Keep the tests from writing into the real api_usage.jsonl."""
import pytest

from pipeline import channel_discovery, usage_log


@pytest.fixture(autouse=True)
def _isolated_usage_log(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_log, "PATH", tmp_path / "api_usage.jsonl")
    usage_log._pending.clear()
    channel_discovery._cache.clear()
    yield
    usage_log._pending.clear()
