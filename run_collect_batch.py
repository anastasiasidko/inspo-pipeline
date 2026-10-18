import sys, json, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(message)s")
from dotenv import load_dotenv; load_dotenv(".env")

from adapters.feed_discovery import format_channel_lines, sources_for_competitor
import engine
from storage.sheets_backend import SheetsBackend

exec(open("collect_batch_input.py").read())

storage = SheetsBackend.from_env()
storage.init()

all_sources, all_skipped = [], []
for name, channels in COMPETITORS.items():
    lines_text = format_channel_lines(channels)
    sources, skipped = sources_for_competitor(name, channels)
    all_sources += sources
    all_skipped += skipped
    storage.upsert_competitor("ksenia", "competitor_monitoring", name, lines_text)
    print(f"[{name}] saved to Settings, {len(sources)} collectable source(s), {len(skipped)} skipped")

print("\nSkipped lines:")
for s in all_skipped:
    print(" -", s)

print(f"\nStarting collect-only run on {len(all_sources)} sources (last 30 days)...\n")
result = engine.run_collect_only(
    sources=all_sources, stream="competitor_monitoring", lookback_days=30,
    started_by="anastasia", client_id="ksenia", storage=storage,
)
print("\n=== RESULT ===")
print(json.dumps(result, indent=2, default=str))
