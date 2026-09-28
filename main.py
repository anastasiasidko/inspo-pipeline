#!/usr/bin/env python3
"""
Inspo Pipeline — CLI entry point

A thin wrapper over engine.run_stream(). Same engine the future Streamlit app
will call — this is just one more screen on top of it, per the brief's rule
that the engine never knows which screen is in front of it.
"""

import argparse
import logging
import sys

from dotenv import load_dotenv

import engine

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Inspo Pipeline — run one stream end to end"
    )
    parser.add_argument("--stream", default="competitor_monitoring", help="Stream to run")
    parser.add_argument(
        "--mode", default="saved_only",
        choices=["typed_only", "saved_plus_typed", "saved_only"],
        help="Which sources to use this run (default: saved_only)",
    )
    parser.add_argument(
        "--inputs", nargs="*", default=[],
        help="Extra sources for this run only (space-separated), used with --mode typed_only/saved_plus_typed",
    )
    parser.add_argument("--lookback-days", type=int, default=14, help="Digest window in days")
    parser.add_argument("--started-by", default="cli", help="Who's running this (for the Runs log)")
    parser.add_argument("--client-id", default="ksenia")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print, no LLM calls, no writes")
    parser.add_argument("--limit", type=int, default=None, help="Cap articles processed (for testing)")

    args = parser.parse_args()

    logger.info("=" * 70)
    logger.info(f"Starting Inspo Pipeline — {args.stream} (mode={args.mode})")
    logger.info(f"Dry run: {args.dry_run}, Limit: {args.limit}")
    logger.info("=" * 70)

    try:
        result = engine.run_stream(
            stream=args.stream,
            inputs=args.inputs,
            mode=args.mode,
            lookback_days=args.lookback_days,
            started_by=args.started_by,
            client_id=args.client_id,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except ValueError as e:
        logger.error(f"Can't run: {e}")
        logger.error(
            "This pipeline reads its sources and rules from a Google Sheet — "
            "make sure GOOGLE_SHEET_ID and either GOOGLE_SERVICE_ACCOUNT_JSON "
            "or GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT are set in .env. "
            "See README.md's Setup section for the step-by-step service account guide."
        )
        sys.exit(1)

    logger.info("=" * 70)
    logger.info(f"Run {result['run_id']} complete")
    logger.info(result["stats"])
    logger.info("=" * 70)

    print(result["digest"])


if __name__ == "__main__":
    main()
