import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wordstat.errors import DownloadEscapedError, ResumeMismatchError
from wordstat.models import CollectionManifest, ExportSummary, WordstatView
from wordstat.storage import (
    create_run_directory,
    finalize_raw,
    load_manifest,
    merge_export,
    prepare_resume_directory,
    slugify,
    views_to_collect,
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


def test_finalize_raw_refuses_a_source_outside_the_run_directory(tmp_path: Path) -> None:
    """Issue #27 belt-and-suspenders: even if a caller ever passes a source
    path that escaped the collector's own downloads_path (e.g. a stray path
    Chrome reported under the user's real ~/Downloads), finalize_raw must
    never move or delete it — regardless of keep_raw. The file must be left
    exactly where it was and a domain error raised instead of a bare OSError
    (Errno 1 on macOS's TCC-protected ~/Downloads) or, worse, a silent
    unlink() of a file the tool has no business touching."""

    output_root = tmp_path / "wordstat-output"
    run_directory = output_root / "runs" / "20260821T000000Z-test"
    run_directory.mkdir(parents=True)

    # Outside output_root entirely — mirrors a stray download landing under
    # the user's real ~/Downloads, unrelated to --output-dir.
    outside_dir = tmp_path / "not-ours"
    outside_dir.mkdir()
    source = outside_dir / "wordstat_regions.csv"
    source.write_text("Регион;Показов\n", encoding="cp1251")

    with pytest.raises(DownloadEscapedError, match=str(source)):
        finalize_raw(source, run_directory, WordstatView.REGIONS, keep_raw=True)

    assert source.exists()
    assert source.read_text(encoding="cp1251") == "Регион;Показов\n"

    # Same guard must hold for keep_raw=False, where the naive behavior
    # would have been an outright unlink() of the user's file.
    with pytest.raises(DownloadEscapedError, match=str(source)):
        finalize_raw(source, run_directory, WordstatView.REGIONS, keep_raw=False)

    assert source.exists()


def test_finalize_raw_accepts_a_source_inside_output_root_even_when_run_directory_is_elsewhere(
    tmp_path: Path,
) -> None:
    """--resume-dir can point at a run_directory that is not nested under
    --output-dir at all (see prepare_resume_directory: it never requires
    that). A legitimate download sitting inside the batch's shared
    downloads directory (under output_root) must still be accepted even
    though it is not inside run_directory itself — output_root is an
    independently allowed root, not merely a fallback derived from
    run_directory's parents."""

    output_root = tmp_path / "wordstat-output"
    downloads_dir = output_root / ".downloads-abc123"
    downloads_dir.mkdir(parents=True)
    source = downloads_dir / "wordstat_regions.csv"
    source.write_text("Регион;Показов\n", encoding="cp1251")

    # An arbitrary resume directory, deliberately NOT under output_root.
    run_directory = tmp_path / "elsewhere" / "my-resume-dir"
    run_directory.mkdir(parents=True)

    kept = finalize_raw(source, run_directory, WordstatView.REGIONS, keep_raw=True, output_root=output_root)

    assert kept == run_directory / "regions.csv"
    assert kept.exists()
    assert not source.exists()


def _manifest(
    phrase: str = "ремонт квартир",
    region: str = "Москва",
    exports: list[ExportSummary] | None = None,
) -> CollectionManifest:
    return CollectionManifest(
        phrase=phrase,
        region=region,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        source_url="https://wordstat.yandex.ru/?words=test",
        exports=exports if exports is not None else [],
    )


def _export(view: WordstatView, file: str | None = None) -> ExportSummary:
    return ExportSummary(
        view=view,
        file=file or f"{view.value}.parquet",
        raw_file=None,
        row_count=1,
        dtypes={"Запрос": "string"},
    )


# --- status / missing_views visible on disk -------------------------------


def test_manifest_json_marks_an_incomplete_run_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = _manifest(exports=[_export(WordstatView.TOP_POPULAR)])

    write_manifest(path, manifest)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "incomplete"
    assert on_disk["missing_views"] == [
        v.value for v in WordstatView if v != WordstatView.TOP_POPULAR
    ]


def test_manifest_json_marks_a_complete_run_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = _manifest(exports=[_export(view) for view in WordstatView])

    write_manifest(path, manifest)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "complete"
    assert on_disk["missing_views"] == []


def test_manifest_status_and_missing_views_cannot_disagree_with_exports() -> None:
    # Regression guard for the original design flaw: status/missing_views
    # used to be separate stored fields that a caller could set
    # inconsistently with exports (e.g. exports=[] with no missing_views,
    # which would have reported "complete" on zero exports). Both are now
    # computed straight from exports, so there is no constructor call that
    # can produce a disagreement.
    manifest = _manifest(exports=[])

    assert manifest.status == "incomplete"
    assert manifest.missing_views == list(WordstatView)


# --- atomic write ------------------------------------------------------


def test_write_manifest_leaves_the_previous_content_intact_if_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    original = _manifest(phrase="исходная фраза")
    write_manifest(path, original)
    original_bytes = path.read_bytes()

    def failing_replace(*_args, **_kwargs):
        raise OSError("simulated crash between write and replace")

    monkeypatch.setattr("wordstat.storage.os.replace", failing_replace)

    with pytest.raises(OSError, match="simulated crash"):
        write_manifest(path, _manifest(phrase="новая фраза"))

    # The target file was never truncated: it's still exactly the old,
    # valid manifest, not empty and not a half-written mix.
    assert path.read_bytes() == original_bytes
    assert CollectionManifest.model_validate_json(path.read_text(encoding="utf-8")).phrase == "исходная фраза"


def test_write_manifest_leaves_no_temp_files_behind_on_success(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"

    write_manifest(path, _manifest())

    assert [p.name for p in tmp_path.iterdir()] == ["manifest.json"]


def test_write_manifest_leaves_no_temp_files_behind_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    write_manifest(path, _manifest())

    def failing_replace(*_args, **_kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr("wordstat.storage.os.replace", failing_replace)
    with pytest.raises(OSError):
        write_manifest(path, _manifest(phrase="другая фраза"))

    assert [p.name for p in tmp_path.iterdir()] == ["manifest.json"]


def test_write_manifest_writes_the_temp_file_next_to_the_target(tmp_path: Path) -> None:
    # Regression guard: a bare tempfile.mkstemp()/NamedTemporaryFile() call
    # without dir= defaults to the system temp directory, which can sit on a
    # different filesystem than --output-dir. os.replace() across
    # filesystems raises OSError instead of renaming atomically, so the temp
    # file must be created in path.parent.
    nested = tmp_path / "runs" / "some-run"
    path = nested / "manifest.json"

    write_manifest(path, _manifest())

    assert path.exists()
    assert [p.name for p in nested.iterdir()] == ["manifest.json"]


# --- resume: mismatch rejection -----------------------------------------


def test_prepare_resume_directory_rejects_a_directory_without_a_manifest(tmp_path: Path) -> None:
    run_directory = tmp_path / "runs" / "empty"
    run_directory.mkdir(parents=True)

    with pytest.raises(ResumeMismatchError, match="No manifest.json"):
        prepare_resume_directory(run_directory, "ремонт квартир", "Москва")


def test_prepare_resume_directory_rejects_a_non_directory(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("oops", encoding="utf-8")

    with pytest.raises(ResumeMismatchError, match="is not a directory"):
        prepare_resume_directory(not_a_dir, "ремонт квартир", "Москва")


def test_prepare_resume_directory_rejects_corrupt_json(tmp_path: Path) -> None:
    run_directory = tmp_path / "runs" / "broken"
    run_directory.mkdir(parents=True)
    (run_directory / "manifest.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ResumeMismatchError, match="not a valid Wordstat manifest"):
        prepare_resume_directory(run_directory, "ремонт квартир", "Москва")


def test_prepare_resume_directory_rejects_a_different_phrase(tmp_path: Path) -> None:
    run_directory = tmp_path / "runs" / "run"
    run_directory.mkdir(parents=True)
    write_manifest(run_directory / "manifest.json", _manifest(phrase="ремонт квартир"))

    with pytest.raises(ResumeMismatchError, match="does not match"):
        prepare_resume_directory(run_directory, "натяжные потолки", "Москва")


def test_prepare_resume_directory_rejects_a_different_region(tmp_path: Path) -> None:
    run_directory = tmp_path / "runs" / "run"
    run_directory.mkdir(parents=True)
    write_manifest(run_directory / "manifest.json", _manifest(region="Москва"))

    with pytest.raises(ResumeMismatchError, match="does not match"):
        prepare_resume_directory(run_directory, "ремонт квартир", "Санкт-Петербург")


def test_prepare_resume_directory_accepts_whitespace_differences(tmp_path: Path) -> None:
    # collect_many strips phrase/region before storing them in the manifest,
    # so the comparison here must also compare stripped values, or a
    # trailing space on the CLI argument would cause a false rejection.
    run_directory = tmp_path / "runs" / "run"
    run_directory.mkdir(parents=True)
    write_manifest(run_directory / "manifest.json", _manifest(phrase="ремонт квартир", region="Москва"))

    manifest = prepare_resume_directory(run_directory, "  ремонт квартир  ", " Москва ")

    assert manifest.phrase == "ремонт квартир"


# --- resume: views_to_collect / merge_export ------------------------------


def test_views_to_collect_skips_views_recorded_with_their_file_present(tmp_path: Path) -> None:
    run_directory = tmp_path
    (run_directory / "top_popular.parquet").write_bytes(b"data")
    manifest = _manifest(exports=[_export(WordstatView.TOP_POPULAR)])

    pending = views_to_collect(run_directory, manifest)

    assert pending == [v for v in WordstatView if v != WordstatView.TOP_POPULAR]


def test_views_to_collect_does_not_trust_a_manifest_entry_whose_file_was_deleted(tmp_path: Path) -> None:
    # The parquet for top_popular was removed by hand after a successful
    # write; the manifest entry alone must not be enough to skip it again.
    run_directory = tmp_path
    manifest = _manifest(exports=[_export(WordstatView.TOP_POPULAR)])

    pending = views_to_collect(run_directory, manifest)

    assert pending == list(WordstatView)


def test_merge_export_adds_a_missing_view_without_touching_existing_ones() -> None:
    existing = _export(WordstatView.TOP_POPULAR)
    manifest = _manifest(exports=[existing])

    updated = merge_export(manifest, _export(WordstatView.REGIONS))

    by_view = {item.view: item for item in updated.exports}
    assert by_view[WordstatView.TOP_POPULAR] == existing
    assert WordstatView.REGIONS in by_view
    assert updated.missing_views == [WordstatView.TOP_RELATED, WordstatView.DYNAMICS]


def test_merge_export_orders_exports_by_view_declaration_not_append_order() -> None:
    manifest = _manifest(exports=[])

    manifest = merge_export(manifest, _export(WordstatView.REGIONS))
    manifest = merge_export(manifest, _export(WordstatView.TOP_POPULAR))

    assert [item.view for item in manifest.exports] == [WordstatView.TOP_POPULAR, WordstatView.REGIONS]


def test_merge_export_marks_the_manifest_complete_once_every_view_is_present() -> None:
    manifest = _manifest(exports=[])

    for view in WordstatView:
        manifest = merge_export(manifest, _export(view))

    assert manifest.missing_views == []
    assert manifest.status == "complete"


# --- load_manifest ---------------------------------------------------------


def test_load_manifest_round_trips_what_write_manifest_wrote(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    original = _manifest(phrase="ремонт квартир", exports=[_export(WordstatView.TOP_POPULAR)])
    write_manifest(path, original)

    loaded = load_manifest(path)

    assert loaded.phrase == "ремонт квартир"
    assert [item.view for item in loaded.exports] == [WordstatView.TOP_POPULAR]


def test_load_manifest_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ResumeMismatchError, match="No manifest.json"):
        load_manifest(tmp_path / "manifest.json")


# --- fsync before os.replace (write_manifest atomicity, cheri-pick #1) -----


def test_write_manifest_fsyncs_before_replacing_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A test that only asserts fsync was *called* would also pass if fsync
    # ran after os.replace, where it can no longer prevent a torn write.
    # Record the actual order both land in.
    path = tmp_path / "manifest.json"
    order = []

    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def recording_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr("wordstat.storage.os.fsync", recording_fsync)
    monkeypatch.setattr("wordstat.storage.os.replace", recording_replace)

    write_manifest(path, _manifest())

    assert order == ["fsync", "replace"]


# --- unique-view validator (cheri-pick #2) ---------------------------------


def test_collection_manifest_rejects_duplicate_view_exports() -> None:
    with pytest.raises(ValueError, match="duplicate view exports"):
        CollectionManifest(
            phrase="ремонт квартир",
            region="Москва",
            created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
            source_url="https://wordstat.yandex.ru/?words=test",
            exports=[_export(WordstatView.DYNAMICS), _export(WordstatView.DYNAMICS)],
        )


def test_load_manifest_rejects_a_hand_edited_file_with_duplicate_views(tmp_path: Path) -> None:
    # The threat this guards against is a corrupted/hand-edited file on
    # disk, read back by a resume — not a constructor call in this
    # codebase's own code (merge_export can't produce this). ValidationError
    # is a ValueError subclass, so load_manifest's existing `except
    # ValueError` must turn it into the same ResumeMismatchError a resuming
    # caller already handles.
    path = tmp_path / "manifest.json"
    write_manifest(path, _manifest(exports=[_export(WordstatView.DYNAMICS)]))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["exports"].append(dict(raw["exports"][0]))  # duplicate the one export
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ResumeMismatchError, match="not a valid Wordstat manifest"):
        load_manifest(path)


def test_merge_export_does_not_reintroduce_duplicate_views() -> None:
    # merge_export replaces by view in a dict, so re-merging the same view
    # must not somehow produce two entries — confirms the validator and the
    # write path agree rather than one silently working around the other.
    manifest = _manifest(exports=[_export(WordstatView.DYNAMICS)])

    updated = merge_export(manifest, _export(WordstatView.DYNAMICS, file="dynamics-v2.parquet"))

    assert [e.view for e in updated.exports] == [WordstatView.DYNAMICS]
    assert updated.exports[0].file == "dynamics-v2.parquet"


# --- updated_at optionality / round-trip (prerequisite for bugfix #3) ------


def test_updated_at_defaults_to_none() -> None:
    assert _manifest().updated_at is None


def test_load_manifest_accepts_a_manifest_json_written_before_updated_at_existed(tmp_path: Path) -> None:
    # A manifest.json on disk from an older version of this tool has no
    # "updated_at" key at all. load_manifest must still validate it (as
    # None), not reject an otherwise perfectly resumable directory.
    path = tmp_path / "manifest.json"
    payload = json.loads(_manifest(exports=[_export(WordstatView.TOP_POPULAR)]).model_dump_json())
    del payload["updated_at"]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    loaded = load_manifest(path)

    assert loaded.updated_at is None


# --- merge_export bumps updated_at (prerequisite for bugfix #3) ------------


def test_merge_export_sets_updated_at_to_the_given_now() -> None:
    now = datetime(2026, 8, 21, 9, 30, tzinfo=UTC)
    manifest = _manifest(exports=[])

    updated = merge_export(manifest, _export(WordstatView.TOP_POPULAR), now=now)

    assert updated.updated_at == now


def test_merge_export_does_not_touch_created_at() -> None:
    manifest = _manifest(exports=[])
    original_created_at = manifest.created_at

    updated = merge_export(manifest, _export(WordstatView.TOP_POPULAR), now=datetime(2026, 8, 22, tzinfo=UTC))

    assert updated.created_at == original_created_at
