"""Command-line entry point."""

import asyncio
from pathlib import Path

import click

from wordstat.collector import WordstatCollector
from wordstat.config import load_config
from wordstat.errors import WordstatError

_CONFIG = load_config()
_DEFAULT_CDP_URL = _CONFIG.get("cdp_url", "http://127.0.0.1:9222")
# Wordstat's native export encoding is cp1251; a phrases file typed or saved
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
    decode_errors: list[str] = []
    for encoding in _PHRASES_FILE_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
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
    "--keep-raw",
    is_flag=True,
    default=False,
    help="Keep each downloaded CSV as <view>.csv instead of discarding it after conversion.",
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
    keep_raw: bool,
) -> None:
    """Collect all MVP Wordstat reports for one or more PHRASE as Parquet datasets.

    Accepts several PHRASE arguments and/or --phrases-file; all phrases are
    collected inside a single browser session. A failure on one phrase is
    reported and does not stop the rest of the batch.
    """

    phrases = resolve_phrases(phrase, phrases_file)
    if not phrases:
        raise click.ClickException("At least one search phrase is required")

    collector = WordstatCollector(
        cdp_url=cdp_url,
        output_root=output_dir,
        timeout_seconds=timeout_seconds,
        keep_raw=keep_raw,
    )
    try:
        batch = asyncio.run(collector.collect_many(phrases, region=region))
    # Only domain errors become friendly messages; an unexpected ValueError
    # from a dependency should keep its traceback instead of being reworded.
    except WordstatError as error:
        raise click.ClickException(str(error)) from error

    for result in batch.results:
        click.echo(result.manifest_path)
    for failure in batch.failures:
        click.echo(f"{failure.phrase}: {failure.error}", err=True)

    attempted = len(batch.results) + len(batch.failures)
    if attempted < batch.total:
        # collect_many stopped early (a lost authentication makes the whole
        # session unusable) — the remaining phrases were never attempted, so
        # "Собрано N из M" would misleadingly read as "all the rest failed".
        skipped = batch.total - attempted
        click.echo(
            f"Собрано {len(batch.results)} из {batch.total}"
            f" (батч прерван, {skipped} фраз(ы) не пробовались)",
            err=True,
        )
    else:
        click.echo(f"Собрано {len(batch.results)} из {batch.total}", err=True)
    if batch.failures:
        ctx.exit(1)
