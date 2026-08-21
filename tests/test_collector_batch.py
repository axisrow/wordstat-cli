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
from wordstat.errors import (
    AuthenticationRequiredError,
    DownloadTimeoutError,
    InvalidRequestError,
    PhraseEntryError,
    ResumeMismatchError,
)
from wordstat.models import CollectionManifest, CollectionResult, WordstatView
from wordstat.storage import load_manifest


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
    def __init__(self, url: str = "https://wordstat.yandex.ru/?words=test"):
        self.url = url

    async def goto(self, url):
        pass

    async def get_url(self):
        return self.url


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
    async def fake_collect_one(
        self,
        page,
        session,
        downloads_path,
        phrase,
        region,
        set_region=True,
        on_region_applied=None,
        resume_directory=None,
    ):
        return await behavior(phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", fake_collect_one)


def test_collect_many_reuses_one_session_for_all_phrases(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    _patch_collect_one(monkeypatch, lambda phrase: _async(_fake_result(tmp_path, phrase)))

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
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

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
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

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
    batch = asyncio.run(collector.collect_many(["первый", "сломается", "второй"]))

    assert [r.run_directory.name for r in batch.results] == ["первый", "второй"]
    assert [f.phrase for f in batch.failures] == ["сломается"]
    assert isinstance(batch.failures[0].error, RuntimeError)


def test_collect_many_aborts_on_lost_authentication_without_trying_the_rest(monkeypatch, tmp_path):
    """A2: AuthenticationRequiredError mid-batch stops the loop instead of
    being retried phrase by phrase."""

    _patch_common(monkeypatch)
    attempted = []

    async def collect_one(
        self,
        page,
        session,
        downloads_path,
        phrase,
        region,
        set_region=True,
        on_region_applied=None,
        resume_directory=None,
    ):
        attempted.append(phrase)
        if phrase == "второй":
            raise AuthenticationRequiredError("session lost")
        return _fake_result(tmp_path, phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", collect_one)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
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

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
    asyncio.run(collector.collect_many(["первый", "второй", "третий"]))

    # Once before the loop (collect_many) + once per phrase (_collect_one).
    assert len(calls) == 1 + 3


def _patch_collect_one_passthrough(monkeypatch, tmp_path):
    """Let _collect_one call the real _assert_authenticated, then
    short-circuit the rest of the browser interaction."""

    async def passthrough(
        self,
        page,
        session,
        downloads_path,
        phrase,
        region,
        set_region=True,
        on_region_applied=None,
        resume_directory=None,
    ):
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

    async def recording_collect_one(
        self,
        page,
        session,
        downloads_path,
        phrase,
        region,
        set_region=True,
        on_region_applied=None,
        resume_directory=None,
    ):
        set_region_calls.append(set_region)
        if set_region and on_region_applied is not None:
            on_region_applied()
        return _fake_result(tmp_path, phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", recording_collect_one)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
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

    async def recording_collect_one(
        self,
        page,
        session,
        downloads_path,
        phrase,
        region,
        set_region=True,
        on_region_applied=None,
        resume_directory=None,
    ):
        set_region_calls.append(set_region)
        if phrase == "первый":
            # Fails before the region would have been confirmed.
            raise PhraseEntryError("boom before region confirmed")
        if set_region and on_region_applied is not None:
            on_region_applied()
        return _fake_result(tmp_path, phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", recording_collect_one)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
    batch = asyncio.run(collector.collect_many(["первый", "второй", "третий"]))

    assert set_region_calls == [True, True, False]
    assert [f.phrase for f in batch.failures] == ["первый"]
    assert [r.run_directory.name for r in batch.results] == ["второй", "третий"]


def test_collect_many_does_not_retry_region_if_it_was_applied_before_a_later_failure(monkeypatch, tmp_path):
    """Codex + /review finding (PR #5, round 2): region_ready must flip to
    True as soon as _set_region itself succeeds, not only after the whole
    phrase (_collect_one) returns successfully. Otherwise a phrase that
    applies the region fine but then fails later — e.g. on its last view,
    the map tab where the region control is absent from the DOM — leaves
    region_ready False, so the next phrase retries _set_region on a page
    still parked on the map and gets InterfaceChangedError, cascading the
    failure through the rest of the batch."""

    _patch_common(monkeypatch)
    set_region_calls = []

    async def collect_one(
        self,
        page,
        session,
        downloads_path,
        phrase,
        region,
        set_region=True,
        on_region_applied=None,
        resume_directory=None,
    ):
        set_region_calls.append(set_region)
        if set_region:
            await self._set_region(page, region)
            if on_region_applied is not None:
                on_region_applied()
        if phrase == "первый":
            # Region was applied successfully above, but the phrase still
            # fails afterwards (e.g. on its last view/report).
            raise PhraseEntryError("boom after region confirmed")
        return _fake_result(tmp_path, phrase)

    monkeypatch.setattr(WordstatCollector, "_collect_one", collect_one)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
    batch = asyncio.run(collector.collect_many(["первый", "второй", "третий"]))

    # Region was actually applied while handling "первый" — the second
    # phrase must not try to re-select it (that would hit the map-tab
    # InterfaceChangedError this test guards against).
    assert set_region_calls == [True, False, False]
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

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
    batch = asyncio.run(collector.collect_many(["чай"]))

    assert [r.run_directory.name for r in batch.results] == ["чай"]


def test_collect_many_rejects_an_empty_phrase_list(tmp_path):
    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    with pytest.raises(InvalidRequestError, match="At least one search phrase"):
        asyncio.run(collector.collect_many([]))


def test_collect_many_rejects_a_blank_phrase_in_the_middle(tmp_path):
    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    with pytest.raises(InvalidRequestError, match="must not be empty"):
        asyncio.run(collector.collect_many(["чай", "   ", "кофе"]))


def test_collect_many_rejects_a_blank_region(tmp_path):
    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    with pytest.raises(InvalidRequestError, match="region must not be empty"):
        asyncio.run(collector.collect_many(["чай"], region="  "))


def test_collect_wraps_collect_many_and_returns_the_single_result(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    _patch_collect_one(monkeypatch, lambda phrase: _async(_fake_result(tmp_path, phrase)))

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
    result = asyncio.run(collector.collect(phrase="чай"))

    assert result.run_directory.name == "чай"


def test_collect_propagates_the_original_exception_type(monkeypatch, tmp_path):
    _patch_common(monkeypatch)

    async def fail(phrase):
        raise PhraseEntryError("boom")

    _patch_collect_one(monkeypatch, fail)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    with pytest.raises(PhraseEntryError, match="boom"):
        asyncio.run(collector.collect(phrase="чай"))


async def _async(value):
    return value


def _write_view_csv(path, phrase):
    path.write_text(f"Запрос;Показов\n{phrase};100\n", encoding="cp1251")


def test_collect_one_preserves_a_table_snapshot_for_the_next_phrase(monkeypatch, tmp_path):
    """The map is last, so the next phrase must not snapshot it as None."""

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()
    waits = []
    snapshots = iter(
        ["popular first", "related first", "dynamics first", "popular second", "related second", "dynamics second"]
    )
    download_count = 0

    async def noop(*args, **kwargs):
        pass

    async def snapshot(self, page):
        return next(snapshots)

    async def wait(self, page, expression, seconds=None, required=True):
        waits.append((expression, seconds, required))

    async def download(self, page, session, directory):
        nonlocal download_count
        download_count += 1
        source = directory / f"export-{download_count}.csv"
        _write_view_csv(source, "тест")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_assert_authenticated", noop)
    monkeypatch.setattr(WordstatCollector, "_set_phrase", noop)
    monkeypatch.setattr(WordstatCollector, "_set_region", noop)
    monkeypatch.setattr(WordstatCollector, "_select_view", noop)
    monkeypatch.setattr(WordstatCollector, "_table_snapshot", snapshot)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
    page = _FakePage()
    session = _FakeSession()
    asyncio.run(collector._collect_one(page, session, downloads_path, "первая", "Россия", set_region=False))
    asyncio.run(collector._collect_one(page, session, downloads_path, "вторая", "Россия", set_region=False))

    assert waits == [
        (
            '() => document.querySelector(".table__wrapper tbody tr")?.textContent !== "dynamics first"',
            3.0,
            False,
        )
    ]
    assert collector._previous_table_snapshot == "dynamics second"


def test_collect_one_rescues_the_csv_into_the_run_directory_on_write_failure(monkeypatch, tmp_path):
    """A4: the CSV rescue must trigger for ANY failure past parsing (e.g. a
    write_dataset/pyarrow error), not just CsvFormatError — and the original
    exception type/message must still propagate unwrapped."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    call_count = {"n": 0}

    async def fake_download(self, page, session, dl_path):
        call_count["n"] += 1
        source = dl_path / f"export-{call_count['n']}.csv"
        _write_view_csv(source, "тест")
        return source, None

    def failing_write_dataset(dataset, run_directory):
        raise RuntimeError("simulated pyarrow.ArrowInvalid")

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)
    monkeypatch.setattr(collector_module, "write_dataset", failing_write_dataset)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

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

    async def fake_select_view(self, page, selector, view):
        pass

    async def fake_download(self, page, session, dl_path):
        source = dl_path / "export.csv"
        _write_view_csv(source, "тест")
        return source, None

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

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run():
        page = _FakePage()
        session = _FakeSession()
        return await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    # Issue #27: the first view already succeeded, so this is now a partial
    # result (not a raised exception) — the rescue must still not raise a
    # FileNotFoundError on top of the second view's original RuntimeError,
    # which is what this test guards; that message now surfaces through
    # view_errors (the failed view) instead of propagating out of
    # _collect_one. The two views after it were never attempted (the loop
    # breaks) and get their own distinct "не пробовался" entries.
    result = asyncio.run(run())
    assert len(result.view_errors) == 3
    assert "second view fails" in result.view_errors[WordstatView.TOP_RELATED]
    for never_attempted in (WordstatView.DYNAMICS, WordstatView.REGIONS):
        assert "не пробовался" in result.view_errors[never_attempted]


# --- issue #27: a single failing view must not fail the whole phrase -----


def test_collect_one_returns_a_partial_result_when_one_view_fails(monkeypatch, tmp_path):
    """3 of 4 views succeed, the 4th (regions) fails with a non-auth error
    (DownloadTimeoutError, mirroring issue #27's live symptom). _collect_one
    must not raise: it returns a CollectionResult whose manifest honestly
    reports the failed view as missing, and whose view_errors carries the
    reason — instead of the caller losing 3 successfully collected views and
    seeing "Собрано 0 из 1" for a phrase that mostly succeeded."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    call_count = {"n": 0}

    async def fake_download(self, page, session, dl_path):
        call_count["n"] += 1
        if call_count["n"] == 4:
            raise DownloadTimeoutError("simulated: Wordstat never produced a CSV for regions")
        source = dl_path / f"export-{call_count['n']}.csv"
        _write_view_csv(source, "тест")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run():
        page = _FakePage()
        session = _FakeSession()
        return await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    result = asyncio.run(run())

    assert result.manifest.missing_views == [WordstatView.REGIONS]
    assert len(result.manifest.exports) == 3
    assert result.manifest.status == "incomplete"
    on_disk = load_manifest(result.manifest_path)
    assert on_disk.missing_views == [WordstatView.REGIONS]
    assert len(on_disk.exports) == 3
    assert WordstatView.REGIONS in result.view_errors
    assert "simulated" in result.view_errors[WordstatView.REGIONS]


def test_collect_one_records_untried_views_after_a_non_final_view_fails(monkeypatch, tmp_path):
    """When the 2nd of 4 views fails (not the last one), the loop breaks
    (see the comment above the view loop) and REGIONS/whichever views come
    after are never attempted at all — that must be recorded distinctly
    from "this view was tried and it failed", or the CLI's fallback message
    for a view with no view_errors entry would misreport an untried view as
    a failure with no known cause."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    call_count = {"n": 0}

    async def fake_download(self, page, session, dl_path):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise DownloadTimeoutError("simulated: top_related never downloaded")
        source = dl_path / f"export-{call_count['n']}.csv"
        _write_view_csv(source, "тест")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run():
        page = _FakePage()
        session = _FakeSession()
        return await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    result = asyncio.run(run())

    assert len(result.manifest.exports) == 1  # only TOP_POPULAR, the first view
    assert result.manifest.missing_views == [
        WordstatView.TOP_RELATED,
        WordstatView.DYNAMICS,
        WordstatView.REGIONS,
    ]
    # The view that actually failed carries the real error...
    assert "simulated: top_related never downloaded" in result.view_errors[WordstatView.TOP_RELATED]
    # ...but DYNAMICS/REGIONS were never even attempted, and must say so
    # distinctly rather than reusing the same failure message or being
    # silently absent from view_errors altogether.
    for never_attempted in (WordstatView.DYNAMICS, WordstatView.REGIONS):
        assert "не пробовался" in result.view_errors[never_attempted]


def test_collect_one_still_propagates_authentication_loss_mid_phrase(monkeypatch, tmp_path):
    """AuthenticationRequiredError must keep propagating out of _collect_one
    (not be swallowed into a partial result) — collect_many relies on it to
    break the whole batch instead of retrying a dead session phrase by
    phrase."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    call_count = {"n": 0}

    async def fake_download(self, page, session, dl_path):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise AuthenticationRequiredError("simulated: session logged out mid-phrase")
        source = dl_path / f"export-{call_count['n']}.csv"
        _write_view_csv(source, "тест")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run():
        page = _FakePage()
        session = _FakeSession()
        with pytest.raises(AuthenticationRequiredError):
            await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    asyncio.run(run())


def test_collect_one_raises_when_every_view_fails(monkeypatch, tmp_path):
    """A phrase where nothing at all was collected must not come back as a
    "partial success" CollectionResult with zero exports — that would let
    the CLI count an entirely failed phrase as collected."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    async def fake_download(self, page, session, dl_path):
        raise DownloadTimeoutError("simulated: nothing ever downloads")

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run():
        page = _FakePage()
        session = _FakeSession()
        with pytest.raises(DownloadTimeoutError):
            await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    asyncio.run(run())


# --- incremental manifest / resume, exercised through the real _collect_one ---


def test_collect_one_writes_the_manifest_after_every_view_not_only_at_the_end(monkeypatch, tmp_path):
    """The manifest.json on disk must reflect progress mid-phrase, not just
    the final result — otherwise a crash between views leaves nothing to
    resume from."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    call_count = {"n": 0}

    async def fake_download(self, page, session, dl_path):
        call_count["n"] += 1
        source = dl_path / f"export-{call_count['n']}.csv"
        _write_view_csv(source, "тест")
        return source, None

    seen_statuses = []
    real_write_manifest = collector_module.write_manifest

    def recording_write_manifest(path, manifest):
        real_write_manifest(path, manifest)
        seen_statuses.append((manifest.status, len(manifest.exports)))

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)
    monkeypatch.setattr(collector_module, "write_manifest", recording_write_manifest)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run():
        page = _FakePage()
        session = _FakeSession()
        return await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    result = asyncio.run(run())

    # One write before the loop (empty, incomplete) + one per view (4).
    assert seen_statuses[0] == ("incomplete", 0)
    assert seen_statuses[-1] == ("complete", 4)
    assert len(seen_statuses) == 5
    assert result.manifest.status == "complete"
    on_disk = load_manifest(result.manifest_path)
    assert on_disk.status == "complete"
    assert len(on_disk.exports) == 4
    # Even the very first write (before any view exists) must set
    # updated_at, not leave it null — null is reserved for manifests written
    # by a version of this tool that predates the field, and a fresh run
    # must never look like one of those.
    assert on_disk.updated_at is not None
    assert on_disk.updated_at >= on_disk.created_at


def test_collect_one_resume_directory_only_collects_missing_views(monkeypatch, tmp_path):
    """Resuming an existing run directory must add only the missing views
    and must not re-download or overwrite views already recorded with their
    file present on disk."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    # First pass: run for real, but make it fail right after the first view
    # so the run directory is left genuinely incomplete.
    async def fake_select_view(self, page, selector, view):
        pass

    call_count = {"n": 0}

    async def failing_after_first_download(self, page, session, dl_path):
        call_count["n"] += 1
        if call_count["n"] > 1:
            raise RuntimeError("simulated interruption")
        source = dl_path / "export-1.csv"
        _write_view_csv(source, "тест")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", failing_after_first_download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run_first():
        page = _FakePage()
        session = _FakeSession()
        return await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    # Issue #27: one view succeeding before another fails is now a partial
    # result, not a raised exception (see _collect_one's view_errors guard —
    # it only re-raises when *no* view was collected at all).
    first_result = asyncio.run(run_first())
    assert WordstatView.TOP_RELATED in first_result.view_errors
    assert "simulated interruption" in first_result.view_errors[WordstatView.TOP_RELATED]

    run_directories = [p for p in tmp_path.glob("runs/*") if p.is_dir()]
    assert len(run_directories) == 1
    run_directory = run_directories[0]
    partial_manifest = load_manifest(run_directory / "manifest.json")
    assert partial_manifest.status == "incomplete"
    first_view_export = next(e for e in partial_manifest.exports if e.view == WordstatView.TOP_POPULAR)
    first_view_mtime = (run_directory / first_view_export.file).stat().st_mtime

    # Second pass: resume, and this time let every remaining view succeed.
    call_count["n"] = 0

    async def succeeding_download(self, page, session, dl_path):
        call_count["n"] += 1
        source = dl_path / f"resume-{call_count['n']}.csv"
        _write_view_csv(source, "тест")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_download_current_view", succeeding_download)

    async def run_resume():
        page = _FakePage()
        session = _FakeSession()
        return await collector._collect_one(
            page, session, downloads_path, "тест", "Россия", resume_directory=run_directory
        )

    result = asyncio.run(run_resume())

    assert result.run_directory == run_directory
    assert call_count["n"] == 3  # only the 3 previously-missing views were downloaded
    final_manifest = load_manifest(run_directory / "manifest.json")
    assert final_manifest.status == "complete"
    assert len(final_manifest.exports) == 4
    # The already-collected view's file was not touched by the resume.
    still_there = next(e for e in final_manifest.exports if e.view == WordstatView.TOP_POPULAR)
    assert still_there == first_view_export
    assert (run_directory / still_there.file).stat().st_mtime == first_view_mtime


def test_collect_one_resume_prunes_stale_export_when_parquet_is_missing_and_retry_fails(monkeypatch, tmp_path):
    """Cycle-review follow-up to issue #27 (round 2): views_to_collect()
    re-attempts a view whose <view>.parquet is missing from disk even though
    manifest.exports still has a stale ExportSummary for it (e.g. the file
    was manually deleted after a prior run). If that re-attempt then fails
    too, the stale export entry must have been pruned from manifest.exports
    up front — otherwise missing_views (computed from exports only) falsely
    reports nothing missing for a view whose data file does not exist, and a
    programmatic manifest consumer gets a false "this view is fine" signal."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)

    async def succeeding_download(self, page, session, dl_path):
        source = dl_path / "export.csv"
        _write_view_csv(source, "тест")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_download_current_view", succeeding_download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run_first():
        page = _FakePage()
        session = _FakeSession()
        return await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    first_result = asyncio.run(run_first())
    assert not first_result.view_errors  # all 4 views collected cleanly
    run_directory = first_result.run_directory
    full_manifest = load_manifest(run_directory / "manifest.json")
    assert full_manifest.status == "complete"

    # Simulate a manually-deleted parquet: the manifest still names it in
    # exports, but the file itself is gone from disk.
    regions_export = next(e for e in full_manifest.exports if e.view == WordstatView.REGIONS)
    (run_directory / regions_export.file).unlink()

    # Resume, and this time make the re-attempted view's download fail.
    async def failing_download(self, page, session, dl_path):
        raise DownloadTimeoutError("simulated: regions re-collection failed on resume")

    monkeypatch.setattr(WordstatCollector, "_download_current_view", failing_download)

    async def run_resume():
        page = _FakePage()
        session = _FakeSession()
        return await collector._collect_one(
            page, session, downloads_path, "тест", "Россия", resume_directory=run_directory
        )

    result = asyncio.run(run_resume())

    # The stale export entry must be gone: missing_views must name REGIONS,
    # not silently omit it because a stale exports entry survived.
    assert WordstatView.REGIONS in result.manifest.missing_views
    assert WordstatView.REGIONS in result.view_errors
    on_disk = load_manifest(run_directory / "manifest.json")
    assert WordstatView.REGIONS in on_disk.missing_views
    assert not any(e.view == WordstatView.REGIONS for e in on_disk.exports)


def test_collect_one_resume_directory_rejects_a_different_phrase(monkeypatch, tmp_path):
    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    async def fake_download(self, page, session, dl_path):
        source = dl_path / "export.csv"
        _write_view_csv(source, "чай")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run():
        page = _FakePage()
        session = _FakeSession()
        return await collector._collect_one(page, session, downloads_path, "чай", "Россия")

    result = asyncio.run(run())

    async def run_resume_with_wrong_phrase():
        page = _FakePage()
        session = _FakeSession()
        with pytest.raises(ResumeMismatchError, match="does not match"):
            await collector._collect_one(
                page,
                session,
                downloads_path,
                "кофе",
                "Россия",
                resume_directory=result.run_directory,
            )

    asyncio.run(run_resume_with_wrong_phrase())


def test_collect_many_rejects_resume_directory_with_more_than_one_phrase(tmp_path):
    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)
    resume_dir = tmp_path / "some-run"
    resume_dir.mkdir()

    with pytest.raises(InvalidRequestError, match="--resume-dir requires exactly one phrase"):
        asyncio.run(collector.collect_many(["чай", "кофе"], resume_directory=resume_dir))


# --- resume updates source_url/updated_at, leaves created_at alone (bugfix #3) ---


def test_collect_one_resume_updates_source_url_and_updated_at_but_not_created_at(monkeypatch, tmp_path):
    """The bug no reviewer caught: resuming used to leave created_at and
    source_url exactly as they were after the first, interrupted pass, even
    though the remaining views are collected later, from a different page
    state. created_at must still describe when the run *started*; source_url
    must reflect the page at the time of this write, not the first one."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    async def failing_download(self, page, session, dl_path):
        raise RuntimeError("simulated interruption before any view finished")

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", failing_download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run_first():
        page = _FakePage(url="https://wordstat.yandex.ru/?words=тест&region=Россия")
        session = _FakeSession()
        with pytest.raises(RuntimeError, match="simulated interruption"):
            await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    asyncio.run(run_first())

    run_directory = next(p for p in tmp_path.glob("runs/*") if p.is_dir())
    first_manifest = load_manifest(run_directory / "manifest.json")
    original_created_at = first_manifest.created_at
    original_source_url = first_manifest.source_url
    original_updated_at = first_manifest.updated_at

    async def succeeding_download(self, page, session, dl_path):
        source = dl_path / "export.csv"
        _write_view_csv(source, "тест")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_download_current_view", succeeding_download)

    resumed_url = "https://wordstat.yandex.ru/?words=тест&region=Россия&tab=table"

    async def run_resume():
        page = _FakePage(url=resumed_url)
        session = _FakeSession()
        return await collector._collect_one(
            page, session, downloads_path, "тест", "Россия", resume_directory=run_directory
        )

    result = asyncio.run(run_resume())

    assert result.manifest.created_at == original_created_at  # start time is untouched
    assert result.manifest.source_url == resumed_url  # reflects this session's page, not the old one
    assert result.manifest.source_url != original_source_url
    # The very first write already sets updated_at (equal to created_at at
    # that point) — see _collect_one — so null is unambiguous elsewhere as
    # "predates this field". A resume must bump it forward, never leave it
    # equal to (let alone before) the interrupted first pass's value.
    assert original_updated_at is not None
    assert result.manifest.updated_at is not None
    assert result.manifest.updated_at > original_updated_at

    on_disk = load_manifest(result.manifest_path)
    assert on_disk.created_at == original_created_at
    assert on_disk.source_url == resumed_url


def test_collect_one_resume_of_an_already_complete_run_does_not_touch_the_manifest(monkeypatch, tmp_path):
    """A resume that finds nothing missing (the early return in
    _collect_one) must not bump updated_at or rewrite source_url — nothing
    was actually collected in that call, so claiming an update would be a
    lie."""

    _patch_common(monkeypatch)

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    async def fake_select_view(self, page, selector, view):
        pass

    async def fake_download(self, page, session, dl_path):
        source = dl_path / "export.csv"
        _write_view_csv(source, "тест")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_select_view", fake_select_view)
    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)

    collector = WordstatCollector("cdp", tmp_path, settling_seconds=0, empty_export_retry_seconds=0)

    async def run_full():
        page = _FakePage(url="https://wordstat.yandex.ru/?words=тест")
        session = _FakeSession()
        return await collector._collect_one(page, session, downloads_path, "тест", "Россия")

    first_result = asyncio.run(run_full())
    manifest_bytes_before = first_result.manifest_path.read_bytes()

    async def run_resume():
        page = _FakePage(url="https://wordstat.yandex.ru/?words=тест&tab=different")
        session = _FakeSession()
        return await collector._collect_one(
            page, session, downloads_path, "тест", "Россия", resume_directory=first_result.run_directory
        )

    resumed_result = asyncio.run(run_resume())

    assert resumed_result.manifest.source_url == first_result.manifest.source_url
    assert resumed_result.manifest.updated_at == first_result.manifest.updated_at
    assert resumed_result.manifest_path.read_bytes() == manifest_bytes_before
