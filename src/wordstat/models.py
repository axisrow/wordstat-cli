"""Validated data exchanged between the browser and filesystem layers."""

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from wordstat.periods import Granularity


class WordstatView(StrEnum):
    """The four Wordstat reports exported by the MVP."""

    TOP_POPULAR = "top_popular"
    TOP_RELATED = "top_related"
    DYNAMICS = "dynamics"
    REGIONS = "regions"


class CsvDataset(BaseModel):
    """A parsed CSV export with headers preserved exactly as Wordstat provided them.

    Values stay as text here; column typing happens on the way to Parquet
    (see :mod:`wordstat.dtypes`).
    """

    view: WordstatView
    headers: list[str] = Field(min_length=1)
    rows: list[dict[str, str]]


class ExportSummary(BaseModel):
    """Small manifest entry for one converted report.

    ``raw_file`` names the original download, which is removed unless the run
    was asked to keep it.  ``dtypes`` records the inferred column types so an
    unexpected export format is visible in the manifest; it is keyed by the
    localized column names in their original order, so it doubles as the
    header list.
    """

    view: WordstatView
    file: str
    raw_file: str | None
    row_count: int
    dtypes: dict[str, str]


class CollectionManifest(BaseModel):
    """Reproducibility metadata written beside the converted Parquet files.

    Written incrementally, once per successfully collected view (see
    ``collector._collect_one``), so a run interrupted partway through leaves
    a manifest on disk that honestly describes what it has so far rather than
    none at all.

    ``exports`` is the single source of truth for completeness: ``missing_views``
    (every :class:`WordstatView` not yet present in ``exports``, in enum
    declaration order), ``empty_views`` (views present in ``exports`` whose
    ``row_count`` is zero) and ``status`` are all *derived* from it via
    ``computed_field`` rather than stored fields, so there is no way to
    construct a manifest where they disagree with ``exports`` — the bug this
    feature exists to avoid (a caller building ``CollectionManifest(exports=[])``
    without separately remembering to set ``missing_views`` would otherwise
    silently get a manifest that lies about being complete). All three are
    still plain JSON fields in ``manifest.json`` on disk (pydantic includes
    computed fields in ``model_dump_json`` by default), which is what makes
    "incomplete" visible to a reader of the file itself, not only on the
    in-memory object.

    ``empty_views`` exists because of issue #22/#16: ``TOP_POPULAR``/
    ``TOP_RELATED`` are collected and recorded in ``exports`` even when
    Wordstat returns them empty (a permanent property of those two reports on
    live Wordstat, not a collection failure — see
    ``collector._is_untrustworthy_empty_export``), so they are not
    "missing" — the run did produce a file for them, with the header schema
    preserved (issue #18). But a manifest with zero missing views and two
    zero-row exports must still not read as an unqualified success, or issue
    #16's original concern (``status: "complete"`` at zero rows) resurfaces
    for exactly the two views this run legitimately can't fill. ``status``
    is therefore ``"incomplete"`` if either ``missing_views`` or
    ``empty_views`` is non-empty, not only the former.

    Three timestamp/URL fields describe when and where the data came from,
    and their semantics differ deliberately once ``--resume-dir`` is in
    play:

    - ``created_at`` is set once, when the run directory is first created,
      and never touched again by a resume. It answers "when did this run
      start", not "when was every view collected" — a resumed run's views
      can legitimately come from different moments in time.
    - ``updated_at`` is the timestamp of the most recent successful write to
      this manifest (the initial write or any later resume), so a reader
      can tell a fresh run from one stitched together over several
      sessions. ``None`` means the manifest predates this field (written by
      an older version of this tool) — it is optional so
      :func:`~wordstat.storage.load_manifest` can still read a
      manifest.json that lacks it, rather than rejecting an otherwise
      resumable directory with a validation error.
    - ``source_url`` reflects the page URL at the time of the *last*
      successful write, not the first: a resume updates it to the current
      run's URL (see ``collector._collect_one``), so it never silently
      keeps pointing at a stale phrase/tab from a previous session.

    A ``model_validator`` rejects a manifest whose ``exports`` contain more
    than one entry for the same :class:`WordstatView`. The normal write path
    (``storage.merge_export``) can never produce that — it keeps exports in
    a dict keyed by view — but this guards
    :func:`~wordstat.storage.load_manifest` against a manifest.json that was
    hand-edited or corrupted on disk before a resume reads it back: without
    this, ``views_to_collect`` would silently pick whichever duplicate entry
    appears first.
    """

    phrase: str
    region: str
    created_at: datetime
    updated_at: datetime | None = None
    source_url: str
    exports: list[ExportSummary]
    granularity: Granularity = Granularity.MONTHLY
    requested_period: dict[str, str] | None = None
    actual_period: dict[str, str] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def missing_views(self) -> list[WordstatView]:
        present = {export.view for export in self.exports}
        return [view for view in WordstatView if view not in present]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def empty_views(self) -> list[WordstatView]:
        return [export.view for export in self.exports if export.row_count == 0]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> str:
        return "incomplete" if self.missing_views or self.empty_views else "complete"

    @model_validator(mode="after")
    def _exports_have_unique_views(self) -> "CollectionManifest":
        views = [export.view for export in self.exports]
        if len(views) != len(set(views)):
            raise ValueError("Manifest contains duplicate view exports")
        return self


class CollectionResult(BaseModel):
    """In-memory result returned by :class:`WordstatCollector`.

    The parsed rows are deliberately not carried here: they are already on disk
    as Parquet, and the manifest describes every export.
    """

    run_directory: Path
    manifest_path: Path
    manifest: CollectionManifest


class PhraseFailure(BaseModel):
    """One phrase's failure inside a batch collection.

    ``error`` carries the original :class:`~wordstat.errors.WordstatError` so
    the CLI can report the real cause instead of a generic message.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    phrase: str
    error: Exception


class BatchCollectionResult(BaseModel):
    """Outcome of collecting several phrases inside one browser session.

    A phrase that failed is recorded in ``failures`` instead of aborting the
    whole batch (see :meth:`WordstatCollector.collect_many`), so ``results``
    and ``failures`` together account for every requested phrase — unless the
    batch was aborted early (e.g. a lost authentication), in which case
    ``len(results) + len(failures) < total`` and the remaining phrases were
    never attempted.
    """

    total: int
    results: list[CollectionResult]
    failures: list[PhraseFailure]
