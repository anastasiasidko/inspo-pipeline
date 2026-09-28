# Inspo Pipeline — Competitor Monitoring via RSS

A fixed-sequence pipeline that watches competitor sources, extracts structured findings, and periodically synthesizes them into a digest. This is **not** an autonomous agent — every step is deterministic and debuggable.

Shared by Anastasia and Ksenia through one Google Sheet — no more local `signals.db`. The engine (all the logic) never knows which storage it's talking to or which screen is calling it; see **Architecture** below.

## What It Does

1. **Fetch** — Pulls new articles from RSS feeds
2. **Dedup** — Drops already-collected items (checked in bulk once per run, not one lookup per item)
3. **Extract** — LLM analyzes relevance, pulls structured findings (with quote verification)
4. **Verify** — Programmatically checks quotes are real (anti-hallucination gate)
5. **Score** — Novelty scoring
6. **Synthesize** — Generates a digest (handoff to content creation)

Every finding, raw item, AI verdict, and digest is saved to a shared Google Sheet, not a local file.

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

Tabs, created automatically on first connection (`storage.init()`):

```
Settings   client_id, stream, kind, value, active, added_by, notes
Rules      client_id, stream, rules_text, last_edited_by, last_edited_at
Runs       run_id, client_id, stream, started_by, started_at, inputs_used, mode,
           sources_checked, collected, new_after_dedup, ai_findings, quote_verified,
           in_digest, zero_result_sources, errors, est_cost, finished_at, status
Collected  run_id, stream, source, tool, url, published_at, author, title, text,
           text_complete, content_hash, collected_at
AI_log     run_id, item_url, verdict, raw_ai_response, logged_at
Findings   finding_id, run_id, client_id, stream, entity, signal_type, signal_category,
           summary, evidence_quote, quote_verified, confidence, novelty, source, author,
           url, date, themes, value_claims, target_audience, terminology, other_note,
           created_at
Digests    run_id, client_id, stream, date_range, digest_text, created_at
```

Collected and AI_log rows older than 30 days are purged automatically at the end of every run (`cleanup_old_data`). Findings/Digests/Runs/Settings/Rules are kept forever.

## Local Operational Config (`config.yaml`)

Only technical/cost settings live here now — model choice, the signal-type taxonomy, timeouts. The source list and relevance ruleset are **not** here anymore; they live in the Sheet's Settings/Rules tabs so both people can read and edit them without touching code. See `config.py`'s `load_local_config()`.

## Extending This

**Adding another stream** (industry trends, social listening, etc.)
- Create `config_industry_trends.yaml` — same code, different local config
- Add sources to the Settings tab with `stream="industry_trends"` and write that stream's ruleset in the Rules tab
- No code changes needed unless the stream needs a non-RSS adapter

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
│   └── rss_adapter.py          # RSS fetch + full-text extraction
│
├── pipeline/
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

- RSS feeds only (web scraping / page-watching is v2)
- Derived signals (messaging change, pricing trends) require time-series — banked for later
- Semantic dedup (same news, many outlets, different wording) is v2
- `--force-extract` is not supported against the Sheets backend yet (no cheap delete-by-item there) — filter the Findings tab manually if re-extraction is needed
- No UI yet — a Streamlit app is the next planned phase, on top of this same engine

## Testing

- `test_rss_local.py` — RSS adapter logic (mocked feed, no network)
- `test_sheets_backend.py` — Google Sheets backend contract (mocked `gspread`, no real Google account): idempotent tab creation, batched writes, multi-tenant filtering, dedup index, retention cleanup, run-details drill-down
- `test_engine_e2e.py` — the full pipeline through `engine.run_stream()` against `InMemoryBackend` (mocked feed + mocked LLM): quote verification, idempotent re-runs, AI_log completeness, cost tracking

Run any of them directly: `python test_engine_e2e.py`

## Questions?

See `open-questions.md` in the project memory for decisions that need Ksenia's input before real-world deployment.
