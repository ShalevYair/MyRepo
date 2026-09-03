"""Loads pipeline/config.yaml. Every pipeline program reads its thresholds,
paths, and patterns through this — nothing is hardcoded in the programs
themselves (WORKPLAN.md 0.2)."""
from __future__ import annotations

import pathlib

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config.yaml"


class ConfigError(Exception):
    pass


def load(path: pathlib.Path | str | None = None) -> dict:
    """Load and return the pipeline config as a plain dict.

    Raises ConfigError with an actionable message if PyYAML isn't
    installed or the file is missing/malformed — never fails silently
    or falls back to made-up defaults for a config a program needs.
    """
    try:
        import yaml
    except ImportError as e:
        raise ConfigError(
            "PyYAML is required to read config.yaml. Install it with: "
            "pip install -r pipeline/requirements.txt"
        ) from e

    cfg_path = pathlib.Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")

    with cfg_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ConfigError(f"{cfg_path} did not parse to a mapping (got {type(data).__name__})")

    return data
