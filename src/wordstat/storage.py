"""Run-directory and manifest handling."""

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

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
    """Atomically write reproducibility metadata in stable UTF-8 JSON.

    Incremental collection rewrites the manifest after every successful view.
    Writing a temporary file in the destination directory and replacing the
    old file only after the write completes prevents an interrupted rewrite
    from leaving a truncated manifest behind.
    """

    # pydantic emits UTF-8 without escaping non-ASCII, so the Cyrillic stays
    # readable without a round-trip through the json module.
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(manifest.model_dump_json(indent=2) + "\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def load_resume_manifest(run_directory: Path, phrase: str, region: str) -> tuple[Path, CollectionManifest]:
    """Load an explicitly requested partial run after checking its identity.

    A run directory is never selected implicitly: this function only opens the
    exact directory the caller supplied, and refuses to mix its Parquet files
    with a different phrase or region.
    """

    manifest_path = run_directory / "manifest.json"
    if not run_directory.is_dir():
        raise InvalidRequestError(f"Resume run directory does not exist: {run_directory}")
    if not manifest_path.is_file():
        raise InvalidRequestError(f"Resume run directory has no manifest.json: {run_directory}")
    try:
        manifest = CollectionManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        raise InvalidRequestError(f"Cannot read resume manifest: {manifest_path}") from error
    if manifest.phrase != phrase or manifest.region != region:
        raise InvalidRequestError(
            "Resume run identity does not match the requested phrase and region "
            f"({manifest.phrase!r}, {manifest.region!r})"
        )
    return manifest_path, manifest
