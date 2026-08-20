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
    """A parsed CSV export with headers preserved exactly as Wordstat provided them."""

    view: WordstatView
    source_file: Path
    headers: list[str] = Field(min_length=1)
    rows: list[dict[str, str]]


class ExportSummary(BaseModel):
    """Small manifest entry for one validated CSV export."""

    view: WordstatView
    file: str
    source_file: str
    row_count: int
    headers: list[str]


class CollectionManifest(BaseModel):
    """Reproducibility metadata written beside the downloaded CSV files."""

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
