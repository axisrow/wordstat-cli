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


def _fake_manifest_with_stale_export_for_regions(phrase: str) -> CollectionManifest:
    """All four views present in exports, including a *stale* REGIONS entry
    (as if a prior run collected it, but the on-disk parquet was later
    deleted) — mirrors --resume-dir's views_to_collect re-attempting a view
    whose export entry survives but whose file doesn't (see storage.py). With
    all four exports present, `missing_views` (computed only from `exports`)
    is empty even though this call's re-collection of REGIONS failed."""
    base = _fake_manifest_with_missing_view(phrase)
    return base.model_copy(
        update={
            "exports": [
                *base.exports,
                ExportSummary(
                    view=WordstatView.REGIONS,
                    file="regions.parquet",
                    raw_file=None,
                    row_count=934,
                    dtypes={"Регион": "string"},
                ),
            ]
        }
    )


def test_batch_resume_reattempt_failure_is_reported_even_when_missing_views_is_empty(
    monkeypatch, tmp_path: Path
):
    """Cycle-review follow-up to issue #27: missing_views is a computed field
    over manifest.exports only. Under --resume-dir, a view can have a stale
    export entry (its parquet was manually deleted, but the manifest still
    lists it) — views_to_collect re-attempts such a view, and if that
    re-attempt fails, view_errors gets an entry for it but missing_views
    stays empty (the stale export entry is still in manifest.exports). Keying
    the CLI's partial-report gate on `missing_views` alone would silently
    report this run as a full success at exit 0 while the view's parquet is
    still absent — the exact "врёт о фактическом результате" failure mode
    issue #27 was about. The gate must be the union of missing_views and
    view_errors."""

    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        run_directory = tmp_path / "чай"
        manifest_path = run_directory / "manifest.json"
        return BatchCollectionResult(
            total=1,
            results=[
                CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=_fake_manifest_with_stale_export_for_regions("чай"),
                    view_errors={WordstatView.REGIONS: "DownloadTimeoutError: simulated resume re-attempt failure"},
                )
            ],
            failures=[],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай"])

    assert result.exit_code == 0
    assert "Собрано 1 из 1, из них частично 1" in result.output
    assert "чай [regions]: DownloadTimeoutError: simulated resume re-attempt failure" in result.output


def test_batch_reports_escaped_download_warnings(monkeypatch, tmp_path: Path):
    """A view that succeeded despite a same-tick escaped download (cycle-
    review follow-up to issue #27) must still surface the warning to the
    operator, even though the phrase is otherwise fully collected."""

    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        run_directory = tmp_path / "чай"
        manifest_path = run_directory / "manifest.json"
        return BatchCollectionResult(
            total=1,
            results=[
                CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=_fake_manifest_with_stale_export_for_regions("чай"),
                    escaped_download_warnings=[
                        "[regions] Chrome reported a download outside the run's downloads directory: stray.csv"
                    ],
                )
            ],
            failures=[],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай"])

    assert result.exit_code == 0
    assert "чай: [regions] Chrome reported a download outside the run's downloads directory" in result.output


def _fake_manifest_all_views_present(phrase: str) -> CollectionManifest:
    """All four views in exports (nothing missing, nothing failed), with
    top_popular/top_related at row_count 0 — the shape of an ordinary live
    Wordstat run (issue #22/#25: those two reports export zero rows every
    time). Before the wordstat-35 fix this read as fully collected."""
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
                row_count=24,
                dtypes={"Дата": "string"},
            ),
            ExportSummary(
                view=WordstatView.REGIONS,
                file="regions.parquet",
                raw_file=None,
                row_count=934,
                dtypes={"Регион": "string"},
            ),
        ],
    )


def _fake_manifest_fully_collected(phrase: str) -> CollectionManifest:
    """Every view present with non-zero rows — the only shape --strict-exit
    accepts as complete."""
    base = _fake_manifest_all_views_present(phrase)
    return base.model_copy(
        update={
            "exports": [
                export.model_copy(update={"row_count": max(export.row_count, 1)})
                for export in base.exports
            ]
        }
    )


def test_empty_views_mark_result_partial_in_the_summary(monkeypatch, tmp_path: Path):
    """wordstat-35: a result whose only defect is a zero-row export (all four
    views present, none failed) must not print a plain "Собрано 1 из 1" —
    manifest.status is "incomplete" for exactly this shape, and the summary
    ignoring it is how CI swallowed an incomplete dataset. The per-view line
    must say the export is empty, not "не собран": the parquet exists with its
    header schema, the data is empty."""

    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        run_directory = tmp_path / "чай"
        manifest_path = run_directory / "manifest.json"
        return BatchCollectionResult(
            total=1,
            results=[
                CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=_fake_manifest_all_views_present("чай"),
                )
            ],
            failures=[],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай"])

    assert result.exit_code == 0
    assert "Собрано 1 из 1, из них частично 1" in result.output
    assert "чай [top_popular]: пустой экспорт (0 строк)" in result.output


def test_strict_exit_on_a_fully_collected_run_exits_zero(monkeypatch, tmp_path: Path):
    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        run_directory = tmp_path / "чай"
        manifest_path = run_directory / "manifest.json"
        return BatchCollectionResult(
            total=1,
            results=[
                CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=_fake_manifest_fully_collected("чай"),
                )
            ],
            failures=[],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай", "--strict-exit"])

    assert result.exit_code == 0


def test_strict_exit_on_a_partial_run_exits_two(monkeypatch, tmp_path: Path):
    """wordstat-35 (production incident, 2026-09-01): a partial run exits 0 by
    default (issue #27 contract, unchanged) — but with --strict-exit it must
    exit 2, so CI keyed on exit codes alone cannot silently accept an
    incomplete dataset."""

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

    result = CliRunner().invoke(main, ["collect", "чай", "--strict-exit"])

    assert result.exit_code == 2


def test_strict_exit_on_a_zero_row_export_run_exits_two(monkeypatch, tmp_path: Path):
    """Zero-row exports count as "not fully collected" under --strict-exit
    too, not only missing/failed views. Consequence, deliberate: on live
    Wordstat the top views export zero rows on every run (issue #22/#25), so
    an otherwise complete live run exits 2 under this flag — "strict" means
    "every requested view produced rows", not "the collector did everything
    it could" (see the comment at the partial_count loop in cli.py)."""

    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        run_directory = tmp_path / "чай"
        manifest_path = run_directory / "manifest.json"
        return BatchCollectionResult(
            total=1,
            results=[
                CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=_fake_manifest_all_views_present("чай"),
                )
            ],
            failures=[],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай", "--strict-exit"])

    assert result.exit_code == 2


def test_strict_exit_on_phrase_failures_still_exits_one(monkeypatch, tmp_path: Path):
    """Failures keep exit code 1 even under --strict-exit: the non-zero codes
    rank severity, and a phrase that collected nothing at all is strictly
    worse than a phrase that collected some views. Collapsing both into 2
    would let a caller keyed to "1 means a phrase needs a full re-run" miss
    that signal in a mixed batch (one dead phrase + one partial), so when
    both conditions hold the heavier code wins."""

    async def fake_collect_many(self, phrases, region="Россия", resume_directory=None):
        run_directory = tmp_path / "чай"
        manifest_path = run_directory / "manifest.json"
        return BatchCollectionResult(
            total=2,
            results=[
                CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=_fake_manifest_all_views_present("чай"),
                )
            ],
            failures=[PhraseFailure(phrase="кофе", error=PhraseEntryError("boom"))],
        )

    monkeypatch.setattr(cli.WordstatCollector, "collect_many", fake_collect_many)

    result = CliRunner().invoke(main, ["collect", "чай", "кофе", "--strict-exit"])

    assert result.exit_code == 1


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


def _readme_text() -> str:
    return (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_readme_documents_that_weekly_ignores_an_explicit_period():
    """Wordstat silently ignores --date-from/--date-to for --granularity
    weekly and returns its default ~2-year window (confirmed by a live run on
    2026-08-31). An undocumented limitation here is worse than most: the
    request is *accepted* (validate_period passes, the picker clicks through),
    so the only honest place to warn the operator is the docs. The wording is
    free to change; these anchors must keep naming weekly, the ignoring, and
    the default window."""
    text = _readme_text()

    assert "weekly" in text
    assert "игнориру" in text
    assert "дефолтное окно" in text


def test_collect_help_documents_the_weekly_default_window():
    """The same weekly limitation must also surface where a user actually
    looks before typing a command: `wordstat collect --help`. Whitespace is
    flattened first: click wraps help text at the terminal width, so the
    anchors must not depend on where a line break happens to fall."""
    result = CliRunner().invoke(main, ["collect", "--help"])

    flattened = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "weekly" in flattened
    assert "default window" in flattened
