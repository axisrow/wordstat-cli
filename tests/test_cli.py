"""CLI-level error handling, without touching a browser."""

from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from wordstat import cli
from wordstat.cli import main, resolve_phrases
from wordstat.errors import PhraseEntryError
from wordstat.models import (
    BatchCollectionResult,
    CollectionManifest,
    CollectionResult,
    ExportSummary,
    PhraseFailure,
    WordstatView,
)


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


def test_no_phrases_at_all_is_reported_without_a_traceback():
    result = CliRunner().invoke(main, ["collect"])

    assert result.exit_code != 0
    assert "At least one search phrase is required" in result.output
    assert "Traceback" not in result.output


def test_resolve_phrases_merges_arguments_and_file(tmp_path: Path):
    phrases_file = tmp_path / "phrases.txt"
    phrases_file.write_text("чай\n\n  кофе  \n", encoding="utf-8")

    phrases = resolve_phrases(("вода",), phrases_file)

    assert phrases == ["вода", "чай", "  кофе  "]


def test_resolve_phrases_without_a_file_returns_only_arguments():
    assert resolve_phrases(("чай", "кофе"), None) == ["чай", "кофе"]


def test_resolve_phrases_drops_blank_lines_from_the_file(tmp_path: Path):
    phrases_file = tmp_path / "phrases.txt"
    phrases_file.write_text("чай\n   \n\nкофе\n", encoding="utf-8")

    assert resolve_phrases((), phrases_file) == ["чай", "кофе"]


def _fake_manifest(phrase: str) -> CollectionManifest:
    return CollectionManifest(
        phrase=phrase,
        region="Россия",
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        source_url="https://wordstat.yandex.ru/?words=" + phrase,
        exports=[],
    )


def test_batch_failure_on_one_phrase_does_not_stop_the_rest(monkeypatch, tmp_path: Path):
    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        assert phrases == ["чай", "кофе"]
        run_directory = tmp_path / "чай"
        manifest_path = run_directory / "manifest.json"
        return BatchCollectionResult(
            total=2,
            results=[
                CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=_fake_manifest("чай"),
                )
            ],
            failures=[PhraseFailure(phrase="кофе", error=PhraseEntryError("boom"))],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай", "кофе"])

    assert result.exit_code == 1
    assert str(tmp_path / "чай" / "manifest.json") in result.output
    assert "кофе: boom" in result.output
    assert "Собрано 1 из 2" in result.output


def test_batch_full_success_exits_zero(monkeypatch, tmp_path: Path):
    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        run_directory = tmp_path / "чай"
        manifest_path = run_directory / "manifest.json"
        return BatchCollectionResult(
            total=1,
            results=[
                CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=_fake_manifest("чай"),
                )
            ],
            failures=[],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай"])

    assert result.exit_code == 0
    assert "Собрано 1 из 1" in result.output


def _fake_manifest_with_missing_view(phrase: str) -> CollectionManifest:
    return CollectionManifest(
        phrase=phrase,
        region="Россия",
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        source_url="https://wordstat.yandex.ru/?words=" + phrase,
        exports=[
            ExportSummary(
                view=WordstatView.TOP_POPULAR,
                file="top_popular.parquet",
                raw_file=None,
                row_count=0,
                dtypes={"Запрос": "string"},
            ),
            ExportSummary(
                view=WordstatView.TOP_RELATED,
                file="top_related.parquet",
                raw_file=None,
                row_count=0,
                dtypes={"Запрос": "string"},
            ),
            ExportSummary(
                view=WordstatView.DYNAMICS,
                file="dynamics.parquet",
                raw_file=None,
                row_count=12,
                dtypes={"Дата": "string"},
            ),
            # regions deliberately absent: missing_views == [REGIONS]
        ],
    )


def test_batch_partial_phrase_exits_zero_and_reports_the_missing_view(monkeypatch, tmp_path: Path):
    """Issue #27: a phrase that collected 3 of 4 views must not print
    "Собрано 0 из 1" or exit non-zero — it belongs in batch.results (not
    batch.failures), same as a fully collected phrase, but the CLI must
    still surface which view is missing and why. Deliberately does NOT
    assert on manifest.status, which is "incomplete" here for an unrelated
    reason (empty_views from the always-empty top_popular/top_related, see
    CLAUDE.md/issue #22) — exit code must not be keyed off that field."""

    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        run_directory = tmp_path / "чай"
        manifest_path = run_directory / "manifest.json"
        return BatchCollectionResult(
            total=1,
            results=[
                CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=_fake_manifest_with_missing_view("чай"),
                    view_errors={WordstatView.REGIONS: "DownloadTimeoutError: simulated"},
                )
            ],
            failures=[],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай"])

    assert result.exit_code == 0
    assert "Собрано 1 из 1, из них частично 1" in result.output
    assert "чай [regions]: DownloadTimeoutError: simulated" in result.output


def test_batch_aborted_early_reports_untried_phrases_distinctly(monkeypatch):
    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        # Only the first of 3 phrases was attempted (and failed) before the
        # batch aborted; the CLI must not read this as "2 more failed".
        return BatchCollectionResult(
            total=3,
            results=[],
            failures=[PhraseFailure(phrase="чай", error=PhraseEntryError("session lost"))],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай", "кофе", "вода"])

    assert result.exit_code == 1
    assert "батч прерван" in result.output
    assert "2 фраз" in result.output


def test_phrases_file_falls_back_to_cp1251(tmp_path: Path):
    phrases_file = tmp_path / "phrases.txt"
    phrases_file.write_bytes("чай\nкофе\n".encode("cp1251"))

    assert resolve_phrases((), phrases_file) == ["чай", "кофе"]


def test_phrases_file_with_a_utf8_bom_does_not_leak_into_the_first_phrase(tmp_path: Path):
    # A BOM'd UTF-8 file decodes successfully under plain "utf-8" with the
    # BOM character left attached to the first line; '﻿'.isspace() is
    # False, so neither resolve_phrases' line.strip() nor the collector's
    # own phrase.strip() removes it. utf-8-sig must be tried before plain
    # utf-8 so the BOM is stripped during decoding itself.
    phrases_file = tmp_path / "phrases.txt"
    phrases_file.write_bytes("ремонт квартир\nдизайн интерьера\n".encode("utf-8-sig"))

    assert resolve_phrases((), phrases_file) == ["ремонт квартир", "дизайн интерьера"]


def test_phrases_file_with_undecodable_bytes_is_reported_without_a_traceback(tmp_path: Path):
    # 0x98 is invalid in both utf-8 (a lone continuation byte) and cp1251
    # (unassigned in that codepage) — one of the few byte values neither
    # fallback encoding can decode.
    phrases_file = tmp_path / "phrases.txt"
    phrases_file.write_bytes(b"\x98")

    result = CliRunner().invoke(main, ["collect", "--phrases-file", str(phrases_file)])

    assert result.exit_code != 0
    assert "Cannot decode --phrases-file" in result.output
    assert "Traceback" not in result.output


def test_resume_dir_with_more_than_one_phrase_is_rejected_before_the_browser_starts(tmp_path: Path):
    resume_dir = tmp_path / "some-run"
    resume_dir.mkdir()

    result = CliRunner().invoke(main, ["collect", "чай", "кофе", "--resume-dir", str(resume_dir)])

    assert result.exit_code != 0
    assert "--resume-dir requires exactly one phrase" in result.output
    assert "Traceback" not in result.output


def test_resume_dir_that_does_not_exist_is_reported_by_click(tmp_path: Path):
    missing = tmp_path / "does-not-exist"

    result = CliRunner().invoke(main, ["collect", "чай", "--resume-dir", str(missing)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_resume_dir_mismatched_phrase_is_rejected_before_the_browser_starts(tmp_path: Path):
    from wordstat.models import CollectionManifest as _Manifest
    from wordstat.storage import write_manifest

    resume_dir = tmp_path / "some-run"
    resume_dir.mkdir()
    write_manifest(
        resume_dir / "manifest.json",
        _Manifest(
            phrase="чай",
            region="Россия",
            created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
            source_url="https://wordstat.yandex.ru/?words=чай",
            exports=[],
        ),
    )

    result = CliRunner().invoke(main, ["collect", "кофе", "--resume-dir", str(resume_dir)])

    assert result.exit_code != 0
    assert "does not match" in result.output
    assert "Traceback" not in result.output
