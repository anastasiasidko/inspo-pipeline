#!/usr/bin/env python3
"""
One-time migration: push the sources and relevance ruleset that used to live
in config.yaml into the connected Google Sheet's Settings and Rules tabs.

Run this ONCE, after the service account is set up and GOOGLE_SHEET_ID /
GOOGLE_SERVICE_ACCOUNT_JSON are in .env, to seed the sheet for the first time.
Safe to re-run — add_saved_item / save_rules don't delete anything first, so
re-running will add duplicate Settings rows if the sheet already has them;
check the Settings tab before running twice.

Usage:
    python migrate_config_to_sheets.py
"""

import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

STREAM = "competitor_monitoring"
CLIENT_ID = "ksenia"
MIGRATED_BY = "migration_script"

# Starter sources to seed into the Settings tab (currently none).
SOURCES = []  # add your own {name, feed_url} entries here, or add them in the Sheet's Settings tab

# The ruleset that used to live in config.yaml's `relevance_ruleset:` field.
RELEVANCE_RULESET = """Flag anything that signals a change in how a competitor competes, positions
itself, or presents itself to customers. This includes but is not limited to:
- Product features, launches, and deprecations
- Pricing changes
- Messaging, visual identity, or branding shifts
- Leadership or team changes
- Strategic partnerships or market moves
- Content themes or campaign launches

If you're unsure whether something qualifies, err toward including it and
explain your reasoning in the summary. Do not flag routine housekeeping
(typo fixes, minor formatting) or generic content with no company-specific info."""


def main():
    from storage.sheets_backend import SheetsBackend

    storage = SheetsBackend.from_env()
    storage.init()

    existing = storage.get_saved_items(CLIENT_ID, STREAM, active_only=False)
    existing_values = {row["value"] for row in existing}

    added = 0
    for source in SOURCES:
        if source["feed_url"] in existing_values:
            logger.info(f"Already in Settings, skipping: {source['name']}")
            continue
        storage.add_saved_item(
            CLIENT_ID, STREAM, kind="competitor", value=source["feed_url"],
            added_by=MIGRATED_BY, notes=source["name"],
        )
        added += 1
        logger.info(f"Added to Settings: {source['name']} ({source['feed_url']})")

    storage.save_rules(CLIENT_ID, STREAM, RELEVANCE_RULESET, edited_by=MIGRATED_BY)
    logger.info("Rules tab updated")

    logger.info(f"Migration complete — {added} new source(s) added, rules saved")


if __name__ == "__main__":
    main()
