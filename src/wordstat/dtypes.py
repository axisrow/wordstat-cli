"""Column type inference for Wordstat's localized string values.

Wordstat exports every value as text: counts arrive as ``"5 228 679"`` with
non-breaking thousands separators, and dynamics periods as ``"01.2024"``.
Writing those straight to Parquet would only change the container, so columns
are typed here first.

Inference is driven by the values, never by the column names: Wordstat's
headers are localized and it renames them without warning, so a name-based
schema would fail silently.  A column becomes numeric only when *every*
non-empty value parses; a single outlier keeps the whole column as text.  The
worst case is therefore an untyped column rather than corrupted data.
"""

from __future__ import annotations

import re
from types import NotImplementedType

# Wordstat separates thousands with several different space characters.
_SPACES = (" ", " ", " ", " ", " ")

# Deliberately strict.  ``int()``/``float()`` would accept values that are not
# numbers in this data: "01.2024" (a dynamics period) parses as 1.2024,
# silently destroying it, and "1e5", "1_000", "inf" and "nan" are accepted too.
# Only a comma is a decimal separator here — Wordstat exports with a Russian
# locale — which is precisely what rejects the dotted period format.
_NUMERIC = re.compile(r"^-?[0-9]+(?:,[0-9]+)?$")


def parse_number(value: str) -> int | float | None | NotImplementedType:
    """Parse one Wordstat cell.

    Returns ``None`` for an empty cell (a null, never a zero) and
    ``NotImplemented`` when the value is not a number at all.
    """

    compact = value
    for space in _SPACES:
        compact = compact.replace(space, "")
    if not compact.strip():
        return None
    if not _NUMERIC.match(compact):
        return NotImplemented
    if "," in compact:
        return float(compact.replace(",", "."))
    return int(compact)


def infer_column(values: list[str]) -> tuple[str, list[int | float | None]] | None:
    """Type one column from its values.

    Returns ``None`` when the column must stay text, otherwise the Arrow type
    name and the coerced values.  Parquet requires a single type per column, so
    one fractional value promotes the whole column to ``float64``.
    """

    parsed = [parse_number(value) for value in values]
    if any(item is NotImplemented for item in parsed):
        return None
    # An all-empty column carries no evidence that it is numeric; typing it as
    # int64 full of nulls would be an invention.
    if all(item is None for item in parsed):
        return None
    dtype = "int64" if all(isinstance(item, int) or item is None for item in parsed) else "float64"
    return dtype, parsed
