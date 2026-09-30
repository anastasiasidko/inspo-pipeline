#!/usr/bin/env python3
"""
One-time setup: create the "Inspo Trends" Google Sheet using the SAME service
account already authorized for the Competitors sheet (no new Google Cloud
project or service account needed), share it with the user, and create all its
tabs (including the Sources tab and the Trend-research-only Headlines tab).

Run this ONCE:
    python Trends/create_trends_sheet.py

Prints the new sheet's ID — add it to .env as GOOGLE_SHEET_ID_TRENDS.
"""

import logging
import os
import sys
from pathlib import Path

# Run from anywhere: make sure the repo root (this file's parent) is importable,
# since this script lives in Trends/ but needs storage.sheets_backend from the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

SHEET_TITLE = "Inspo Trends"
SHARE_WITH_EMAIL = "anastasia.sidko@gmail.com"


def main() -> None:
    import gspread

    credentials_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    credentials_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT")
    if not credentials_path and not credentials_json:
        logger.error("Set GOOGLE_SERVICE_ACCOUNT_JSON (or _CONTENT) in .env first — "
                     "same credentials already used for the Competitors sheet.")
        sys.exit(1)

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    if credentials_json:
        import json
        client = gspread.service_account_from_dict(json.loads(credentials_json), scopes=scopes)
    else:
        client = gspread.service_account(filename=credentials_path, scopes=scopes)

    logger.info(f"Creating spreadsheet: {SHEET_TITLE}")
    spreadsheet = client.create(SHEET_TITLE)
    spreadsheet.share(SHARE_WITH_EMAIL, perm_type="user", role="writer", notify=False)
    logger.info(f"Shared with {SHARE_WITH_EMAIL} as Editor")

    from storage.sheets_backend import SheetsBackend
    from Trends.trend_app import HEADLINES_TAB
    storage = SheetsBackend(
        sheet_id=spreadsheet.id,
        credentials_path=credentials_path,
        credentials_json=credentials_json,
        settings_tab="Sources",
        entity_column="source",
        extra_tabs=HEADLINES_TAB,
    )
    storage.init()
    logger.info("All tabs created (Sources, Rules, Runs, Collected, AI_log, Findings, Digests, "
                "API_usage, Competitor_map, Headlines)")

    print()
    print(f"Sheet ID: {spreadsheet.id}")
    print(f"Open it: https://docs.google.com/spreadsheets/d/{spreadsheet.id}/edit")
    print()
    print("Add this to .env:")
    print(f"GOOGLE_SHEET_ID_TRENDS={spreadsheet.id}")
    print()
    print("Then fill in the Rules tab's General and Trends rows before running "
          "Trend research — see backups/rules-for-other-inspo-parts.md for a "
          "starting draft of the Trends rules text.")


if __name__ == "__main__":
    main()
