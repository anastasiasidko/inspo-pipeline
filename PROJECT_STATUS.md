# Inspo — status across chats

Inspo is being built as several parts, each with its own chat (to keep any one chat from growing
huge) but all in this one project folder and repo. **Read this file at the start of a session** —
it's the one place that says what the other parts/chats have done, since chats don't share context
with each other automatically. Whoever finishes a meaningful chunk of work updates their part's
paragraph below. See also `CLAUDE.md` (working agreement) and each part's own README.

## Competitors

What it is: watches named thought-leadership competitors (LinkedIn, X, newsletters, RSS) and turns
their posts into a digest, with channel discovery, a video-approval step, a monthly competitor map,
and "find who else writes about this" suggestions. Code: repo root (`app.py`, `engine.py`,
`pipeline/`, `adapters/`, `storage/`) — this is also the shared engine/storage code every other part
reuses. Its own Sheet: "Inspo competitors" (`GOOGLE_SHEET_ID` in `.env`). Details: `README.md`.

Current state (2026-09-30): built and in use. Gmail newsletter-folder routing is connected. Latest
additions: per-Anthropic-call spend logging (`API_usage` tab), the `#map` run-comment trigger
(posting cadence + similar people), and "Explore a topic" (per-paragraph buttons to find who else
covers one specific point in the digest). Blocked on nothing; the Anthropic account hit its monthly
usage limit on 2026-09-28 — some features are unverified against a real run until it resets.

## Trend research

What it is: the second stream — journals/newsletters/research sources instead of competitors, with
a headline-approval gate (scan headlines for free, approve/disapprove, only approved items cost AI).
Reuses the same engine/pipeline/storage code as Competitors. Code: `Trends/` (`trend_app.py`,
`config_trend_research.yaml`, `create_trends_sheet.py`) plus a "Trend research" mode in the root
`app.py`'s sidebar and `engine.run_headline_scan` / `run_digest_from_headlines`. Its own Sheet:
"Inspo Trends" (`GOOGLE_SHEET_ID_TRENDS` in `.env`, already set locally). Details: `Trends/README.md`.

Current state (2026-09-30): first version built, committed, and running locally. `create_trends_sheet.py`
was being reworked (in progress as of this writing) to support pointing at a sheet the user already
created and shared, since the service account has no Drive quota of its own to create one directly.

## Not started yet

Two more Inspo parts are planned (their own chat each, per the working style above) — not named or
scoped yet. Add a paragraph here once one starts.
