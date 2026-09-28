# Brief for Claude Code: Google Sheets storage + shared app

Paste this file into Claude Code (or tell it: "read claude-code-brief-sheets-and-app.md and let's plan it").
Work step by step. Ask me before each part, and tell me what you need from me (accounts, keys).

## Context

- This folder (`inspo-pipeline`) holds the Inspo engine: fetch → remove repeats → AI extraction →
  quote check → scoring → digest. Read `README.md` first.
- Today it only does competitor monitoring from RSS, saves results to a local file (`signals.db`)
  and a `digest.md`, and has only been tested with fake data.
- Two people use it: me (Anastasia) and Ksenia. Later it may become a product for other clients.

## The one rule that matters most: keep the engine and the screen separate

- **Engine** = all the logic (collecting, AI, checks, digest). It must not know which screen is in
  front of it. It exposes simple functions like `run_stream(stream, inputs, mode, lookback_days,
  started_by)` and `get_run_details(run_id)`.
- **Storage** = a swappable part behind one interface. Implement Google Sheets now; later we may
  switch to a real database (e.g. Supabase) by adding a second implementation, not rewriting.
- **Screen** = the Streamlit app. It only collects inputs, calls engine functions and shows results.
  No rules, lists, prompts or logic hard-coded in the app.
- Every record carries a `client_id` (just "ksenia" for now) so the same system can serve several
  clients later.

## Part 1: Google Sheets storage

One Google Sheets workbook, shared by both of us. Tabs:

| Tab | One row = | Key columns |
|---|---|---|
| Settings | one saved item (the "main list") | client_id, stream, kind (competitor / topic / account / hashtag / subreddit / url), value, active (Y/N), added_by, notes |
| Rules | one stream's rules | client_id, stream, rules_text, last_edited_by, last_edited_at |
| Runs | one run | run_id, client_id, stream, started_by, started_at, inputs_used, mode, sources_checked, collected, new_after_dedup, ai_findings, quote_verified, in_digest, zero_result_sources, errors, est_cost |
| Collected | one raw item exactly as collected | run_id, stream, source, tool, url, published_at, author (username/handle), title, text (cut at 45,000 chars), text_complete |
| AI_log | one AI decision per item | run_id, item_url, verdict (N findings / nothing), raw_ai_response |
| Findings | one finding | run_id, client_id, stream, entity, signal_type, summary, evidence_quote, quote_verified, confidence, novelty, source, author (username/handle), url, date |
| Digests | one digest | run_id, client_id, stream, date_range, digest_text |

Notes:
- Raw items MUST be saved (Collected tab) before the AI step, so we can troubleshoot and re-run the
  AI without collecting again.
- Auto-clean Collected and AI_log rows older than 30 days (setting). Findings, Digests, Runs, Settings
  and Rules are kept.
- The engine reads its lists and rules from Settings/Rules at the start of every run (no lists or
  rules in config files or code). Move the current rules text from `config.yaml` into the Rules tab.
- Use a Google service account; tell me step by step how to create it and share the sheet with it.
  Keys go in a local `.env` / Streamlit secrets, never in the code or the sheet.

## Part 2: The Streamlit app

Layout (see `inspo-app-mockup.html` for the picture):

- Tabs: Competitors · Industry trends · Social listening · Creative inspiration · Results · Troubleshooting.
- Each stream tab:
  - **Saved list**: shows items from the Settings tab + "Edit saved list" button (edit in the app or
    open the sheet).
  - **Just for this run**: a text box (one item per line) + choice: *Only what I typed* /
    *Saved list + what I typed* / *Saved list only*. Typed items are used for this run only and never
    written to Settings.
  - **Look back**: 7 / 14 / 30 days.
  - **Rules**: "View / edit rules" opens the stream's rules text; saving writes to the Rules tab with
    who edited it.
  - **Run** button: shows an estimated cost before starting, progress while running, then a link to
    the result. Streams that aren't connected yet show the button disabled ("not connected yet").
- **Results**: pick a run; show the digest (conclusions with number of supporting findings) and the
  findings table (company, what happened, quote, link, confidence). Only quote-verified findings.
- **Troubleshooting**: pick a run; show the step counts: Collected → New → AI found something →
  Quote check passed → In digest. Click a step to see its items: raw items per source (flag sources
  with zero items, and loudly if zero twice in a row), the AI verdict per item, and quote-check
  pass/fail.
- Simple "who are you" selector (Anastasia / Ksenia) to record who ran or edited things. No real login yet.
- Hosting: Streamlit Community Cloud as a private app, shared with Ksenia by email. API keys in
  Streamlit secrets.

## Part 3: Order of work

1. Storage interface + Google Sheets implementation; move rules/lists into the sheet.
2. Run the existing competitor engine once for real (I'll provide the Anthropic API key) and check
   the Sheet fills correctly.
3. Streamlit app with all four tabs; only one stream connected.
4. Connect **industry trends** first: sources = industry websites/news (built-in reader), discovery
   via Exa (free monthly credit). Rules tuned for market shifts, not one company.
5. Later, one at a time: competitor social accounts, social listening and creative inspiration via
   Apify (Instagram, TikTok, LinkedIn, Reddit, X, Meta ad library).

## Guardrails

- Collect public pages only, logged out. Never use my or Ksenia's login/cookies in any scraper.
- Every network call can fail without stopping the run; failures show up in Runs and Troubleshooting.
- Keep `--dry-run` and `--limit` working, and show estimated cost before any paid run.
