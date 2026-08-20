"""CLI-level error handling, without touching a browser."""

from click.testing import CliRunner

from wordstat.cli import main


def test_empty_phrase_is_reported_without_a_traceback():
    result = CliRunner().invoke(main, ["collect", "   "])

    assert result.exit_code != 0
    assert "The search phrase must not be empty" in result.output
    assert "Traceback" not in result.output


def test_empty_region_is_reported_without_a_traceback():
    result = CliRunner().invoke(main, ["collect", "чай", "--region", " "])

    assert result.exit_code != 0
    assert "The region must not be empty" in result.output
    assert "Traceback" not in result.output
