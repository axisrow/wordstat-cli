"""Validated data exchanged between the browser and filesystem layers."""

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


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
    """Reproducibility metadata written beside the converted Parquet files."""

    phrase: str
    region: str
    created_at: datetime
    source_url: str
    exports: list[ExportSummary]


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
    and ``failures`` together account for every requested phrase.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    results: list[CollectionResult]
    failures: list[PhraseFailure]
