"""
A record of every Anthropic call this app makes, so the question "what did I spend it on?" has an answer.

Each call becomes one row: when, what for (finding channels, extracting a post, writing the digest, …),
which model, tokens, web searches, and an estimated cost from the prices in config.yaml. Rows go to a local
file (api_usage.jsonl, git-ignored) at once, and to the sheet's API_usage tab when flush() is called.
The Anthropic Console stays the source of truth for what was actually billed; this shows where it went.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

PATH = Path(__file__).resolve().parent.parent / "api_usage.jsonl"
WEB_SEARCH_USD = 0.01  # Anthropic's price per web search request
_pending: List[Dict] = []
_prices: Dict = {}


def _num(x) -> int:
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


def _price(model: str) -> Dict:
    if not _prices:
        try:
            import yaml
            cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text()) or {}
            _prices.update(cfg.get("model_prices") or {})
        except Exception:
            pass
    return _prices.get(model) or {"input": 0.001, "output": 0.005}


def record(purpose: str, model: str, input_tokens=0, output_tokens=0, searches=0, detail: str = "") -> None:
    """Never raises; logging must not be able to break a run."""
    try:
        i, o, s = _num(input_tokens), _num(output_tokens), _num(searches)
        p = _price(model)
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "purpose": purpose, "model": model,
               "input_tokens": i, "output_tokens": o, "web_searches": s,
               "est_cost_usd": round(i / 1000 * p["input"] + o / 1000 * p["output"] + s * WEB_SEARCH_USD, 5),
               "detail": (detail or "")[:120]}
        _pending.append(row)
        with open(PATH, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Could not record API usage: {e}")


def record_response(purpose: str, model: str, response, detail: str = "") -> None:
    """Record straight from an Anthropic response (its .usage: tokens and web searches)."""
    usage = getattr(response, "usage", None)
    tools = getattr(usage, "server_tool_use", None)
    record(purpose, model, getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0),
           getattr(tools, "web_search_requests", 0) if tools is not None else 0, detail)


def flush(storage) -> None:
    """Copy the rows recorded since the last flush into the sheet's API_usage tab."""
    if not _pending:
        return
    rows = list(_pending)
    _pending.clear()
    try:
        storage.save_api_usage(rows)
    except Exception as e:
        logger.warning(f"Could not write API usage to the sheet (it is still in {PATH.name}): {e}")


def summary(days: int = 31) -> List[Dict]:
    """Per purpose over the last `days`: calls, tokens, searches, estimated cost. Newest data from the local file."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    by: Dict[str, Dict] = {}
    try:
        lines = PATH.read_text().splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r["at"] < cutoff:
            continue
        b = by.setdefault(r["purpose"], {"purpose": r["purpose"], "calls": 0, "web searches": 0, "est cost $": 0.0})
        b["calls"] += 1
        b["web searches"] += r.get("web_searches", 0)
        b["est cost $"] = round(b["est cost $"] + r.get("est_cost_usd", 0.0), 4)
    return sorted(by.values(), key=lambda b: -b["est cost $"])
