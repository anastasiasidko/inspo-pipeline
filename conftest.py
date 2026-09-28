"""Keep the tests from writing into the real api_usage.jsonl."""
import pytest

from pipeline import usage_log


@pytest.fixture(autouse=True)
def _isolated_usage_log(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_log, "PATH", tmp_path / "api_usage.jsonl")
    usage_log._pending.clear()
    yield
    usage_log._pending.clear()
