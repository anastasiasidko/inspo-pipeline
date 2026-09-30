# Trend research

The second stream in the Inspo app (see the main `README.md`), tracking journals/newsletters/other
research sources instead of competitors. Shares the same engine, pipeline phases, adapters, and storage
interface as Competitors — everything reused lives at the repo root, not here. This folder holds only
what's specific to this stream:

- `trend_app.py` — the four screens (names → links → headline approval → digest), called from the root
  `app.py`'s "Trend research" sidebar mode.
- `config_trend_research.yaml` — model choice, signal-type taxonomy, etc. for this stream (same shape as
  the root `config.yaml`). Loaded explicitly by path since `config.py`'s `config_<stream>.yaml`-in-cwd
  convention doesn't reach into this subfolder.
- `create_trends_sheet.py` — one-off script that creates and shares the "Inspo Trends" spreadsheet using
  the same service account already set up for Competitors. Run once; see the main README's Setup §5.

## What's different from Competitors

1. **No AI channel discovery.** You already know your journals/newsletters, so you type each one's name
   and paste its RSS/website/newsletter link directly — no web-search cost for finding channels.
2. **Headline-approval gate.** Scraping happens in two steps: first just the headline/title/link of each
   item (`engine.run_headline_scan` — Fetch + Dedup only, no AI cost), then you Approve or Disapprove each
   one, and only approved items go through Extract → Score → Synthesize (`engine.run_digest_from_headlines`).
3. **Its own spreadsheet**, "Inspo Trends" — never mixed with the Competitors sheet. Same schema, except
   the `Settings` tab is called `Sources` (column `source` instead of `competitor`), and there's one extra
   tab, `Headlines`, logging every scraped item and your approve/disapprove decision for QA. See the main
   README's **Sheet Schema** section for the exact columns.

## Setup

See the main `README.md`'s Setup section, step 5. In short: enable the Google Drive API for the same
Cloud project (Sheets API alone isn't enough to create a new spreadsheet), run
`python Trends/create_trends_sheet.py`, add the printed ID to `.env` as `GOOGLE_SHEET_ID_TRENDS`, then
fill in the new sheet's Rules tab (General + Trends rows) before running a scan —
`backups/rules-for-other-inspo-parts.md` has a starting draft for the Trends rules text.
