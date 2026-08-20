"""Validated data exchanged between the browser and filesystem layers."""

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


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
    source_file: Path
    headers: list[str] = Field(min_length=1)
    rows: list[dict[str, str]]


class ExportSummary(BaseModel):
    """Small manifest entry for one converted report.

    ``raw_file`` names the original download, which is removed unless the run
    was asked to keep it.  ``dtypes`` records the inferred column types so an
    unexpected export format is visible in the manifest.
    """

    view: WordstatView
    file: str
    raw_file: str | None
    row_count: int
    headers: list[str]
    dtypes: dict[str, str]


class CollectionManifest(BaseModel):
    """Reproducibility metadata written beside the converted Parquet files."""

    phrase: str
    region: str
    created_at: datetime
    source_url: str
    exports: list[ExportSummary]


class CollectionResult(BaseModel):
    """In-memory result returned by :class:`WordstatCollector`."""

    run_directory: Path
    manifest_path: Path
    manifest: CollectionManifest
    datasets: list[CsvDataset]
