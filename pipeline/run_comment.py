"""
Comments for ONE run. The person can write, next to the channel list, things like
"for Instagram only look back 7 days" or "ignore anything about hiring this time".

Nothing here is saved: the Rules tab is not touched, and the comment applies to this run only.
Two kinds of things are picked out of the comment:
  - per-channel look-back overrides ("Instagram: 7 days") — applied to matching channels;
  - everything else — passed to the AI as run-specific instructions that win over the general
    rules where they conflict.
Claude reads the comment; its answer is checked before use (a look-back must name a real
channel of this run and a sensible number of days), and anything it could not match is
reported back rather than silently dropped.
"""

import json
import logging
import os
import re
from typing import Dict, List

from pipeline import usage_log
from pipeline.costs import llm_cost_usd

logger = logging.getLogger(__name__)

PARSE_MODEL = "claude-haiku-4-5-20251001"
MAP_TRIGGER = "#map"  # write this in the run comment to also fill the Competitor_map tab for this run
MAX_DAYS = 365
PROMPT = """A person is running a content-monitoring job. The default look-back is {default} days.
These are the channels in this run (names as shown): {channels}

They wrote this comment for this run only:
\"\"\"{comment}\"\"\"

Split the comment into:
1. "lookback_days": every place it asks for a different number of days for some channels. "match" must be
   a word or name that appears in the channel names above (a platform like "Instagram", or a person's name),
   "days" a whole number.
2. "instructions": everything else, as short plain instructions, in the person's own words. Do not add anything
   that is not in the comment. Leave the look-back requests out. Use "" if nothing else is left.
Return JSON only:
{{"lookback_days": [{{"match": "Instagram", "days": 7}}], "instructions": ""}}"""


def extract_map_trigger(comment: str):
    """-> (asked for the map?, the comment without the trigger). Exact keyword, no AI involved."""
    pattern = re.compile(r"(?<![\w#])" + re.escape(MAP_TRIGGER) + r"\b", re.I)
    return bool(pattern.search(comment or "")), pattern.sub("", comment or "").strip()


def parse_run_comment(comment: str, sources: List[Dict], default_days: int, config: dict = None,
                      model: str = PARSE_MODEL) -> Dict:
    """-> {"lookback": {source name: days}, "instructions": str, "summary": [str], "warnings": [str], "cost": float}.
    Never raises; if Claude can't be reached the whole comment is used as instructions and a warning says so."""
    comment = (comment or "").strip()
    out = {"lookback": {}, "instructions": "", "summary": [], "warnings": [], "cost": 0.0, "model": model}
    if not comment:
        return out
    key = os.environ.get("ANTHROPIC_API_KEY")
    parsed = None
    if key and sources:
        try:
            import anthropic
            resp = anthropic.Anthropic(api_key=key).messages.create(
                model=model, max_tokens=800, messages=[{"role": "user", "content": PROMPT.format(
                    default=default_days, channels="; ".join(s["name"] for s in sources), comment=comment)}])
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            parsed = json.loads(m.group(0)) if m else None
            usage_log.record_response("Read run comment", model, resp)
            usage = getattr(resp, "usage", None)
            if usage is not None:
                out["cost"] = llm_cost_usd(config or {}, model, usage.input_tokens, usage.output_tokens)
        except Exception as e:
            logger.warning(f"Could not read the run comment: {e}")
    if not isinstance(parsed, dict):
        out["instructions"] = comment
        out["warnings"].append("I could not split your comment into time limits and instructions, so all of it "
                               "was passed on as instructions. Time limits in it were not applied.")
        out["summary"].append(f"Instructions: {comment}")
        return out

    for entry in parsed.get("lookback_days") or []:
        match, days = str((entry or {}).get("match", "")).strip().lower(), (entry or {}).get("days")
        if not match or isinstance(days, bool) or not isinstance(days, (int, float)) or not 1 <= days <= MAX_DAYS:
            out["warnings"].append(f"Ignored a time limit I could not read: {entry}")
            continue
        hits = [s["name"] for s in sources if match in s["name"].lower() or match == str(s.get("kind", "")).lower()]
        if not hits:
            out["warnings"].append(f"'{entry.get('match')}' does not match any channel in this run, so its "
                                   f"{int(days)}-day limit was not applied.")
            continue
        for name in hits:
            out["lookback"][name] = int(days)
        out["summary"].append(f"{', '.join(hits)}: last {int(days)} days (instead of {default_days})")
    instructions = str(parsed.get("instructions") or "").strip()
    if instructions:
        out["instructions"] = instructions
        out["summary"].append(f"Instructions for this run: {instructions}")
    return out


def add_instructions(ruleset: str, instructions: str) -> str:
    """The general/stream rules plus this run's instructions, which win where they conflict."""
    if not instructions:
        return ruleset
    return (f"{ruleset}\n\nInstructions for this run only (from the person running it; where they conflict "
            f"with the rules above, follow these):\n{instructions}")
