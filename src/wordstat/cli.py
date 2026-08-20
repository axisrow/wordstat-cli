"""Command-line entry point."""

import asyncio
from pathlib import Path

import click

from wordstat.collector import WordstatCollector
from wordstat.config import load_config
from wordstat.errors import WordstatError

_CONFIG = load_config()
_DEFAULT_CDP_URL = _CONFIG.get("cdp_url", "http://127.0.0.1:9222")


@click.group()
def main() -> None:
    """Collect validated Yandex Wordstat reports as Parquet datasets."""


@main.command()
@click.argument("phrases", nargs=-1)
@click.option("--phrases-file", type=click.Path(path_type=Path, dir_okay=False), help="File with one phrase per line.")
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
def collect(
    phrases: tuple[str, ...], phrases_file: Path | None, region: str, output_dir: Path,
    cdp_url: str, timeout_seconds: float, keep_raw: bool,
) -> None:
    """Collect all MVP Wordstat reports for one or more PHRASES."""

    file_phrases = []
    if phrases_file:
        try:
            file_phrases = [
                line.strip() for line in phrases_file.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
        except OSError as error:
            raise click.ClickException(f"Could not read phrases file: {error}") from error
    requested = [*phrases, *file_phrases]
    if not requested:
        raise click.UsageError("Provide at least one PHRASE or --phrases-file")

    collector = WordstatCollector(
        cdp_url=cdp_url,
        output_root=output_dir,
        timeout_seconds=timeout_seconds,
        keep_raw=keep_raw,
    )
    try:
        result = asyncio.run(collector.collect_many(phrases=requested, region=region))
    # Only domain errors become friendly messages; an unexpected ValueError
    # from a dependency should keep its traceback instead of being reworded.
    except WordstatError as error:
        raise click.ClickException(str(error)) from error
    for item in result.results:
        click.echo(item.manifest_path)
    click.echo(f"Collected {len(result.results)} of {result.total} phrases.")
    for failure in result.failures:
        click.echo(f"Failed: {failure.phrase or '<empty phrase>'}: {failure.error}", err=True)
    if result.failures:
        raise click.exceptions.Exit(1)
