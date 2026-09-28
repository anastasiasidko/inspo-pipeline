"""
Competitor map — how much each competitor posts, per channel. Counting only, no AI.

Counts everything the scrapers returned inside the look-back window, before dedup and
before the AI's relevance filter, so it reflects real posting activity rather than what
happened to be relevant.
"""

from typing import Dict, List

MONTHLY_MIN_DAYS = 28  # the map is a monthly view; shorter runs don't compute or save it


def build_map(sources: List[Dict], items: List[Dict], lookback_days: int, cap: int) -> List[Dict]:
    """One row per channel checked: {competitor, channel, posts, per_week, capped}.
    Channels that returned nothing show 0 posts. `cap` is the most posts a scraper is asked
    for per profile — a social channel at the cap probably posted more than shown."""
    counts: Dict[str, int] = {}
    for item in items:
        counts[item.get("source_name", "")] = counts.get(item.get("source_name", ""), 0) + 1
    rows = []
    for s in sources:
        name = s["name"]
        competitor, _, channel = name.partition(" — ")
        posts = counts.get(name, 0)
        days = s.get("lookback_days", lookback_days)
        weeks = max(days, 1) / 7
        rows.append({
            "competitor": competitor, "channel": channel or s.get("kind", "rss"),
            "posts": posts, "per_week": round(posts / weeks, 1), "days": days,
            "capped": s.get("kind", "rss") != "rss" and posts >= cap,
        })
    return sorted(rows, key=lambda r: (r["competitor"], r["channel"]))


def format_map(rows: List[Dict], lookback_days: int, cap: int) -> str:
    """Plain text, one line per competitor and channel."""
    lines = [f"Posting activity, last {lookback_days} days", ""]
    current = None
    for r in rows:
        if r["competitor"] != current:
            current = r["competitor"]
            lines.append(current)
        note = f" (at the {cap}-post limit, may be more)" if r["capped"] else ""
        if r.get("days", lookback_days) != lookback_days:
            note += f" [last {r['days']} days]"
        lines.append(f"  {r['channel']}: {r['posts']} posts, {r['per_week']} per week{note}")
    return "\n".join(lines)
