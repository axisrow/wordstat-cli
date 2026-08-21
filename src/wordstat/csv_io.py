"""CSV decoding for Wordstat downloads.

Wordstat exports are commonly UTF-8 with a BOM and may use a lone CR as the
line terminator.  ``cp1251`` remains a supported fallback for older exports.
"""

import csv
from collections.abc import Iterable
from pathlib import Path

from wordstat.errors import CsvFormatError
from wordstat.models import CsvDataset, WordstatView

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")


class _FallbackDialect(csv.excel):
    """Fallback dialect for a Wordstat separator Sniffer couldn't confirm."""

    delimiter = ";"


_FALLBACK_DELIMITERS = (";", "\t")


def parse_wordstat_csv(path: Path, view: WordstatView) -> CsvDataset:
    """Decode a Wordstat CSV while preserving its localized column names.

    The export format is controlled by the website and can vary per view.  The
    parser consequently validates only the stable CSV contract: a non-empty,
    unique header row and data mapped to those headers.
    """

    text = _read_text(path)
    dialect = _detect_dialect(text)
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    raw_headers = list(reader.fieldnames or [])
    headers = _validate_headers(raw_headers, path)
    rows = [_normalise_row(row, raw_headers, headers) for row in reader]
    rows = [row for row in rows if any(value.strip() for value in row.values())]
    return CsvDataset(view=view, headers=headers, rows=rows)


def _read_text(path: Path) -> str:
    decode_errors: list[str] = []
    for encoding in _ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as error:
            decode_errors.append(f"{encoding}: {error.reason}")
    joined = "; ".join(decode_errors)
    raise CsvFormatError(f"Cannot decode CSV {path.name}: {joined}")


def _detect_dialect(text: str) -> csv.Dialect:
    # Sniffer cannot establish a delimiter from a header-only export.  Do not
    # let its Excel fallback turn commas inside a localized header into
    # columns; Wordstat uses either semicolon or tab depending on the view
    # (see the "Fix the CSV sniffer's tab delimiter" fix), so pick whichever
    # of the two known separators actually appears in the header line rather
    # than hardcoding one.
    lines = text.splitlines()
    if len(lines) < 2:
        return _fallback_dialect(lines[0] if lines else "")
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        return _fallback_dialect(lines[0])


def _fallback_dialect(header_line: str) -> csv.Dialect:
    delimiter = max(_FALLBACK_DELIMITERS, key=header_line.count)
    if header_line.count(delimiter) == 0:
        delimiter = ";"

    class _Dialect(_FallbackDialect):
        pass

    _Dialect.delimiter = delimiter
    return _Dialect()


def _validate_headers(fieldnames: Iterable[str | None] | None, path: Path) -> list[str]:
    if not fieldnames:
        raise CsvFormatError(f"CSV {path.name} does not contain a header row")
    headers = [header.strip() if header else "" for header in fieldnames]
    if not all(headers):
        raise CsvFormatError(f"CSV {path.name} contains an empty header")
    if any(delimiter in header for header in headers for delimiter in _FALLBACK_DELIMITERS):
        raise CsvFormatError(f"CSV {path.name} contains an unparsed delimiter in a header")
    if len(set(headers)) != len(headers):
        raise CsvFormatError(f"CSV {path.name} contains duplicate headers")
    return headers


def _normalise_row(
    row: dict[str | None, str | None], raw_headers: list[str | None], headers: list[str]
) -> dict[str, str]:
    values = zip(raw_headers, headers, strict=True)
    return dict((header, (row.get(raw_header) or "").strip()) for raw_header, header in values)
