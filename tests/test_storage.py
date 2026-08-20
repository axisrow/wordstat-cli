from datetime import UTC, datetime
from pathlib import Path

from wordstat.models import CollectionManifest, ExportSummary, WordstatView
from wordstat.storage import create_run_directory, slugify, write_manifest


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
                file="top_popular.csv",
                source_file="wordstat.csv",
                row_count=1,
                headers=["Запрос"],
            )
        ],
    )

    write_manifest(path, manifest)

    assert '"phrase": "ремонт квартир"' in path.read_text(encoding="utf-8")
