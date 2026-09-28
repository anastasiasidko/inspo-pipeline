"""
Local operational config — the stuff that stays in a file because it's a
technical/cost lever (model choice, signal taxonomy, timeouts), not a business
judgment call. The source list and the relevance ruleset are NOT here anymore
— those live in the Google Sheet's Settings/Rules tabs (see storage/backend.py)
and are read at the start of every run in engine.run_stream().

Per-stream: looks for config_<stream>.yaml first, falls back to config.yaml.
This is what lets a new stream (industry_trends, social_listening, ...) reuse
all the same code with just a new config file, per the README's extension plan.
"""

import logging
import sys
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ["stream", "extraction_model", "digest_window_days", "signal_types"]


def load_local_config(stream: str, config_path: Optional[str] = None) -> dict:
    """
    Load and validate the local operational config for a stream.
    Raises: SystemExit if the config is invalid or missing required fields.
    """
    if config_path is None:
        per_stream_path = Path(f"config_{stream}.yaml")
        config_path = str(per_stream_path) if per_stream_path.exists() else "config.yaml"

    config_file = Path(config_path)
    if not config_file.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    try:
        with open(config_file) as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.error(f"Invalid YAML in config: {e}")
        sys.exit(1)

    missing = [f for f in REQUIRED_FIELDS if f not in config]
    if missing:
        logger.error(f"Config missing required fields: {', '.join(missing)}")
        sys.exit(1)

    if not config.get("signal_types") or len(config["signal_types"]) < 2:
        logger.error("Config must list at least 2 signal types")
        sys.exit(1)

    logger.info(f"Local config loaded from {config_path}: stream={config['stream']}")
    return config
