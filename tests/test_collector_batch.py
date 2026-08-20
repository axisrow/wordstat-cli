"""Batch-mode behavior of WordstatCollector, without touching a browser.

BrowserSession and the page/DOM interaction are faked; _collect_one is left
to run for real, so these tests exercise collect_many's actual loop (error
isolation, authentication handling, session reuse) rather than a mock of the
whole method.
"""

import asyncio
from datetime import UTC, datetime

import pytest

import wordstat.collector as collector_module
from wordstat.collector import WordstatCollector
from wordstat.errors import AuthenticationRequiredError, InvalidRequestError, PhraseEntryError
from wordstat.models import CollectionManifest, CollectionResult


def _fake_result(tmp_path, phrase) -> CollectionResult:
    run_directory = tmp_path / phrase
    return CollectionResult(
        run_directory=run_directory,
        manifest_path=run_directory / "manifest.json",
        manifest=CollectionManifest(
            phrase=phrase,
            region="Россия",
            created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
            source_url="https://wordstat.yandex.ru/",
            exports=[],
        ),
    )


class _FakePage:
    async def goto(self, url):
        pass

    async def get_url(self):
        return "https://wordstat.yandex.ru/?words=test"


class _FakeSession:
    instances = 0

    def __init__(self, **kwargs):
        type(self).instances += 1
        self.downloaded_files = []

    async def start(self):
        pass

    async def new_page(self):
        return _FakePage()

    async def stop(self):
        pass


async def _noop(*args, **kwargs):
    return None


def _patch_common(monkeypatch):
    _FakeSession.instances = 0
    monkeypatch.setattr(collector_module, "BrowserSession", _FakeSession)
    monkeypatch.setattr(WordstatCollector, "_wait_for", _noop)
    monkeypatch.setattr(WordstatCollector, "_assert_authenticated", _noop)
    monkeypatch.setattr(WordstatCollector, "_set_phrase", _noop)
    monkeypatch.setattr(WordstatCollector, "_set_region", _noop)
    monkeypatch.setattr(WordstatCollector, "_table_snapshot", _noop)


def _patch_collect_one(monkeypatch, behavior):
    async def fake_collect_one(self, page, session, downloads_path, phrase, region, set_region=True):
        return await behavior(phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", fake_collect_one)


def test_collect_many_reuses_one_session_for_all_phrases(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    _patch_collect_one(monkeypatch, lambda phrase: _async(_fake_result(tmp_path, phrase)))

    collector = WordstatCollector("cdp", tmp_path)
    batch = asyncio.run(collector.collect_many(["чай", "кофе", "вода"]))

    assert _FakeSession.instances == 1
    assert batch.total == 3
    assert [r.run_directory.name for r in batch.results] == ["чай", "кофе", "вода"]
    assert batch.failures == []


def test_collect_many_isolates_a_single_phrase_failure(monkeypatch, tmp_path):
    _patch_common(monkeypatch)

    async def flaky(phrase):
        if phrase == "сломается":
            raise PhraseEntryError("boom")
        return _fake_result(tmp_path, phrase)

    _patch_collect_one(monkeypatch, flaky)

    collector = WordstatCollector("cdp", tmp_path)
    batch = asyncio.run(collector.collect_many(["первый", "сломается", "второй"]))

    assert batch.total == 3
    assert [r.run_directory.name for r in batch.results] == ["первый", "второй"]
    assert [f.phrase for f in batch.failures] == ["сломается"]
    assert isinstance(batch.failures[0].error, PhraseEntryError)


def test_collect_many_isolates_an_unexpected_exception_type(monkeypatch, tmp_path):
    """A3: not just the enumerated WordstatError subtypes — any Exception
    (e.g. a pyarrow error from write_dataset, an OSError from
    create_run_directory) must be caught per phrase, not just the four
    previously hardcoded types."""

    _patch_common(monkeypatch)

    async def flaky(phrase):
        if phrase == "сломается":
            raise RuntimeError("unexpected pyarrow-style failure")
        return _fake_result(tmp_path, phrase)

    _patch_collect_one(monkeypatch, flaky)

    collector = WordstatCollector("cdp", tmp_path)
    batch = asyncio.run(collector.collect_many(["первый", "сломается", "второй"]))

    assert [r.run_directory.name for r in batch.results] == ["первый", "второй"]
    assert [f.phrase for f in batch.failures] == ["сломается"]
    assert isinstance(batch.failures[0].error, RuntimeError)


def test_collect_many_aborts_on_lost_authentication_without_trying_the_rest(monkeypatch, tmp_path):
    """A2: AuthenticationRequiredError mid-batch stops the loop instead of
    being retried phrase by phrase."""

    _patch_common(monkeypatch)
    attempted = []

    async def collect_one(self, page, session, downloads_path, phrase, region, set_region=True):
        attempted.append(phrase)
        if phrase == "второй":
            raise AuthenticationRequiredError("session lost")
        return _fake_result(tmp_path, phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", collect_one)

    collector = WordstatCollector("cdp", tmp_path)
    batch = asyncio.run(collector.collect_many(["первый", "второй", "третий"]))

    assert attempted == ["первый", "второй"]  # "третий" was never attempted
    assert [r.run_directory.name for r in batch.results] == ["первый"]
    assert [f.phrase for f in batch.failures] == ["второй"]
    assert isinstance(batch.failures[0].error, AuthenticationRequiredError)
    assert batch.total == 3
    assert len(batch.results) + len(batch.failures) < batch.total


def test_collect_one_checks_authentication_on_every_phrase(monkeypatch, tmp_path):
    """A2: _assert_authenticated must actually run inside _collect_one (not
    just once before the loop in collect_many), or a lost session surfaces
    as a confusing InterfaceChangedError instead of AuthenticationRequiredError."""

    _patch_common(monkeypatch)
    calls = []

    async def counting_assert_authenticated(self, page):
        calls.append(1)

    monkeypatch.setattr(WordstatCollector, "_assert_authenticated", counting_assert_authenticated)
    _patch_collect_one_passthrough(monkeypatch, tmp_path)

    collector = WordstatCollector("cdp", tmp_path)
    asyncio.run(collector.collect_many(["первый", "второй", "третий"]))

    # Once before the loop (collect_many) + once per phrase (_collect_one).
    assert len(calls) == 1 + 3


def _patch_collect_one_passthrough(monkeypatch, tmp_path):
    """Let _collect_one call the real _assert_authenticated, then
    short-circuit the rest of the browser interaction."""

    async def passthrough(self, page, session, downloads_path, phrase, region, set_region=True):
        await self._assert_authenticated(page)
        return _fake_result(tmp_path, phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", passthrough)


def test_collect_many_only_sets_region_for_the_first_phrase(monkeypatch, tmp_path):
    """Live bug found during manual verification: the region control
    (.settings__selected button) is absent from the DOM on the map tab
    (WordstatView.REGIONS), which every phrase's view loop ends on. Calling
    _set_region again for phrase #2+ raised InterfaceChangedError on a live
    Wordstat session. Region persists in the URL across a phrase switch
    without re-selecting it, so collect_many must only request it once."""

    _patch_common(monkeypatch)
    set_region_calls = []

    async def recording_collect_one(self, page, session, downloads_path, phrase, region, set_region=True):
        set_region_calls.append(set_region)
        return _fake_result(tmp_path, phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", recording_collect_one)

    collector = WordstatCollector("cdp", tmp_path)
    asyncio.run(collector.collect_many(["первый", "второй", "третий"]))

    assert set_region_calls == [True, False, False]


def test_collect_many_retries_region_after_the_first_phrase_fails(monkeypatch, tmp_path):
    """Codex review finding (PR #5): set_region was computed as index == 0
    against the original phrase list, so a failure on phrase #1 before
    _set_region succeeded left region unset for the rest of the batch —
    every remaining phrase silently collected with whatever region the
    browser already had, while the manifest kept claiming the requested
    region. Region must be retried until a phrase actually completes with
    it applied, not permanently given up on after the first attempt."""

    _patch_common(monkeypatch)
    set_region_calls = []

    async def recording_collect_one(self, page, session, downloads_path, phrase, region, set_region=True):
        set_region_calls.append(set_region)
        if phrase == "первый":
            raise PhraseEntryError("boom before region confirmed")
        return _fake_result(tmp_path, phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", recording_collect_one)

    collector = WordstatCollector("cdp", tmp_path)
    batch = asyncio.run(collector.collect_many(["первый", "второй", "третий"]))

    assert set_region_calls == [True, True, False]
    assert [f.phrase for f in batch.failures] == ["первый"]
    assert [r.run_directory.name for r in batch.results] == ["второй", "третий"]


def test_collect_many_keeps_results_when_session_stop_raises(monkeypatch, tmp_path):
    """session.stop() failing (e.g. a dropped CDP connection) must not
    discard results/failures already collected."""

    _patch_common(monkeypatch)

    async def failing_stop(self):
        raise RuntimeError("CDP connection dropped")

    monkeypatch.setattr(_FakeSession, "stop", failing_stop)
    _patch_collect_one(monkeypatch, lambda phrase: _async(_fake_result(tmp_path, phrase)))

    collector = WordstatCollector("cdp", tmp_path)
    batch = asyncio.run(collector.collect_many(["чай"]))

    assert [r.run_directory.name for r in batch.results] == ["чай"]


def test_collect_many_rejects_an_empty_phrase_list(tmp_path):
    collector = WordstatCollector("cdp", tmp_path)

    with pytest.raises(InvalidRequestError, match="At least one search phrase"):
        asyncio.run(collector.collect_many([]))


def test_collect_many_rejects_a_blank_phrase_in_the_middle(tmp_path):
    collector = WordstatCollector("cdp", tmp_path)

    with pytest.raises(InvalidRequestError, match="must not be empty"):
        asyncio.run(collector.collect_many(["чай", "   ", "кофе"]))


def test_collect_many_rejects_a_blank_region(tmp_path):
    collector = WordstatCollector("cdp", tmp_path)

    with pytest.raises(InvalidRequestError, match="region must not be empty"):
        asyncio.run(collector.collect_many(["чай"], region="  "))


def test_collect_wraps_collect_many_and_returns_the_single_result(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    _patch_collect_one(monkeypatch, lambda phrase: _async(_fake_result(tmp_path, phrase)))

    collector = WordstatCollector("cdp", tmp_path)
    result = asyncio.run(collector.collect(phrase="чай"))

    assert result.run_directory.name == "чай"


def test_collect_propagates_the_original_exception_type(monkeypatch, tmp_path):
    _patch_common(monkeypatch)

    async def fail(phrase):
        raise PhraseEntryError("boom")

    _patch_collect_one(monkeypatch, fail)

    collector = WordstatCollector("cdp", tmp_path)

    with pytest.raises(PhraseEntryError, match="boom"):
        asyncio.run(collector.collect(phrase="чай"))


async def _async(value):
    return value


def _write_view_csv(path, phrase):
    path.write_text(f"Запрос;Показов\n{phrase};100\n", encoding="cp1251")


def test_collect_one_rescues_the_csv_into_the_run_directory_on_write_failure(monkeypatch, tmp_path):
    """A4: the CSV rescue must trigger for ANY failure past parsing (e.g. a
    write_dataset/pyarrow error), not just CsvFormatError — and the original
    exception type/message must still propagate unwrapped."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector):
        pass

    call_count = {"n": 0}

    async def fake_download(self, page, session, dl_path):
        call_count["n"] += 1
        source = dl_path / f"export-{call_count['n']}.csv"
        _write_view_csv(source, "тест")
        return source

    def failing_write_dataset(dataset, run_directory):
        raise RuntimeError("simulated pyarrow.ArrowInvalid")

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)
    monkeypatch.setattr(collector_module, "write_dataset", failing_write_dataset)

    collector = WordstatCollector("cdp", tmp_path)

    async def run():
        page = _FakePage()
        session = _FakeSession()
        with pytest.raises(RuntimeError, match="simulated pyarrow.ArrowInvalid"):
            await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    asyncio.run(run())

    run_directories = [p for p in tmp_path.glob("runs/*") if p.is_dir()]
    assert len(run_directories) == 1
    rescued = list(run_directories[0].glob("*.csv"))
    assert len(rescued) == 1
    assert not (downloads_path / "export-1.csv").exists()


def test_collect_one_rescue_does_not_error_when_finalize_raw_already_moved_the_file(monkeypatch, tmp_path):
    """A4 edge case: if finalize_raw already relocated/deleted source (i.e.
    the failure happens after a successful conversion of an earlier view, not
    this one), the rescue's `if source.exists()` guard must not raise
    FileNotFoundError on top of the original exception."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector):
        pass

    async def fake_download(self, page, session, dl_path):
        source = dl_path / "export.csv"
        _write_view_csv(source, "тест")
        return source

    view_calls = {"n": 0}

    def flaky_write_dataset(dataset, run_directory):
        from wordstat.dataset_io import write_dataset as real_write_dataset

        view_calls["n"] += 1
        if view_calls["n"] == 1:
            # First view succeeds normally (source gets relocated by
            # finalize_raw right after this returns).
            return real_write_dataset(dataset, run_directory)
        raise RuntimeError("second view fails after the first already succeeded")

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)
    monkeypatch.setattr(collector_module, "write_dataset", flaky_write_dataset)

    collector = WordstatCollector("cdp", tmp_path)

    async def run():
        page = _FakePage()
        session = _FakeSession()
        with pytest.raises(RuntimeError, match="second view fails"):
            await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    # Must raise the original RuntimeError, not a FileNotFoundError from the
    # rescue trying to move an already-moved file.
    asyncio.run(run())
