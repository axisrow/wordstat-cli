from datetime import UTC, datetime
from pathlib import Path

import pytest

import wordstat.storage as storage_module
from wordstat.errors import InvalidRequestError
from wordstat.models import CollectionManifest, CollectionStatus, ExportSummary, WordstatView
from wordstat.storage import create_run_directory, finalize_raw, load_resume_manifest, slugify, write_manifest


def _manifest(
    phrase: str = "ремонт квартир", region: str = "Москва", status: CollectionStatus = CollectionStatus.IN_PROGRESS
) -> CollectionManifest:
    return CollectionManifest(
        phrase=phrase,
        region=region,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        source_url="https://wordstat.yandex.ru/?words=test",
        exports=[],
        status=status,
    )


def test_create_run_directory_is_unique_and_keeps_cyrillic(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    first = create_run_directory(tmp_path, "Ремонт квартир", now)
    second = create_run_directory(tmp_path, "Ремонт квартир", now)

    assert first.name == "20260820T120000Z-ремонт-квартир"
    assert second.name == "20260820T120000Z-ремонт-квартир-2"


def test_slugify_uses_a_safe_fallback_for_symbols() -> None:
    assert slugify("!!!") == "query"


def test_write_manifest_preserves_cyrillic_metadata(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = _manifest()
    manifest.exports.append(
        ExportSummary(
            view=WordstatView.TOP_POPULAR,
            file="top_popular.parquet",
            raw_file=None,
            row_count=1,
            dtypes={"Запрос": "string"},
        )
    )

    write_manifest(path, manifest)

    assert '"phrase": "ремонт квартир"' in path.read_text(encoding="utf-8")


def test_write_manifest_keeps_the_previous_file_if_atomic_replace_fails(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    original = _manifest(phrase="старый запрос")
    write_manifest(path, original)
    previous_contents = path.read_text(encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated interruption before replacement")

    monkeypatch.setattr(storage_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        write_manifest(path, _manifest(phrase="новый запрос"))

    assert path.read_text(encoding="utf-8") == previous_contents
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []


def test_partial_manifest_is_explicitly_distinct_from_a_complete_manifest(tmp_path: Path) -> None:
    partial_path = tmp_path / "partial.json"
    complete_path = tmp_path / "complete.json"
    write_manifest(partial_path, _manifest())
    write_manifest(complete_path, _manifest(status=CollectionStatus.COMPLETE))

    partial = CollectionManifest.model_validate_json(partial_path.read_text(encoding="utf-8"))
    complete = CollectionManifest.model_validate_json(complete_path.read_text(encoding="utf-8"))

    assert partial.status is CollectionStatus.IN_PROGRESS
    assert complete.status is CollectionStatus.COMPLETE


@pytest.mark.parametrize(
    ("phrase", "region", "message"),
    [("другая фраза", "Москва", "phrase and region"), ("ремонт квартир", "Россия", "phrase and region")],
)
def test_load_resume_manifest_rejects_a_different_query_identity(
    tmp_path: Path, phrase: str, region: str, message: str
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    write_manifest(run_directory / "manifest.json", _manifest())

    with pytest.raises(InvalidRequestError, match=message):
        load_resume_manifest(run_directory, phrase, region)


def test_finalize_raw_removes_the_download_by_default(tmp_path: Path) -> None:
    source = tmp_path / "wordstat-export.csv"
    source.write_text("Запрос;Показов\n", encoding="cp1251")

    kept = finalize_raw(source, tmp_path, WordstatView.TOP_POPULAR, keep_raw=False)

    assert kept is None
    assert not source.exists()
    assert list(tmp_path.glob("*.csv")) == []


def test_finalize_raw_renames_the_download_when_keeping_it(tmp_path: Path) -> None:
    source = tmp_path / "wordstat-export.csv"
    source.write_text("Запрос;Показов\n", encoding="cp1251")

    kept = finalize_raw(source, tmp_path, WordstatView.REGIONS, keep_raw=True)

    assert kept == tmp_path / "regions.csv"
    assert kept.exists()
    assert not source.exists()


def test_finalize_raw_tolerates_an_already_missing_download(tmp_path: Path) -> None:
    assert finalize_raw(tmp_path / "gone.csv", tmp_path, WordstatView.DYNAMICS, keep_raw=False) is None
