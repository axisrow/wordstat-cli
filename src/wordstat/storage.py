"""Run-directory and manifest handling."""

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from wordstat.errors import InvalidRequestError
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
    """Atomically write reproducibility metadata in stable UTF-8 JSON."""

    # pydantic emits UTF-8 without escaping non-ASCII, so the Cyrillic stays
    # readable without a round-trip through the json module.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(manifest.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_manifest(path: Path) -> CollectionManifest:
    """Load a run manifest, preserving a useful domain error for bad input."""

    try:
        return CollectionManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise InvalidRequestError(f"Cannot read manifest {path}: {error}") from error


def validate_resume_directory(run_directory: Path, phrase: str, region: str) -> CollectionManifest:
    """Ensure an explicitly selected run belongs to this exact request."""

    manifest_path = run_directory / "manifest.json"
    if not run_directory.is_dir() or not manifest_path.is_file():
        raise InvalidRequestError(f"Resume directory has no manifest: {run_directory}")
    manifest = read_manifest(manifest_path)
    if manifest.phrase != phrase or manifest.region != region:
        raise InvalidRequestError(
            f"Resume manifest request mismatch: expected phrase={phrase!r}, region={region!r}; "
            f"found phrase={manifest.phrase!r}, region={manifest.region!r}"
        )
    return manifest
