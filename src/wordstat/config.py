"""Optional file-based configuration for CLI defaults.

Values are looked up, lowest to highest priority:

1. Built-in defaults (hardcoded in ``cli.py``).
2. ``wordstat.toml`` (this module).
3. ``WORDSTAT_CDP_URL`` environment variable.
4. ``--cdp-url`` command-line flag.

The config file is entirely optional — if absent, cli.py falls back to its
built-in defaults.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "wordstat.toml"


def _config_search_paths() -> list[Path]:
    """Where to look for the config file, in priority order."""

    return [
        Path.cwd() / CONFIG_FILENAME,
        Path(__file__).resolve().parent.parent.parent / CONFIG_FILENAME,
    ]


def load_config() -> dict[str, Any]:
    """Load ``wordstat.toml`` from the first matching search path.

    Returns an empty dict if no config file exists. Malformed TOML raises
    ``tomllib.TOMLDecodeError`` so the user sees the problem immediately
    instead of silently falling back to defaults.
    """

    for path in _config_search_paths():
        if path.is_file():
            with path.open("rb") as handle:
                return tomllib.load(handle)
    return {}
