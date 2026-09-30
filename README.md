# Inspo Competitors — Competitor Monitoring

A fixed-sequence pipeline that watches competitor sources, extracts structured findings, and periodically synthesizes them into a digest. This is **not** an autonomous agent — every step is deterministic and debuggable.

Shared by Anastasia and Ksenia through one Google Sheet, **"Inspo competitors"**. Each part of Inspo is its own app with its own spreadsheet; this one is only about competitors, so the sheet has no stream or client columns. The engine (all the logic) never knows which storage it's talking to or which screen is calling it; see **Architecture** below.

## What It Does

1. **Fetch** — Pulls new posts and articles: RSS feeds/websites/podcasts/YouTube, plus LinkedIn and X (and any other platform, see below) through Apify scrapers
2. **Dedup** — Drops already-collected items (checked in bulk once per run, not one lookup per item)
3. **Extract** — LLM analyzes relevance, pulls structured findings (with quote verification)
4. **Verify** — Programmatically checks quotes are real (anti-hallucination gate)
5. **Score** — Novelty scoring
6. **Synthesize** — Generates a digest (handoff to content creation)

Every finding, raw item, AI verdict, and digest is saved to a shared Google Sheet, not a local file.

## What the app can do (beyond the core pipeline)

- **Find channels automatically** (`pipeline/channel_discovery.py`): type a name, Claude (Haiku 4.5 + web search; Sonnet 5 was about three times dearer) finds their website, newsletter, LinkedIn, X, etc. Every link is checked against the search results; sales pages are skipped; the person reviews before anything runs.
- **Scrapers with fallback** (`adapters/apify_linkedin_adapter.py`, `adapters/scraper_registry.py`): LinkedIn and X have ordered lists of Apify actors — if one breaks, the next is used and the switch is reported. A platform with no scraper gets one looked up in Apify's Store automatically (spend-capped, saved in `scrapers.json`, marked unverified until it returns real data).
- **Only typed names run**: the app collects only the competitors typed in that session, never every row in Settings.
- **Newsletters with no public feed** (`adapters/newsletter_signup.py`, `adapters/gmail_client.py`, `pipeline/newsletter_routing.py`): a Subscribe button submits the signup form (never bypasses a CAPTCHA — those need a manual signup). The app's own Gmail login then creates a "Competitors" label and a filter so those emails skip the inbox. Setup: enable the Gmail API in the Google Cloud project, create a Desktop-app OAuth client, put its JSON path in `GOOGLE_GMAIL_OAUTH_CLIENT`, and click Allow once. While the Google app is in **Testing** mode, Google expires the login about every 7 days, so that click repeats weekly.
- **Video approval** (`pipeline/media.py`): posts that are relevant and carry a video are listed with the video's length. Nothing is fetched until approved; then an Apify transcript actor (looked up in the Store, capped at $0.10 per run, falls back to the next actor) fetches it and the transcript goes through the normal extraction, quote check and digest under the post's author. Only videos, not images. The video field names are not yet confirmed against a real video post.
- **Competitor map** (`pipeline/competitor_map.py`, tab `Competitor_map`): posts and posts per week per competitor and channel, counted from everything the scrapers returned before dedup and the relevance filter. No engagement. Computed and saved only for runs with a look-back of 28+ days; social channels at the per-profile post limit (30) are flagged.
- **Explore a topic** (`pipeline/suggestions.voices_on_topic`, engine's `topics_by_entity`): under each person's digest paragraph, one button per topic already extracted for them this run (from Findings' `themes` — no extra AI call to produce the buttons). Click one to search for people, not already followed, writing about that same specific thing — anchored to that person's paragraph, not their whole body of work. A free-text box below covers anything the buttons miss. Same verification as the other suggestions (a real source page, reach only if stated); about $0.15 a search, button-triggered only.
- **Suggested new voices** (`pipeline/suggestions.py`): a button (it uses the Anthropic search budget, so it is never automatic) proposes 5 people not already followed, each with a reason, a source page from the search results, and reach only when a search result states it. "Add to competitors" pre-fills the first screen; it never runs anything itself.
- **Comments for one run** (`pipeline/run_comment.py`): a box next to the channel list on screen 2. What is written there applies to that run only and is never saved to the Rules tab. Claude splits the comment into per-channel look-backs (for example "for Instagram only look back 7 days" while the default is 15 — applied to every matching channel, and the digest window widens if one channel looks back further) and everything else (added to the AI's rules as run-only instructions that win on conflict). A limit that matches no channel is reported, not applied. The result screen shows exactly what was applied, and the comment is logged in the Runs tab (`inputs_used`). Costs a fraction of a cent.
- **`#map` in the run comment** (`engine._find_similar_voices`, `pipeline/suggestions.similar_voices`): while tuning, write `#map` anywhere in the comments box to get the competitor map for that run even if it is shorter than 28 days, plus 2 similar people per competitor. Similar people are found by web search from what the competitor posted in the run (and their positioning/keywords in Settings), within the scope of the rules, excluding everyone already followed or already suggested; each needs a source page from the search results, and reach appears only when a search result states it. Both go into the Competitor_map tab (channel rows, plus "Similar voice" rows with the last four columns filled) and onto the result screen. The keyword is matched exactly, without AI. Cost is shown in the Runs tab's cost_breakdown (one web search per competitor).
- **When the AI is unavailable** (usage limit reached, no credit, bad key): the first failed call stops the AI steps, every affected post is written to AI_log as `failed: <the provider's message>`, the result screen and digest say so plainly (never "no new content"), and no paid search is attempted. Those posts are not treated as already collected: the next run analyses them. Every post that *was* analysed appears in AI_log, including the ones judged not relevant (`nothing`). A scraper's "no results" placeholder counts as an empty result, not a failure.
- **API spend log** (`pipeline/usage_log.py`): every Anthropic call the app makes (finding channels, extracting a post, writing the digest, similar people, reading a run comment, writing a scraper input) is recorded with its purpose, model, tokens, web searches and an estimated cost — in `api_usage.jsonl` (local, git-ignored) and the sheet's `API_usage` tab, and summarised at the bottom of the app. The Anthropic Console is still the source of truth for what was billed; this shows where it went. Calls made before this log existed were not recorded.
- **Costs**: every run records a per-tool cost breakdown in the Runs tab (Anthropic extraction and digest, each Apify actor).

## Setup

### 1. Install dependencies
```bash
python -m venv venv
source venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

### 2. Set up the Google Sheet + service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com), sign in, create a new project (e.g. "Inspo Pipeline").
2. Search for **"Google Sheets API"** → **Enable**.
3. **APIs & Services → Credentials → + Create Credentials → Service account** → name it (e.g. "inspo-pipeline-bot") → Create and Continue → Continue → Done.
4. Open the service account, copy its **email address** (`...@your-project.iam.gserviceaccount.com`).
5. **Keys tab → Add Key → Create new key → JSON → Create.** A `.json` file downloads — keep it somewhere safe (not in this project folder unless it matches a `.gitignore`d pattern), never commit it or paste it anywhere public.
6. Create a new blank Google Sheet ([sheets.new](https://sheets.new)). The code creates all the tabs and headers automatically the first time it connects — no manual setup needed there.
7. Click **Share** on the sheet → paste in the service account's email → set to **Editor** → uncheck "Notify people" → Share.
8. Copy the Sheet's ID from its URL (the string between `/d/` and `/edit`).

### 3. Configure `.env`
```bash
cp .env.example .env
```
Fill in `ANTHROPIC_API_KEY`, `GOOGLE_SHEET_ID`, and `GOOGLE_SERVICE_ACCOUNT_JSON` (path to the key file from step 5).

### 4. One-time: migrate the starter sources/ruleset into the Sheet
```bash
python migrate_config_to_sheets.py
```
Seeds the Settings and Rules tabs with the sources and ruleset this project started with. Safe to skip and add your own directly once the sheet exists.

### Run the Pipeline
```bash
# Full run, using the saved source list
python main.py

# Try extra sources for just this run, without saving them
python main.py --mode saved_plus_typed --inputs "https://example.com/feed"

# Dry run (fetch + print, no LLM calls, no writes)
python main.py --dry-run

# Limit articles for testing
python main.py --limit 5
```

### Run the App
```bash
streamlit run app.py
```
Opens at `http://localhost:8501`. Three screens: (1) type competitor names, (2) review the channels found for each (edit or add links) and approve, (3) read the digest as plain text. Approving runs the pipeline; everything it does is still written to the Google Sheet, which is for QA.

## Architecture

**Engine / Storage / Screen, kept strictly separate:**

- **Engine** (`engine.py` + `pipeline/*.py`) — all the logic: collecting, AI extraction, quote verification, scoring, synthesis. Exposes exactly two functions any screen calls: `run_stream(...)` and `get_run_details(run_id)`. Knows nothing about Google Sheets specifically, or about the CLI/Streamlit/anything else calling it.
- **Storage** (`storage/backend.py`) — a swappable interface. Today's only real implementation is `storage/sheets_backend.py` (Google Sheets via `gspread`). `storage/fake_backend.py`'s `InMemoryBackend` exists only for tests. Swapping to a different real database later (e.g. Supabase) means writing one new class against the same interface — the engine and pipeline phases never change.
- **Screen** — `main.py` today (a thin CLI wrapper). A Streamlit app is a planned later addition, calling the same `engine.run_stream`/`get_run_details` — no logic duplicated between them.

Every record carries a `client_id` (`"ksenia"` for now) so the same system can serve other clients later without restructuring.

### Two-Layer Output
- **Collected**: articles saved as-is (no analysis) — written *before* the AI step, so extraction can be re-run without re-fetching.
- **Findings**: atomic findings extracted from Collected items (entity, type, summary, quote, confidence, novelty).
- **Digests**: periodic synthesis of Findings (what gets handed to content creation).

### Anti-Hallucination: Quote Verification
After extraction, every `evidence_quote` is programmatically checked against the source text. If it doesn't appear as an exact substring, the finding is marked `quote_verified: false` and excluded from the digest. **This is the single highest-value check in the build**, and it's proven by a test (`test_engine_e2e.py`) that deliberately feeds it a fabricated quote.

### Scoring (v1)
- **confidence**: model's own judgment (0-1)
- **novelty**: has this entity+signal_type appeared in last 30 days? (0-1, high if novel)

*Note for social listening v2: novelty direction inverts (`scoring_direction: "recurrence"`) — repetition is the signal, not novelty.*

### Rate-limit discipline (Google Sheets)
Google's default quota is 100 read/write requests per 100 seconds per user. Every phase that touches many rows does so with **one batched call**, never one call per row: dedup reads the whole Collected tab once per run into an in-memory index; extraction accumulates findings/AI-log entries and flushes each in one `append_rows` call; scoring updates novelty in one `batch_update`. A full run costs roughly 7-10 total Sheets API calls, independent of article count.

## Sheet Schema

Tabs, created automatically on first connection (`storage.init()`). The exact headers live in `TAB_HEADERS` in `storage/sheets_backend.py`. One spreadsheet is one app for one client, so there are no stream or client_id columns (the engine's `stream`/`client_id` arguments are accepted and ignored by the Sheets backend).

```
Settings   competitor, frequency, channels, positioning, keywords, active
Rules      part, rules_text, last_edited_by, last_edited_at
           (two cells: "General" = the topics and scope that matter, "Competitors" = what to flag)
Runs       run_id, started_by, started_at, inputs_used, mode,
           sources_checked, collected, new_after_dedup, ai_findings, quote_verified,
           in_digest, zero_result_sources, errors, est_cost, finished_at, status,
           cost_breakdown
Collected  run_id, source, tool, url, published_at, author, title, text,
           text_complete, content_hash, collected_at
AI_log     run_id, item_url, verdict, raw_ai_response, logged_at
Findings   finding_id, run_id, entity, signal_type, signal_category,
           summary, evidence_quote, quote_verified, confidence, novelty, source, author,
           url, date, themes, value_claims, target_audience, terminology, other_note,
           created_at
Digests    run_id, date_range, digest_text, created_at
API_usage       at, purpose, model, input_tokens, output_tokens, web_searches,
                est_cost_usd, detail   (every Anthropic call the app makes)
Competitor_map  run_id, created_at, competitor, channel, posts, posts_per_week,
                window_days, at_post_limit, similar_person, why_similar, reach,
                source_url   (28+ day runs, or any run with #map)
```

Collected and AI_log rows older than 30 days are purged automatically at the end of every run (`cleanup_old_data`). Findings/Digests/Runs/Settings/Rules are kept forever.

## Local Operational Config (`config.yaml`)

Only technical/cost settings live here now — model choice, the signal-type taxonomy, timeouts. The source list and relevance ruleset are **not** here anymore; they live in the Sheet's Settings/Rules tabs so both people can read and edit them without touching code. See `config.py`'s `load_local_config()`.

## Extending This

**Another part of Inspo** (industry trends, social listening, …) is a separate app with its own spreadsheet, not another stream in this one. The rules that were written for those parts were removed from this sheet and saved in `backups/rules-for-other-inspo-parts.md` (local, not in Git).

**Restructuring an existing sheet**: `python migrate_sheet_to_competitors_only.py` (one-time, idempotent) backs up every tab to `backups/`, drops the stream/client columns, keeps only the General and Competitors rules, removes the empty default tab and renames the spreadsheet.

**Adding non-RSS sources** (newsletters, job pages, web pages)
- `Collected.tool` already exists for this; add new adapters in `adapters/`

**A different storage backend** (e.g. Supabase later)
- Write a new class implementing every method in `storage/backend.py`'s `StorageBackend` — nothing in `pipeline/*.py` or `engine.py` changes

**Messaging drift detection** (derived signal from history)
- Banked fields (`themes`, `value_claims`, `target_audience`, `terminology`) accumulate in Findings now
- Once there's weeks/months of history, build this as a time-series analysis step

## Standing Instructions Built In

- ✅ Every network call is wrapped; one broken source doesn't kill the run
- ✅ Per-source summary line: fetched / full-text-retrieved / findings
- ✅ Zero-result sources produce a warning (and get flagged if it happens two runs running)
- ✅ `--dry-run` flag: fetch and print, no LLM calls, no writes
- ✅ `--limit N` flag: cap articles processed while testing
- ✅ Config validated on load with clear error messages
- ✅ Never overwrites data; always appends, with dedup

## Project Structure

```
inspo-pipeline/
├── config.yaml                 # Local operational settings (model, taxonomy, timeouts)
├── config.py                   # Loads + validates config.yaml (or config_<stream>.yaml)
├── engine.py                   # run_stream() / get_run_details() — the engine/screen boundary
├── main.py                     # CLI entry point — a thin screen over engine.py
├── app.py                      # Streamlit entry point — another thin screen over engine.py
├── migrate_config_to_sheets.py # One-time: seed Settings/Rules from the old config.yaml values
├── requirements.txt
├── README.md                    # This file
├── .env.example                 # Copy to .env and fill in
├── .gitignore
│
├── adapters/
│   ├── rss_adapter.py          # RSS fetch + full-text extraction
│   ├── apify_linkedin_adapter.py  # Apify scrapers (LinkedIn, X) with fallback + video detection
│   ├── scraper_registry.py     # Which scraper per platform; finds one in Apify's Store if none
│   ├── feed_discovery.py       # Channel links -> collectable sources
│   ├── newsletter_signup.py    # Subscribe-button form submission (never bypasses CAPTCHAs)
│   └── gmail_client.py         # The app's own Gmail login: label + filter
│
├── pipeline/
│   ├── channel_discovery.py    # Find a person's channels (Claude + web search)
│   ├── competitor_map.py       # Posts and cadence per channel (counting only)
│   ├── run_comment.py          # Comments that apply to one run only
│   ├── media.py                # Video candidates + approved-video transcripts
│   ├── suggestions.py          # 5 suggested new people to follow
│   ├── newsletter_routing.py   # Pending newsletter subscriptions -> Gmail filter
│   ├── costs.py                # Cost per model call (prices in config.yaml)
│   ├── fetch.py                # Phase 1: fetch raw items
│   ├── dedup.py                # Phase 2: drop duplicates (bulk index)
│   ├── extract.py              # Phase 3: LLM extraction + verify quotes (batched writes)
│   ├── score.py                # Phase 4: novelty scoring (batched writes)
│   └── synthesis.py            # Phase 5: generate + save the digest
│
└── storage/
    ├── backend.py               # StorageBackend interface — everything else depends on this
    ├── sheets_backend.py         # Google Sheets implementation (the real one)
    └── fake_backend.py           # InMemoryBackend — tests only, not a shipped feature
```

## Development Workflow

1. **Edit the Sheet's Settings/Rules tabs** — sources, ruleset (or `config.yaml` for model/taxonomy)
2. **Run with `--dry-run`** — see what would happen, no costs (still reads real sources/rules from the Sheet)
3. **Run with `--limit 5`** — extract from 5 articles, refine the ruleset
4. **Manual check** — read 15-20 extracted findings against their source articles in the Findings tab, keep an error list
5. **Iterate the ruleset** in the Rules tab
6. **Run full** — all sources, generate the digest
7. **Read the digest** — does it tell you something new?

## Known Limitations (v1)

- Web pages without a feed are not watched (RSS/podcast/YouTube feeds, Apify platform scrapers and subscribed newsletters only)
- Derived signals (messaging change, pricing trends) require time-series — banked for later
- Semantic dedup (same news, many outlets, different wording) is v2
- `--force-extract` is not supported against the Sheets backend yet (no cheap delete-by-item there) — filter the Findings tab manually if re-extraction is needed
- Newsletter double opt-in confirmation emails are not clicked automatically yet
- Scrapers found automatically in Apify's Store are unverified until they return real data

## Testing

- `test_rss_local.py` — RSS adapter logic (mocked feed, no network)
- `test_sheets_backend.py` — Google Sheets backend contract (mocked `gspread`, no real Google account): idempotent tab creation, batched writes, competitors and the two rules cells, dedup index, retention cleanup, run-details drill-down
- `test_engine_e2e.py` — the full pipeline through `engine.run_stream()` against `InMemoryBackend` (mocked feed + mocked LLM): quote verification, idempotent re-runs, AI_log completeness, cost tracking

- `test_near_dedup.py`, `test_feed_discovery.py`, `test_channel_discovery.py`, `test_scraper_registry.py`, `test_apify_linkedin.py`, `test_newsletter_signup.py`, `test_gmail_routing.py` — the matching adapters and helpers, all offline
- `test_run_comment.py` — per-channel look-back and run-only instructions (never saved to Rules)
- `test_monthly_features.py` — video detection and approval, competitor map, suggested voices, video transcript through the digest

Run all with `python -m pytest -q` (one pre-existing `test_rerunning_is_idempotent` error is a test-file quirk, not a pipeline failure).

## Questions?

See `open-questions.md` in the project memory for decisions that need Ksenia's input before real-world deployment.
