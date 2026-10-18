#!/usr/bin/env python3
"""
One-time setup: get the "Inspo Trends" Google Sheet ready — same service
account already authorized for the Competitors sheet (no new Google Cloud
project or service account needed) — and create all its tabs (including the
Sources tab and the Trend-research-only Headlines tab).

Two ways to run it:

1. --sheet-id SHEET_ID   (recommended, and the only option if the service
   account has no Drive storage of its own — the common case for a plain
   service account not on a Google Workspace domain, which is why
   client.create() below can fail with "Drive storage quota exceeded" even
   once the Drive API is enabled). Create a blank sheet yourself first:
       a. sheets.new
       b. Share -> paste the service account's email (in the downloaded
          JSON key as "client_email") -> Editor -> uncheck "Notify people" -> Share
       c. Copy the ID from its URL (between /d/ and /edit)
   Then:  python Trends/create_trends_sheet.py --sheet-id THAT_ID

2. No arguments: tries to create the sheet directly via the service account
   (client.create()) and share it back to you. Only works if the service
   account has its own Drive quota, which most don't.

Either way, prints the sheet's ID — add it to .env as GOOGLE_SHEET_ID_TRENDS.
"""

import argparse
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

    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet-id", help="ID of a blank sheet you already created and shared "
                                            "with the service account (see this file's docstring).")
    args = parser.parse_args()

    credentials_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    credentials_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT")
    if not credentials_path and not credentials_json:
        logger.error("Set GOOGLE_SERVICE_ACCOUNT_JSON (or _CONTENT) in .env first — "
                     "same credentials already used for the Competitors sheet.")
        sys.exit(1)

    if args.sheet_id:
        sheet_id = args.sheet_id
        logger.info(f"Using existing sheet: {sheet_id}")
    else:
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
        sheet_id = spreadsheet.id

    from storage.sheets_backend import SheetsBackend
    from Trends.trend_app import HEADLINES_TAB
    storage = SheetsBackend(
        sheet_id=sheet_id,
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
    print(f"Sheet ID: {sheet_id}")
    print(f"Open it: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    print()
    print("Add this to .env:")
    print(f"GOOGLE_SHEET_ID_TRENDS={sheet_id}")
    print()
    print("Then fill in the Rules tab's General and Trends rows before running "
          "Trend research — see backups/rules-for-other-inspo-parts.md for a "
          "starting draft of the Trends rules text.")


if __name__ == "__main__":
    main()
