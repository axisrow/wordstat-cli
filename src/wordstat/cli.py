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
    """Collect validated CSV reports from Yandex Wordstat."""


@main.command()
@click.argument("phrase")
@click.option("--region", default="Россия", show_default=True, help="Exact region label in the Wordstat selector.")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("wordstat-output"),
    show_default=True,
)
@click.option("--cdp-url", envvar="WORDSTAT_CDP_URL", default=_DEFAULT_CDP_URL, show_default=True)
@click.option("--timeout", "timeout_seconds", type=click.FloatRange(min=1), default=45.0, show_default=True)
def collect(phrase: str, region: str, output_dir: Path, cdp_url: str, timeout_seconds: float) -> None:
    """Download all MVP Wordstat reports for PHRASE."""

    collector = WordstatCollector(cdp_url=cdp_url, output_root=output_dir, timeout_seconds=timeout_seconds)
    try:
        result = asyncio.run(collector.collect(phrase=phrase, region=region))
    except (ValueError, WordstatError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(result.manifest_path)
