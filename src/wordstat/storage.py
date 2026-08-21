"""Run-directory and manifest handling."""

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from wordstat.errors import ResumeMismatchError
from wordstat.models import CollectionManifest, ExportSummary, WordstatView


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
    """Write reproducibility metadata in stable UTF-8 JSON, atomically.

    The manifest is written once per phrase after every successfully
    collected view (see ``collector._collect_one``), not only at the very
    end — a run interrupted mid-batch should leave a manifest that honestly
    describes what it managed to collect, marked ``status: "incomplete"``
    (see :class:`~wordstat.models.CollectionManifest`), rather than nothing.

    That means this function can now overwrite an *existing*, previously
    valid manifest.json — plain ``Path.write_text`` truncates the file before
    writing the new content, so a crash mid-write (process killed, disk full)
    would leave a truncated or empty file where a good manifest used to be.
    Write to a temporary file in the same directory first, then atomically
    replace the target with ``os.replace`` (same filesystem, so it can't fail
    partway through) — the target is always either the old content or the
    new content, never a half-written mix. ``dir=path.parent`` is required,
    not cosmetic: a bare ``tempfile`` call defaults to the system temp
    directory, and ``os.replace`` across filesystems raises ``OSError``
    instead of renaming atomically.

    ``os.replace`` only guarantees the *ordering* of the rename, not that the
    temporary file's bytes have actually reached disk — on a power loss or
    kernel panic the rename can land while the data behind it is still only
    in a page cache buffer, leaving an empty or garbage manifest.json where a
    valid one used to be. ``flush()`` (drain Python's own buffer) followed by
    ``os.fsync()`` (ask the OS to commit it to disk) before the ``replace``
    closes that gap; the whole point of this file is surviving a crash
    mid-run, so this one extra syscall is worth it.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".manifest-", suffix=".json.tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # pydantic emits UTF-8 without escaping non-ASCII, so the
            # Cyrillic stays readable without a round-trip through json.
            handle.write(manifest.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        # os.replace already moved the temp file away on the success path,
        # so this only fires (and only then removes anything) when writing
        # or replace() itself failed partway through.
        tmp_path.unlink(missing_ok=True)


def load_manifest(path: Path) -> CollectionManifest:
    """Read back a previously written manifest.json.

    Raises ``ResumeMismatchError`` if the file is missing or is not valid
    manifest JSON, so a caller resuming a run gets a clear domain error
    instead of a bare FileNotFoundError/ValidationError.
    """

    if not path.is_file():
        raise ResumeMismatchError(f"No manifest.json found at {path}")
    try:
        return CollectionManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ResumeMismatchError(f"{path} is not a valid Wordstat manifest: {error}") from error


def prepare_resume_directory(run_directory: Path, phrase: str, region: str) -> CollectionManifest:
    """Validate an existing run directory for resuming, and return its manifest.

    A resume directory must already hold a manifest.json for the *same*
    request (phrase and region, compared post-strip since both are
    normalized before being stored — see collector.collect_many) — otherwise
    a typo'd --resume-dir would silently splice a second phrase's exports
    into the first phrase's manifest and run directory. This is the primary
    data-corruption risk called out for this feature, so the check is a hard
    reject (``ResumeMismatchError``), never a best-effort guess.

    A view counts as already collected only when *both* its manifest entry
    and its `<view>.parquet` file are present on disk: if the parquet was
    deleted by hand after a successful write, the manifest alone must not be
    trusted, or resume would report "complete" over a missing file. The
    reverse case — a parquet on disk with no manifest entry (a crash between
    writing the parquet and rewriting the manifest) — is handled naturally:
    the caller re-collects and overwrites that view's file, which is
    harmless since the file wasn't accounted for anywhere yet.
    """

    if not run_directory.is_dir():
        raise ResumeMismatchError(f"--resume-dir {run_directory} is not a directory")

    manifest = load_manifest(run_directory / "manifest.json")
    if manifest.phrase.strip() != phrase.strip() or manifest.region.strip() != region.strip():
        raise ResumeMismatchError(
            f"--resume-dir {run_directory} was collected for phrase={manifest.phrase!r} "
            f"region={manifest.region!r}, which does not match the requested "
            f"phrase={phrase!r} region={region!r}"
        )
    return manifest


def views_to_collect(run_directory: Path, manifest: CollectionManifest) -> list[WordstatView]:
    """Return the views still missing from a resumed run, in enum order."""

    done = {
        export.view
        for export in manifest.exports
        if (run_directory / export.file).is_file()
    }
    return [view for view in WordstatView if view not in done]


def merge_export(
    manifest: CollectionManifest, export: ExportSummary, now: datetime | None = None
) -> CollectionManifest:
    """Return a copy of ``manifest`` with one more view recorded.

    Keeps ``exports`` ordered by :class:`WordstatView` declaration order
    (not append order) so a resumed run's manifest looks the same as one
    collected in a single pass. ``missing_views``/``empty_views``/``status``
    are computed fields derived straight from ``exports`` (see
    :class:`~wordstat.models.CollectionManifest`), so updating only
    ``exports`` here is enough to keep them correct — there is nothing else
    to recompute for those three.

    ``updated_at`` is bumped to ``now`` (defaulting to the current UTC time,
    like :func:`create_run_directory` — a caller can pass a fixed value for
    deterministic tests) on every call, since every call records a real
    write to the manifest. This is a pure function, so it never reads the
    clock unless the caller lets it: no hidden ``datetime.now()`` unless
    ``now`` is left unset.
    """

    by_view = {item.view: item for item in manifest.exports}
    by_view[export.view] = export
    exports = [by_view[view] for view in WordstatView if view in by_view]
    return manifest.model_copy(update={"exports": exports, "updated_at": now or datetime.now(UTC)})
