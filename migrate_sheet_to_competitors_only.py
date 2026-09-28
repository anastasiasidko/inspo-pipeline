"""
One-time: turn the shared "Multi-Agent Pipeline" spreadsheet into the "Inspo competitors" one.

Each Inspo part is its own app with its own spreadsheet, so this removes what only made sense with
several streams in one sheet:
  - the `stream` column (Runs, Collected, Findings, Digests) and the stream/client/kind/value columns (Settings)
  - Rules: the `client_id` column goes, `stream` becomes `part` (General / Competitors), and the rows that belong
    to other parts (industry_trends, social_listening, creative_inspiration) are removed
  - the empty default tab "Sheet1"
  - the spreadsheet is renamed to "Inspo competitors"

Safe to re-run (it only removes what is still there). Take a copy of every tab first: backups/*.json.
Run: python migrate_sheet_to_competitors_only.py
"""

import json
import os
from datetime import datetime

from dotenv import load_dotenv

from storage.sheets_backend import RULE_PARTS, TAB_HEADERS, SheetsBackend

NEW_TITLE = "Inspo competitors"
DROP_COLUMNS = {
    "Settings": ["client_id", "stream", "kind", "value"],
    "Runs": ["client_id", "stream"], "Collected": ["client_id", "stream"],
    "Findings": ["client_id", "stream"], "Digests": ["client_id", "stream"],
    "Rules": ["client_id"],
}


def main():
    load_dotenv()
    sheet = SheetsBackend.from_env()
    book = sheet._spreadsheet

    os.makedirs("backups", exist_ok=True)
    path = f"backups/sheet-backup-{datetime.now():%Y%m%d-%H%M%S}.json"
    with open(path, "w") as f:
        json.dump({"title": book.title, "tabs": {ws.title: ws.get_all_values() for ws in book.worksheets()}},
                  f, indent=2, ensure_ascii=False)
    print(f"Backup written: {path}")

    # Rules: keep only General and Competitors; the other rows are removed (their text is in the backup).
    rules = book.worksheet("Rules")
    rows = rules.get_all_values()
    if rows and rows[0] and rows[0][0] == "stream":
        keep = set(RULE_PARTS)
        for i in range(len(rows) - 1, 0, -1):  # bottom-up so row numbers stay valid
            if rows[i][0] not in keep:
                rules.delete_rows(i + 1)
                print(f"Rules: removed the '{rows[i][0]}' row")
        for i, row in enumerate(rules.get_all_values()[1:], start=2):
            if row[0] in RULE_PARTS:
                rules.update_cell(i, 1, RULE_PARTS[row[0]])
        rules.update_cell(1, 1, "part")

    for tab, names in DROP_COLUMNS.items():
        ws = book.worksheet(tab)
        header = ws.row_values(1)
        for name in sorted((n for n in names if n in header), key=header.index, reverse=True):
            ws.delete_columns(header.index(name) + 1)
            print(f"{tab}: removed column '{name}'")
            header = ws.row_values(1)

    if "Sheet1" in [w.title for w in book.worksheets()]:
        default = book.worksheet("Sheet1")
        if not any(any(c for c in row) for row in default.get_all_values()):
            book.del_worksheet(default)
            print("Removed the empty 'Sheet1' tab")
        else:
            print("Sheet1 has content, left in place")

    if book.title != NEW_TITLE:
        book.update_title(NEW_TITLE)
        print(f"Renamed the spreadsheet to '{NEW_TITLE}'")

    print("\nChecking every tab against what the code expects:")
    ok = True
    for tab, expected in TAB_HEADERS.items():
        actual = book.worksheet(tab).row_values(1)
        same = actual == expected
        ok &= same
        print(f"  {'OK ' if same else 'DIFF'} {tab}" + ("" if same else f"\n     sheet:    {actual}\n     expected: {expected}"))
    print("\nAll good." if ok else "\nSome tabs differ, see above.")


if __name__ == "__main__":
    main()
