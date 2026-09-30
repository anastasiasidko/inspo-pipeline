# Working agreement for this repo

## Git

- **Commit and push routine changes automatically**, without asking each time — the user gave this
  standing authorization on 2026-09-28. Use `origin/main`, descriptive commit messages.
- **Still ask first, every time, for:** force-pushing or rewriting history; changing the repo's
  visibility; pushing to a different or new repo; anything destructive.
- **Never commit secrets, automatic or not.** `.env`, the Google service account key, and the Gmail
  OAuth token/client file stay local-only — `.gitignore` enforces this; if something looks like it
  might be a secret and isn't already covered, flag it instead of committing.
- "Everything on GitHub" means the code and config structure — the code still runs from, and lives
  on, the local machine too. GitHub is the backup and version history on top of that, not a
  replacement for local files.

## This project

- The pipeline reads its sources and rules from a shared Google Sheet, not from files in this repo —
  see `storage/backend.py` and `README.md` for the engine/storage/screen split.
- Offline tests (`test_*.py`) mock every external service (Sheets, Anthropic, Apify, Gmail) and
  should stay runnable with no network and no real credentials.
- **Inspo is split into several parts, one chat each** (to keep any one chat from growing huge), all
  in this same repo. Chats don't share context with each other automatically — **read
  `PROJECT_STATUS.md` at the start of a session**, and check `git status`/`git log` before assuming
  sole ownership of the repo; another part's chat may have unfinished, uncommitted work on disk.
  Update `PROJECT_STATUS.md`'s relevant paragraph after a meaningful chunk of work.
