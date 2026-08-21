"""Validated data exchanged between the browser and filesystem layers."""

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, computed_field


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

    ``exports`` is the single source of truth for completeness: both
    ``missing_views`` (every :class:`WordstatView` not yet present in
    ``exports``, in enum declaration order) and ``status`` are *derived* from
    it via ``computed_field`` rather than stored fields, so there is no way
    to construct a manifest where they disagree with ``exports`` — the bug
    this feature exists to avoid (a caller building
    ``CollectionManifest(exports=[])`` without separately remembering to set
    ``missing_views`` would otherwise silently get a manifest that lies about
    being complete). Both are still plain JSON fields in ``manifest.json`` on
    disk (pydantic includes computed fields in ``model_dump_json`` by
    default), which is what makes "incomplete" visible to a reader of the
    file itself, not only on the in-memory object.
    """

    phrase: str
    region: str
    created_at: datetime
    source_url: str
    exports: list[ExportSummary]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def missing_views(self) -> list[WordstatView]:
        present = {export.view for export in self.exports}
        return [view for view in WordstatView if view not in present]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> str:
        return "incomplete" if self.missing_views else "complete"


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
