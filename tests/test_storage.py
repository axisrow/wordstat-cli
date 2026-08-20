from datetime import UTC, datetime
from pathlib import Path

from wordstat.models import CollectionManifest, ExportSummary, WordstatView
from wordstat.storage import create_run_directory, finalize_raw, slugify, write_manifest


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
                headers=["Запрос"],
                dtypes={"Запрос": "string"},
            )
        ],
    )

    write_manifest(path, manifest)

    assert '"phrase": "ремонт квартир"' in path.read_text(encoding="utf-8")


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
