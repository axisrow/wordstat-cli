"""Command-line entry point."""

import asyncio
from pathlib import Path

import click

from wordstat.collector import WordstatCollector
from wordstat.config import load_config
from wordstat.errors import WordstatError
from wordstat.periods import Granularity, parse_date, validate_period
from wordstat.storage import prepare_resume_directory

_CONFIG = load_config()
_DEFAULT_CDP_URL = _CONFIG.get("cdp_url", "http://127.0.0.1:9222")
# Wordstat exports CSV as UTF-8 with BOM; a phrases file typed or saved
# on the same machine can plausibly be in either, and utf-8-sig must come
# before plain utf-8: a BOM'd file decodes successfully under plain
# "utf-8" too, but leaves "﻿" attached to the first line. Neither
# resolve_phrases' line.strip() nor the collector's own phrase.strip()
# removes it ('﻿'.isspace() is False), so a BOM left in is not
# harmless — it silently prepends an invisible character to the first
# phrase, which then propagates into the typed Wordstat search, the
# run-directory slug, and manifest.json. Mirrors the encoding probe in
# csv_io.py.
_PHRASES_FILE_ENCODINGS = ("utf-8-sig", "cp1251")


@click.group()
def main() -> None:
    """Collect validated Yandex Wordstat reports as Parquet datasets."""


def resolve_phrases(phrase_args: tuple[str, ...], phrases_file: Path | None) -> list[str]:
    """Merge PHRASE arguments and --phrases-file into one ordered phrase list.

    Blank lines in the file are dropped; phrases are not deduplicated, so
    requesting the same phrase twice collects it twice into two run
    directories. Whitespace is left untouched here — WordstatCollector is the
    single place that strips and validates each phrase.
    """

    phrases = list(phrase_args)
    if phrases_file is not None:
        lines = _read_phrases_file(phrases_file).splitlines()
        phrases.extend(line for line in lines if line.strip())
    return phrases


def _read_phrases_file(path: Path) -> str:
    data = path.read_bytes()
    decode_errors: list[str] = []
    for encoding in _PHRASES_FILE_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as error:
            decode_errors.append(f"{encoding}: {error.reason}")
    joined = "; ".join(decode_errors)
    raise click.ClickException(f"Cannot decode --phrases-file {path.name} as UTF-8 or cp1251: {joined}")


@main.command()
@click.argument("phrase", nargs=-1)
@click.option(
    "--phrases-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Path to a file with one search phrase per line, collected alongside any PHRASE arguments.",
)
@click.option("--region", default="Россия", show_default=True, help="Exact region label in the Wordstat selector.")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("wordstat-output"),
    show_default=True,
)
@click.option("--cdp-url", envvar="WORDSTAT_CDP_URL", default=_DEFAULT_CDP_URL, show_default=True)
@click.option("--timeout", "timeout_seconds", type=click.FloatRange(min=1), default=45.0, show_default=True)
@click.option(
    "--granularity",
    type=click.Choice(Granularity, case_sensitive=False),
    default=Granularity.MONTHLY,
    show_default=True,
)
@click.option("--date-from", type=str, default=None, help="Dynamics window start (YYYY-MM-DD).")
@click.option("--date-to", type=str, default=None, help="Dynamics window end (YYYY-MM-DD).")
@click.option(
    "--keep-raw",
    is_flag=True,
    default=False,
    help="Keep each downloaded CSV as <view>.csv instead of discarding it after conversion.",
)
@click.option(
    "--resume-dir",
    "resume_dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=None,
    help=(
        "Append missing views to an existing run directory instead of starting a new one. "
        "Requires exactly one PHRASE, matching the phrase/region recorded in that "
        "directory's manifest.json."
    ),
)
@click.pass_context
def collect(
    ctx: click.Context,
    phrase: tuple[str, ...],
    phrases_file: Path | None,
    region: str,
    output_dir: Path,
    cdp_url: str,
    timeout_seconds: float,
    granularity: str,
    date_from: str | None,
    date_to: str | None,
    keep_raw: bool,
    resume_dir: Path | None,
) -> None:
    """Collect all MVP Wordstat reports for one or more PHRASE as Parquet datasets.

    Accepts several PHRASE arguments and/or --phrases-file; all phrases are
    collected inside a single browser session. A failure on one phrase is
    reported and does not stop the rest of the batch.
    """

    phrases = resolve_phrases(phrase, phrases_file)
    selected_granularity = Granularity(granularity.lower())
    try:
        parsed_from = parse_date(date_from)
        parsed_to = parse_date(date_to)
        validate_period(selected_granularity, parsed_from, parsed_to)
    except WordstatError as error:
        raise click.ClickException(str(error)) from error
    if not phrases:
        raise click.ClickException("At least one search phrase is required")
    if resume_dir is not None:
        if len(phrases) != 1:
            raise click.ClickException("--resume-dir requires exactly one phrase")
        # Pre-flight check: prepare_resume_directory is pure filesystem logic
        # (see storage.py), so a bad --resume-dir (wrong phrase/region, no
        # manifest.json, not a directory) can be rejected here before Chrome
        # is even touched, instead of surfacing later as a batch failure.
        # collect_many/​_collect_one still re-validate the same way right
        # before use — this call is a fail-fast convenience, not the only
        # guard against a mismatched directory.
        try:
            prepare_resume_directory(resume_dir, phrases[0], region)
        except WordstatError as error:
            raise click.ClickException(str(error)) from error

    collector = WordstatCollector(
        cdp_url=cdp_url,
        output_root=output_dir,
        timeout_seconds=timeout_seconds,
        keep_raw=keep_raw,
    )
    try:
        collect_kwargs = {"region": region, "resume_directory": resume_dir}
        if selected_granularity is not Granularity.MONTHLY or parsed_from is not None:
            collect_kwargs.update(
                granularity=selected_granularity,
                date_from=parsed_from,
                date_to=parsed_to,
            )
        batch = asyncio.run(collector.collect_many(phrases, **collect_kwargs))
    # Only domain errors become friendly messages; an unexpected ValueError
    # from a dependency should keep its traceback instead of being reworded.
    except WordstatError as error:
        raise click.ClickException(str(error)) from error

    partial_count = 0
    for result in batch.results:
        click.echo(result.manifest_path)
        # issue #27: a result with missing_views is one where at least one
        # view failed (but at least one other succeeded — a phrase where
        # nothing at all was collected never reaches batch.results, see
        # _collect_one's own guard) — surface that per view instead of
        # letting "Собрано N из M" imply every phrase in it is complete.
        # Deliberately NOT keyed off manifest.status: status is
        # "incomplete" whenever empty_views is non-empty too, and
        # top_popular/top_related are legitimately empty on every live
        # Wordstat run (issue #22/#25) — keying success on status would
        # make ordinary complete runs exit 1 again.
        #
        # Union with view_errors, not manifest.missing_views alone (cycle-review
        # follow-up to #27): missing_views is a computed field over
        # manifest.exports only, which under --resume-dir can still list a view
        # as "exported" from a *prior* run even though that view's parquet is
        # gone from disk and this run's re-collection attempt failed for it
        # (views_to_collect re-attempts a view whose export entry survives but
        # whose file doesn't — see storage.py). In that case view_errors has an
        # entry for the view but missing_views is empty, so keying only off
        # missing_views silently reported a run with a missing parquet as a
        # full, error-free success at exit 0 — the exact "врёт о фактическом
        # результате" failure mode issue #27 was about, just from the opposite
        # direction (a stale manifest entry instead of a fresh gap).
        reported_views = set(result.manifest.missing_views) | set(result.view_errors)
        if reported_views:
            partial_count += 1
            for view in sorted(reported_views, key=lambda v: v.value):
                reason = result.view_errors.get(view, "не собран")
                click.echo(f"  {result.manifest.phrase} [{view.value}]: {reason}", err=True)
        for warning in result.escaped_download_warnings:
            click.echo(f"  {result.manifest.phrase}: {warning}", err=True)
    for failure in batch.failures:
        click.echo(f"{failure.phrase}: {failure.error}", err=True)

    skipped = batch.total - len(batch.results) - len(batch.failures)
    # collect_many stopped early when skipped > 0 (a lost authentication makes
    # the whole session unusable) — the remaining phrases were never
    # attempted, so "Собрано N из M" alone would misleadingly read as "all the
    # rest failed".
    suffix = f" (батч прерван, {skipped} фраз(ы) не пробовались)" if skipped else ""
    partial_suffix = f", из них частично {partial_count}" if partial_count else ""
    click.echo(f"Собрано {len(batch.results)} из {batch.total}{partial_suffix}{suffix}", err=True)
    if batch.failures:
        ctx.exit(1)
