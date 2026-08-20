"""Run-directory and manifest handling."""

import re
from datetime import UTC, datetime
from pathlib import Path

from wordstat.models import CollectionManifest, WordstatView


def create_run_directory(output_root: Path, phrase: str, now: datetime | None = None) -> Path:
    """Create a unique, non-destructive directory for a collection run."""

    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    stem = slugify(phrase)
    base = output_root / "runs" / f"{timestamp}-{stem}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{suffix}")
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def slugify(value: str) -> str:
    """Produce a short filesystem-safe slug without losing Cyrillic readability."""

    compact = re.sub(r"[^0-9A-Za-zА-Яа-я]+", "-", value.strip()).strip("-").lower()
    return compact[:64] or "query"


def finalize_raw(source: Path, run_directory: Path, view: WordstatView, keep_raw: bool) -> Path | None:
    """Dispose of a download once it has been converted to Parquet.

    By default the raw CSV is removed, leaving only the converted datasets in
    the run directory.  With ``keep_raw`` it is renamed to the view's canonical
    name so it sits next to its ``<view>.parquet`` counterpart.
    """

    if not keep_raw:
        source.unlink(missing_ok=True)
        return None
    destination = run_directory / f"{view.value}.csv"
    return source.replace(destination)


def write_manifest(path: Path, manifest: CollectionManifest) -> None:
    """Write reproducibility metadata in stable UTF-8 JSON."""

    # pydantic emits UTF-8 without escaping non-ASCII, so the Cyrillic stays
    # readable without a round-trip through the json module.
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
