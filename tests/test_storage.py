from datetime import UTC, datetime
from pathlib import Path

import pytest

from wordstat.errors import InvalidRequestError
from wordstat.models import CollectionManifest, CollectionStatus, ExportSummary, WordstatView
from wordstat.storage import (
    create_run_directory,
    finalize_raw,
    read_manifest,
    slugify,
    validate_resume_directory,
    write_manifest,
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
    manifest = CollectionManifest(
        phrase="ремонт квартир",
        region="Москва",
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        source_url="https://wordstat.yandex.ru/?words=test",
        exports=[
            ExportSummary(
                view=WordstatView.TOP_POPULAR,
                file="top_popular.parquet",
                raw_file=None,
                row_count=1,
                dtypes={"Запрос": "string"},
            )
        ],
    )

    write_manifest(path, manifest)

    assert '"phrase": "ремонт квартир"' in path.read_text(encoding="utf-8")


def test_write_manifest_does_not_corrupt_existing_file_if_replace_fails(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("old manifest", encoding="utf-8")
    manifest = CollectionManifest(
        phrase="новый", region="Россия", created_at=datetime.now(UTC), source_url="url", exports=[]
    )

    def fail_replace(source, destination):
        raise OSError("simulated interruption")

    monkeypatch.setattr("wordstat.storage.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        write_manifest(path, manifest)
    assert path.read_text(encoding="utf-8") == "old manifest"


def test_manifest_distinguishes_incomplete_run_from_complete(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    partial = CollectionManifest(
        phrase="чай", region="Россия", created_at=datetime.now(UTC), source_url="url", exports=[],
        status=CollectionStatus.INCOMPLETE, missing_views=list(WordstatView),
    )
    write_manifest(path, partial)
    loaded = read_manifest(path)
    assert loaded.status is CollectionStatus.INCOMPLETE
    assert loaded.missing_views == list(WordstatView)

    complete = partial.model_copy(update={"status": CollectionStatus.COMPLETE, "missing_views": []})
    write_manifest(path, complete)
    loaded = read_manifest(path)
    assert loaded.status is CollectionStatus.COMPLETE
    assert loaded.missing_views == []


def test_resume_validation_accepts_same_request_and_rejects_other_request(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    manifest = CollectionManifest(
        phrase="чай", region="Москва", created_at=datetime.now(UTC), source_url="url", exports=[],
        status=CollectionStatus.INCOMPLETE, missing_views=list(WordstatView),
    )
    write_manifest(run / "manifest.json", manifest)

    assert validate_resume_directory(run, "чай", "Москва").phrase == "чай"
    with pytest.raises(InvalidRequestError, match="mismatch"):
        validate_resume_directory(run, "кофе", "Москва")
    with pytest.raises(InvalidRequestError, match="mismatch"):
        validate_resume_directory(run, "чай", "Россия")


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
