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
# on the same machine can plausibly be in either. Mirrors the encoding probe
# in csv_io.py, minus utf-8-sig (a phrases file is authored by hand, not
# exported by Wordstat, so a BOM is unlikely but harmless either way).
_PHRASES_FILE_ENCODINGS = ("utf-8", "cp1251")


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

    for result in batch.results:
        click.echo(result.manifest_path)
    for failure in batch.failures:
        click.echo(f"{failure.phrase}: {failure.error}", err=True)

    skipped = batch.total - len(batch.results) - len(batch.failures)
    # collect_many stopped early when skipped > 0 (a lost authentication makes
    # the whole session unusable) — the remaining phrases were never
    # attempted, so "Собрано N из M" alone would misleadingly read as "all the
    # rest failed".
    suffix = f" (батч прерван, {skipped} фраз(ы) не пробовались)" if skipped else ""
    click.echo(f"Собрано {len(batch.results)} из {batch.total}{suffix}", err=True)
    if batch.failures:
        ctx.exit(1)
