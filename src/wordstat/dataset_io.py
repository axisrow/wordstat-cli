"""Parquet output for parsed Wordstat exports."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from wordstat.dtypes import infer_column
from wordstat.models import CsvDataset


def write_dataset(dataset: CsvDataset, run_directory: Path) -> tuple[Path, dict[str, str]]:
    """Write one report as ``<view>.parquet`` and report the inferred column types.

    Column order follows the export's own header order.  The returned dtype
    mapping goes into the manifest so an unexpected export format is visible
    there instead of failing silently.
    """

    columns: dict[str, pa.Array] = {}
    dtypes: dict[str, str] = {}
    for header in dataset.headers:
        values = [row[header] for row in dataset.rows]
        inferred = infer_column(values)
        if inferred is None:
            dtypes[header] = "string"
            columns[header] = pa.array(values, type=pa.string())
        else:
            dtype, coerced = inferred
            dtypes[header] = dtype
            columns[header] = pa.array(coerced, type=pa.type_for_alias(dtype))

    table = pa.table(columns)
    destination = run_directory / f"{dataset.view.value}.parquet"
    pq.write_table(table, destination, compression="zstd")
    return destination, dtypes
