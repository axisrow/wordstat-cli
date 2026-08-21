"""Deterministic interaction with the authenticated Wordstat UI."""

import asyncio
import json
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from browser_use.browser import BrowserSession

from wordstat.csv_io import parse_wordstat_csv
from wordstat.dataset_io import write_dataset
from wordstat.errors import (
    AuthenticationRequiredError,
    DownloadTimeoutError,
    InterfaceChangedError,
    InvalidRequestError,
    PhraseEntryError,
)
from wordstat.models import (
    BatchCollectionResult,
    CollectionManifest,
    CollectionResult,
    CsvDataset,
    ExportSummary,
    PhraseFailure,
    WordstatView,
)
from wordstat.periods import Granularity, validate_period
from wordstat.storage import (
    create_run_directory,
    finalize_raw,
    merge_export,
    prepare_resume_directory,
    views_to_collect,
    write_manifest,
)

WORDSTAT_URL = "https://wordstat.yandex.ru/"
QUERY_SELECTOR = 'input[placeholder="Введите слово или словосочетание"]'
SEARCH_SELECTOR = ".wordstat__search-button"
DOWNLOAD_SELECTOR = "button.save-button"
DOWNLOAD_CSV_MENU_ITEM_SELECTOR = "a[download]:has(button.save-csv-button)"
GRANULARITY_SELECTOR = ".wordstat__content-type_select > button"
DATE_RANGE_SELECTOR = ".range-datepicker__selected-dates > button"
REGION_BUTTON_SELECTOR = ".settings__selected button"
TABLE_ROW_SELECTOR = ".table__wrapper tbody tr"
# Tab selectors live here with the rest of the DOM knowledge; the markup is
# inconsistent enough (id- vs for-based) that it is worth having in one place.
VIEW_SELECTORS = {
    WordstatView.TOP_POPULAR: "label[for='table']",
    WordstatView.TOP_RELATED: "label:has(#associations)",
    WordstatView.DYNAMICS: "label[for='graph']",
    WordstatView.REGIONS: "label[for='map']",
}
# The live interface is Russian; map explicitly rather than depending on the
# host locale. Shared by every place that needs a Russian month name (the
# calendar popups and the applied-period wait below), so there is exactly
# one list to keep in sync with the live UI's wording. Nominative form —
# what the calendar popups' option labels use ("январь", "декабрь").
RUSSIAN_MONTHS = "январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь".split()
# Genitive form — what the dynamics table's first cell uses for daily/weekly
# rows ("22 июня", "24 декабря 2018": "of June", "of December"). Confirmed
# live (issue #6 phase 1/2 documents) that this differs from RUSSIAN_MONTHS;
# a fixed nominative-month match against this genitive text never matches.
RUSSIAN_MONTHS_GENITIVE = (
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
)


def _is_untrustworthy_empty_export(view: WordstatView, dataset: CsvDataset) -> bool:
    """True if an empty CSV for this view can never be a legitimate export.

    TOP_POPULAR/TOP_RELATED/DYNAMICS are checked — every view for which
    _select_view hard-gates TABLE_ROW_SELECTOR.length > 0 on the DOM before
    the "Скачать" click is ever issued (see the docstring on _select_view
    and CLAUDE.md's issue #3/#13 section). REGIONS is the only view exempt
    from that gate (it is a map with no table rows in its DOM), so it is the
    only view exempt here too — this set must stay in lockstep with
    _select_view's `if view != WordstatView.REGIONS:` condition, not be
    picked per-view by hand.

    A phrase that clears the pre-download gate has already had the code
    itself prove TABLE_ROW_SELECTOR.length > 0 moments earlier; genuinely
    zero rows never reaches the download step at all — it dies inside
    _select_view's retry loop with InterfaceChangedError instead. So by the
    time a dataset for one of these views is parsed here, an empty
    dataset.rows already contradicts a state the code itself proved moments
    earlier; there is no code path left where that emptiness is legitimate,
    for any of the three table-based views alike.

    This used to be conditioned on a second, post-download DOM read
    (`rendered_rows > 0`) — but re-querying the DOM after the download's
    unbounded polling window can observe a table that has since emptied
    (rerender, auth transition, page degradation), which let the empty
    export through as an apparently valid `row_count: 0` and silently
    corrupt the manifest into `status: "complete"` — the exact failure mode
    issue #11's fix was meant to close. The pre-download gate is already
    proof enough; no second opinion from a later DOM read is needed or
    trustworthy. See tests/test_collector_view.py.

    DYNAMICS was previously excluded on the strength of issue #11's live-CDP
    data (24 rows across three runs, never empty) — but that is evidence the
    gate rarely fires for DYNAMICS, not evidence that omitting it is safe.
    The gate's premise is structural (the DOM proved rows>0 immediately
    before the click), and that premise holds for DYNAMICS exactly as it
    does for TOP_POPULAR/TOP_RELATED; selecting views by observed frequency
    of emptiness rather than by the structural gate they share was the bug.
    """
    return (
        view in (WordstatView.TOP_POPULAR, WordstatView.TOP_RELATED, WordstatView.DYNAMICS)
        and not dataset.rows
    )


def _without_traceback(error: Exception) -> Exception:
    """Drop the traceback before an exception is stashed in PhraseFailure.

    Only the exception's type/message are ever read back out (collect()'s
    re-raise, the CLI's error line); the traceback would otherwise keep every
    frame on the stack alive — page, session, a parsed CsvDataset, ... — for
    as long as the batch's failures list is (until collect_many returns and
    the CLI finishes printing it).
    """
    return error.with_traceback(None)


class WordstatCollector:
    """Export four Wordstat reports through an already authenticated CDP session."""

    def __init__(
        self,
        cdp_url: str,
        output_root: Path,
        timeout_seconds: float = 45.0,
        keep_raw: bool = False,
        settling_seconds: float = 1.0,
        empty_export_retry_seconds: float = 2.0,
    ) -> None:
        self.cdp_url = cdp_url
        self.output_root = output_root
        self.timeout_seconds = timeout_seconds
        self.keep_raw = keep_raw
        # Both are real, unconditional pauses against the live UI (see their
        # call sites in _collect_one) — not upper-bound timeouts like
        # timeout_seconds, which _wait_for polls against a condition and
        # returns from early. Nothing in the live DOM signals "the export
        # blob has been rebuilt" or "the empty table has repainted", so
        # there is no condition for _wait_for to poll here; a fixed sleep is
        # the only available fix. Kept as constructor parameters (not
        # hardcoded) so tests against a fake, instantly-responding page can
        # set them to 0 instead of actually blocking the test process for
        # real wall-clock time on every view of every phrase.
        self.settling_seconds = settling_seconds
        self.empty_export_retry_seconds = empty_export_retry_seconds
        self._previous_table_snapshot: str | None = None

    async def collect(
        self, phrase: str, region: str = "Россия", resume_directory: Path | None = None
    ) -> CollectionResult:
        """Collect popular, related, dynamics and regional reports for one phrase."""

        batch = await self.collect_many([phrase], region=region, resume_directory=resume_directory)
        if batch.failures:
            raise batch.failures[0].error
        if not batch.results:
            # Unreachable given a single input phrase (collect_many always
            # returns exactly one result or one failure for it), but this
            # keeps the contract from degrading into a bare IndexError if
            # that ever changes.
            raise InvalidRequestError("Collecting the phrase produced neither a result nor a failure")
        return batch.results[0]

    async def collect_many(
        self,
        phrases: list[str],
        region: str = "Россия",
        resume_directory: Path | None = None,
        granularity: Granularity = Granularity.MONTHLY,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> BatchCollectionResult:
        """Collect reports for several phrases inside a single browser session.

        A failure on one phrase is recorded and does not stop the remaining
        phrases, so a large batch does not throw away everything it already
        collected because of one bad phrase.  A lost authentication, however,
        means the session itself is no longer usable, so it aborts the batch
        instead of repeating the same failure for every remaining phrase.

        ``resume_directory`` requests appending missing views to an existing
        run directory instead of creating a new one, and is only valid for a
        single phrase: one run directory holds one phrase's manifest, so
        resuming while collecting several phrases could not be applied to
        all of them without splicing unrelated exports into one manifest.
        """

        region = region.strip()
        validate_period(granularity, date_from, date_to)
        if not phrases:
            raise InvalidRequestError("At least one search phrase is required")
        if not region:
            raise InvalidRequestError("The region must not be empty")

        cleaned_phrases = [phrase.strip() for phrase in phrases]
        if not all(cleaned_phrases):
            raise InvalidRequestError("The search phrase must not be empty")
        if resume_directory is not None and len(cleaned_phrases) != 1:
            raise InvalidRequestError("--resume-dir requires exactly one phrase")

        results: list[CollectionResult] = []
        failures: list[PhraseFailure] = []

        # create_run_directory() used to be the first thing that created
        # output_root (via mkdir(parents=True)). The shared downloads
        # directory below is now created before any run directory, so
        # output_root has to exist first, or a fresh --output-dir fails with
        # a bare FileNotFoundError instead of a WordstatError.
        self.output_root.mkdir(parents=True, exist_ok=True)

        # One downloads directory for the whole batch: BrowserSession fixes
        # downloads_path at construction time, but each phrase needs its own
        # run directory (built from its own slug). Downloads land in this
        # shared holding area and are moved into the per-phrase run directory
        # as they are converted; nothing is left behind in it once this `with`
        # exits, because finalize_raw/parquet writing always relocates or
        # deletes the file — and the failure path in _collect_one rescues it
        # into the run directory first for any exception, not just success.
        with tempfile.TemporaryDirectory(dir=self.output_root, prefix=".downloads-") as downloads_dir:
            downloads_path = Path(downloads_dir)
            session = BrowserSession(
                cdp_url=self.cdp_url,
                is_local=True,
                downloads_path=downloads_path,
                allowed_domains=["wordstat.yandex.ru", "passport.yandex.ru"],
                keep_alive=True,
            )
            try:
                await session.start()
                page = await session.new_page()
                await page.goto(WORDSTAT_URL)
                await self._wait_for(page, f"() => Boolean(document.querySelector({json.dumps(QUERY_SELECTOR)}))")
                await self._assert_authenticated(page)

                # Tracks whether the region has actually been applied in the
                # browser — set by _collect_one's on_region_applied callback
                # right after _set_region succeeds, not after the whole
                # phrase (_collect_one) returns. A phrase can apply the
                # region fine and then still fail later (e.g. on its last
                # view, the map tab) — if region_ready were only set on a
                # clean return, the next phrase would retry _set_region on a
                # page already parked on the map, where the region control is
                # absent from the DOM, and cascade InterfaceChangedError
                # through the rest of the batch (Codex + /review finding,
                # PR #5 round 2).
                region_ready = False

                def _mark_region_ready() -> None:
                    nonlocal region_ready
                    region_ready = True

                for phrase in cleaned_phrases:
                    try:
                        # set_region: only until region_ready flips — see
                        # _collect_one for why the region control can't be
                        # re-selected once a phrase's view loop has run (and
                        # doesn't need to be after that).
                        collect_kwargs = {
                            "set_region": not region_ready,
                            "on_region_applied": _mark_region_ready,
                            "resume_directory": resume_directory,
                        }
                        # Keep the historical call shape for the default
                        # monthly path; several integrations monkeypatch this
                        # seam and default behavior must remain unchanged.
                        if granularity is not Granularity.MONTHLY or date_from is not None:
                            collect_kwargs.update(
                                granularity=granularity,
                                date_from=date_from,
                                date_to=date_to,
                            )
                        result = await self._collect_one(
                            page, session, downloads_path, phrase, region, **collect_kwargs
                        )
                        results.append(result)
                    except AuthenticationRequiredError as error:
                        # The session itself is gone: every remaining phrase
                        # would fail the same way, so stop instead of piling
                        # up identical failures. Must be caught before the
                        # generic Exception branch below.
                        failures.append(PhraseFailure(phrase=phrase, error=_without_traceback(error)))
                        break
                    except Exception as error:  # noqa: BLE001 - batch isolation is intentional
                        # Anything else (InterfaceChangedError, a pyarrow
                        # error from write_dataset, a dropped CDP connection
                        # for this one phrase, ...) must not take down the
                        # rest of the batch. BaseException (KeyboardInterrupt,
                        # SystemExit) deliberately still propagates.
                        failures.append(PhraseFailure(phrase=phrase, error=_without_traceback(error)))
            finally:
                try:
                    await session.stop()
                except Exception:  # noqa: BLE001
                    # A session.stop() failure (e.g. the CDP connection is
                    # already gone) must not discard the results/failures
                    # already collected below.
                    pass

        return BatchCollectionResult(total=len(cleaned_phrases), results=results, failures=failures)

    async def _collect_one(
        self,
        page,
        session: BrowserSession,
        downloads_path: Path,
        phrase: str,
        region: str,
        set_region: bool = True,
        on_region_applied: Callable[[], None] | None = None,
        resume_directory: Path | None = None,
        granularity: Granularity = Granularity.MONTHLY,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> CollectionResult:
        # Checked once before the batch starts (collect_many), but a session
        # can lose authentication mid-batch (e.g. Yandex logs it out); check
        # again per phrase so that loss surfaces as AuthenticationRequiredError
        # instead of a confusing InterfaceChangedError from a control that
        # silently stopped working.
        await self._assert_authenticated(page)

        if resume_directory is not None:
            # prepare_resume_directory is the hard reject against the main
            # data-corruption risk here: it never reuses a directory whose
            # stored phrase/region don't match this request. create_run_directory's
            # own never-overwrite guarantee is untouched — resuming is this
            # separate, explicit path, not a fallback baked into it.
            run_directory = resume_directory
            manifest = prepare_resume_directory(run_directory, phrase, region)
            requested_period = self._requested_period(date_from, date_to)
            if manifest.granularity is not granularity or manifest.requested_period != requested_period:
                raise InvalidRequestError(
                    "--resume-dir was created with a different dynamics granularity or requested period"
                )
            manifest_path = run_directory / "manifest.json"
            pending_views = views_to_collect(run_directory, manifest)
            if not pending_views:
                return CollectionResult(
                    run_directory=run_directory,
                    manifest_path=manifest_path,
                    manifest=manifest,
                )
        else:
            run_directory = create_run_directory(self.output_root, phrase)
            manifest = None
            manifest_path = run_directory / "manifest.json"
            pending_views = list(WordstatView)
        # The preceding complete view loop ends on REGIONS, whose map has no
        # table. Keep the last table snapshot while a table view is visible
        # so the next phrase gets the best-effort re-render nudge. Content
        # equality is only evidence, never a gate: related phrases can
        # legally have the same first row (or no row).
        previous_table = self._previous_table_snapshot
        self._previous_table_snapshot = None
        await self._set_phrase(page, phrase)
        if set_region:
            # Only until region is actually applied — collect_many tracks
            # that via on_region_applied, called right here, not by whether
            # this whole method later returns successfully: a failure later
            # in this same phrase (e.g. on its last view) must not make
            # collect_many think the region still needs setting. The region
            # control lives on the table/graph/associations tabs but not on
            # the map tab (WordstatView.REGIONS) — and every phrase's view
            # loop ends on the map, so calling this again for a later phrase
            # that already has it applied would fail with
            # InterfaceChangedError (confirmed live). It also isn't needed
            # again once applied: Wordstat keeps `region=` in the URL across
            # a phrase switch without re-selecting it (confirmed live too).
            await self._set_region(page, region)
            if on_region_applied is not None:
                on_region_applied()
        if previous_table is not None:
            await self._wait_for(
                page,
                f"() => document.querySelector({json.dumps(TABLE_ROW_SELECTOR)})?.textContent"
                f" !== {json.dumps(previous_table)}",
                seconds=3.0,
                required=False,
            )

        if manifest is None:
            # Written once up front (exports=[], so missing_views/status are
            # derived as every view missing / "incomplete") so a crash before
            # the first view even finishes still leaves a manifest.json on
            # disk describing an empty-but-started run, instead of nothing.
            # updated_at is set here too (equal to created_at for this first
            # write): leaving it null would make null ambiguous between "a
            # successful write already happened" and "this manifest predates
            # the updated_at field" — see CollectionManifest's docstring,
            # which promises null means only the latter.
            start = datetime.now(UTC)
            manifest = CollectionManifest(
                phrase=phrase,
                region=region,
                created_at=start,
                updated_at=start,
                source_url=await page.get_url(),
                exports=[],
                granularity=granularity,
                requested_period=self._requested_period(date_from, date_to),
            )
            write_manifest(manifest_path, manifest)
        else:
            # Resuming: source_url must not silently keep pointing at
            # whatever tab/phrase the *previous* session ended on — it is
            # only accurate as of the last successful write (see
            # CollectionManifest's docstring), and this write is that.
            # created_at is deliberately left untouched: it marks when this
            # run started, not when every view was collected, and a resumed
            # run's views can legitimately span more than one session.
            # Doing this after _set_phrase (not before) is required: the
            # page is still on the previous phrase/tab until _set_phrase
            # returns.
            manifest = manifest.model_copy(update={"source_url": await page.get_url(), "updated_at": datetime.now(UTC)})
            write_manifest(manifest_path, manifest)

        for view in pending_views:
            selector = VIEW_SELECTORS[view]
            await self._select_view(page, selector, view)
            # The live UI can expose the first table row before the export
            # blob has been rebuilt for the selected phrase/view. A short
            # settling interval prevents a header-only CSV from racing the
            # table repaint; the structural row gate above remains required.
            if view is not WordstatView.REGIONS and self.settling_seconds > 0:
                await asyncio.sleep(self.settling_seconds)
            if view is WordstatView.DYNAMICS and (
                granularity is not Granularity.MONTHLY or date_from is not None
            ):
                await self._set_granularity(page, granularity)
                if date_from is not None:
                    await self._set_period(page, granularity, date_from, date_to)
            source = await self._download_current_view(page, session, downloads_path)
            try:
                # Convert before disposing of the download, so a parse or
                # write failure leaves the raw CSV on disk to inspect. The
                # live export blob can lag the table repaint once; retry an
                # empty table export once before failing closed.
                dataset = parse_wordstat_csv(source, view)
                if _is_untrustworthy_empty_export(view, dataset):
                    if self.empty_export_retry_seconds > 0:
                        await asyncio.sleep(self.empty_export_retry_seconds)
                    source = await self._download_current_view(page, session, downloads_path)
                    dataset = parse_wordstat_csv(source, view)
                if _is_untrustworthy_empty_export(view, dataset):
                    raise InterfaceChangedError(
                        f"Wordstat returned an empty {view.value} CSV after a retry, but the page had rendered "
                        "at least one table row before the download was triggered; export is not trustworthy"
                    )
                if view is WordstatView.DYNAMICS:
                    self._assert_contiguous_dynamics_rows(dataset, granularity)
                file_name = self._dynamics_file_name(granularity) if view is WordstatView.DYNAMICS else None
                if file_name is None:
                    data_path, dtypes = write_dataset(dataset, run_directory)
                else:
                    data_path, dtypes = write_dataset(dataset, run_directory, file_name=file_name)
                raw_path = finalize_raw(source, run_directory, view, self.keep_raw)
            except Exception:  # noqa: BLE001
                # source lives in the batch's shared, temporary downloads
                # directory; it would otherwise vanish with that directory
                # once the batch finishes. Rescue it into this phrase's own
                # run directory for any failure past this point (parsing,
                # dtype inference, the parquet write itself), not just
                # CsvFormatError, so "the CSV stays on disk to inspect" holds
                # regardless of which step failed.
                if source.exists():
                    source.replace(run_directory / source.name)
                raise
            if view != WordstatView.REGIONS:
                self._previous_table_snapshot = await self._table_snapshot(page)
            export = ExportSummary(
                view=view,
                file=data_path.name,
                raw_file=raw_path.name if raw_path else None,
                row_count=len(dataset.rows),
                dtypes=dtypes,
            )
            # Rewritten after every view, not just once at the end: an
            # interruption between views must leave a manifest that honestly
            # reflects the views collected so far (see write_manifest and
            # CollectionManifest.status), not a stale one still claiming
            # every view is missing.
            manifest = merge_export(manifest, export)
            if view is WordstatView.DYNAMICS:
                manifest = manifest.model_copy(
                    update={"actual_period": self._actual_period(dataset)}
                )
            write_manifest(manifest_path, manifest)

        return CollectionResult(
            run_directory=run_directory,
            manifest_path=manifest_path,
            manifest=manifest,
        )

    @staticmethod
    def _requested_period(date_from: date | None, date_to: date | None) -> dict[str, str] | None:
        if date_from is None or date_to is None:
            return None
        return {"from": date_from.isoformat(), "to": date_to.isoformat()}

    @staticmethod
    def _actual_period(dataset: CsvDataset) -> dict[str, str] | None:
        if not dataset.rows or not dataset.headers:
            return None
        field = dataset.headers[0]
        return {"field": field, "from": dataset.rows[0][field], "to": dataset.rows[-1][field]}

    @staticmethod
    def _assert_contiguous_dynamics_rows(dataset: CsvDataset, granularity: Granularity) -> None:
        if granularity is Granularity.MONTHLY or len(dataset.rows) < 2:
            return
        field = dataset.headers[0]
        try:
            dates = [datetime.strptime(row[field], "%d.%m.%Y").date() for row in dataset.rows]
        except ValueError as error:
            raise InterfaceChangedError(
                f"Wordstat returned an unexpected {granularity.value} dynamics date format"
            ) from error
        step = timedelta(days=1 if granularity is Granularity.DAILY else 7)
        for previous, current in zip(dates, dates[1:]):
            if current - previous != step:
                raise InterfaceChangedError(
                    f"Wordstat returned a gap in the {granularity.value} dynamics series "
                    f"between {previous:%d.%m.%Y} and {current:%d.%m.%Y}"
                )


    @staticmethod
    def _dynamics_file_name(granularity: Granularity) -> str:
        # Preserve the old monthly filename for existing consumers; non-monthly
        # exports must carry their granularity in the filename.
        return "dynamics.parquet" if granularity is Granularity.MONTHLY else f"dynamics_{granularity.value}.parquet"

    async def _set_granularity(self, page, granularity: Granularity) -> None:
        labels = {
            Granularity.DAILY: "По дням",
            Granularity.WEEKLY: "По неделям",
            Granularity.MONTHLY: "По месяцам",
        }
        await self._click(page, GRANULARITY_SELECTOR)
        await self._click_visible_text(page, ".Popup2_visible [role='option']", labels[granularity])
        await self._wait_for(
            page,
            f"() => document.querySelector({json.dumps(GRANULARITY_SELECTOR)})?.textContent?.trim() === "
            f"{json.dumps(labels[granularity])}",
        )
        await self._wait_for_table_granularity(page, granularity)

    async def _set_period(self, page, granularity: Granularity, date_from: date, date_to: date | None) -> None:
        if date_to is None:
            raise InvalidRequestError("A period end date is required when a start date is provided")
        popup_type = "month" if granularity is Granularity.MONTHLY else (
            "week" if granularity is Granularity.WEEKLY else "day"
        )
        await self._click(page, DATE_RANGE_SELECTOR)
        await self._select_calendar_date(page, popup_type, date_from)
        await self._click(page, DATE_RANGE_SELECTOR)
        await self._select_calendar_date(page, popup_type, date_to)
        # _wait_for_table_granularity alone is not enough here: it only
        # checks that the first cell's *format* matches the granularity
        # (e.g. "looks like a week range"), which is already true of the
        # table's pre-existing content before the period change has been
        # applied. Live CDP checks (issue #6 phase 2) caught this exact
        # race: the date-range button already showed the newly picked
        # dates, _wait_for_table_granularity's format check passed
        # immediately, and the exported CSV still carried Wordstat's
        # default window — the table's *values* had not caught up yet.
        # Waiting for the first cell to actually start at the expected
        # date closes that gap, and turns a silent wrong-period export
        # into a loud InterfaceChangedError if the picker never catches up.
        await self._wait_for_period_applied(page, granularity, date_from)

    async def _wait_for_period_applied(self, page, granularity: Granularity, date_from: date) -> None:
        # The month name in the first cell is genitive ("22 июня", "24
        # декабря" — day-of/week-of a date) for daily/weekly, but
        # nominative ("август 2024") for monthly (confirmed live, issue #6
        # phase 1/2 documents) — hence two separate month lists rather than
        # one. Matching the exact expected month (not just "any month word")
        # matters: without it, "24 <any month> 2018" would silently accept
        # a wrong month picked by a stuck calendar, defeating the point of
        # this wait (confirmed as a real gap during review, not just theory).
        if granularity is Granularity.MONTHLY:
            expected = f"{RUSSIAN_MONTHS[date_from.month - 1]} {date_from.year}"
        elif granularity is Granularity.DAILY:
            expected = f"{date_from.day} {RUSSIAN_MONTHS_GENITIVE[date_from.month - 1]}"
        else:
            # Weekly's first cell is the Monday of date_from's week, not
            # date_from itself (confirmed live: 2018-12-26, a Wednesday,
            # produced a first cell starting "24 декабря" — the preceding
            # Monday). Wordstat does this alignment itself; not a bug.
            week_start = date_from - timedelta(days=date_from.weekday())
            expected = f"{week_start.day} {RUSSIAN_MONTHS_GENITIVE[week_start.month - 1]} {week_start.year}"
        pattern = "^" + re.escape(expected)
        await self._wait_for(
            page,
            f"() => new RegExp({json.dumps(pattern)}).test("
            f"document.querySelector({json.dumps(TABLE_ROW_SELECTOR)})?.querySelector('td')"
            "?.textContent?.trim() ?? '')",
        )

    async def _wait_for_table_granularity(self, page, granularity: Granularity) -> None:
        patterns = {
            Granularity.DAILY: r"^\d{1,2}\s+[А-Яа-яЁё]+$",
            Granularity.WEEKLY: r"^\d{1,2}\s+[А-Яа-яЁё]+\s+\d{4}\s+–\s+\d{1,2}\s+[А-Яа-яЁё]+\s+\d{4}$",
            Granularity.MONTHLY: r"^[А-Яа-яЁё]+\s+\d{4}$",
        }
        expression = (
            "() => new RegExp(" + json.dumps(patterns[granularity]) + ").test(" 
            f"document.querySelector({json.dumps(TABLE_ROW_SELECTOR)})?.querySelector('td')?.textContent?.trim() ?? '')"
        )
        await self._wait_for(page, expression)

    async def _select_calendar_date(self, page, popup_type: str, target: date) -> None:
        root = f".range-datepicker_type_{popup_type}"
        await self._wait_for(
            page,
            f"() => [...document.querySelectorAll({json.dumps(root)})].some((x) => "
            "x.offsetParent !== null && getComputedStyle(x).visibility !== 'hidden')",
        )
        month_label = RUSSIAN_MONTHS[target.month - 1]
        if popup_type == "month":
            year_selector = f"{root} .datepicker-month__years_select > button"
            month_selector = f"{root} .react-datepicker__month-text"
        else:
            year_selector = f"{root} .range-datepicker__years_select > button"
            month_selector = f"{root} .range-datepicker__months_select > button"
        await self._click(page, year_selector)
        await self._click_visible_text(page, ".Popup2_visible [role='option']", str(target.year))
        if popup_type in {"day", "week"}:
            await self._click(page, month_selector)
            await self._click_visible_text(page, ".Popup2_visible [role='option']", month_label)
        day_selector = f"{root} button[name='day']"
        if popup_type == "month":
            await self._click_visible_text(page, month_selector, month_label.capitalize())
        else:
            await self._click_visible_date_button(page, day_selector, str(target.day))

    async def _click_visible_date_button(self, page, selector: str, text: str) -> None:
        result = await page.evaluate(
            """(...args) => {
                const [selector, text] = args;
                const matches = [...document.querySelectorAll(selector)].filter((element) => {
                    const style = getComputedStyle(element);
                    return element.textContent?.trim() === text
                        && !element.className.toString().includes('outside')
                        && !element.className.toString().includes('disabled')
                        && element.offsetParent !== null
                        && style.visibility !== 'hidden';
                });
                if (matches.length !== 1) return JSON.stringify({count: matches.length});
                matches[0].click();
                return JSON.stringify({count: 1});
            }""",
            selector,
            text,
        )
        if json.loads(result) != {"count": 1}:
            raise InterfaceChangedError(f"Wordstat date control {selector!r} was not uniquely found")

    async def _click_visible_text(self, page, selector: str, text: str) -> None:
        result = await page.evaluate(
            """(...args) => {
                const [selector, text] = args;
                const matches = [...document.querySelectorAll(selector)].filter((element) => {
                    const style = getComputedStyle(element);
                    return element.textContent?.trim() === text
                        && element.offsetParent !== null
                        && style.visibility !== 'hidden';
                });
                if (matches.length !== 1) return JSON.stringify({count: matches.length});
                matches[0].click();
                return JSON.stringify({count: 1});
            }""",
            selector,
            text,
        )
        if json.loads(result) != {"count": 1}:
            raise InterfaceChangedError(f"Wordstat option {text!r} was not uniquely found")

    async def _assert_authenticated(self, page) -> None:
        state = await page.evaluate(
            """() => JSON.stringify({
                url: location.href,
                hasLogout: [...document.querySelectorAll('a')].some((link) => link.textContent?.trim() === 'Выйти'),
            })"""
        )
        parsed = json.loads(state)
        if "passport.yandex.ru" in parsed["url"] or not parsed["hasLogout"]:
            raise AuthenticationRequiredError(
                "Wordstat requires an authenticated Yandex session in the attached Chrome"
            )

    async def _set_phrase(self, page, phrase: str) -> None:
        # browser-use types character-by-character over CDP; a keystroke can
        # race the input's focus/handler setup and get dropped or fail to
        # reach React's controlled-input state (observed: "тест" -> "ст" or
        # "ест"; or the DOM value updates but React's own state doesn't, so
        # the next re-render snaps the field back to empty). Checking
        # input.value alone is not enough: the search button's `disabled`
        # state is derived from React's state, so it is the only reliable
        # signal that React actually saw the phrase. Re-fill and re-check
        # both, re-querying the input each time since a stale element handle
        # from before a re-render can turn later attempts into silent no-ops.
        query_selector = json.dumps(QUERY_SELECTOR)
        search_selector = json.dumps(SEARCH_SELECTOR)
        max_attempts = 3
        state = None
        matched = False
        for _ in range(max_attempts):
            elements = await page.get_elements_by_css_selector(QUERY_SELECTOR)
            if len(elements) != 1:
                raise InterfaceChangedError("Wordstat search field was not uniquely found")
            await elements[0].fill(phrase)
            state = json.loads(
                await page.evaluate(
                    f"""() => JSON.stringify({{
                        value: document.querySelector({query_selector})?.value ?? null,
                        searchDisabled: document.querySelector({search_selector})?.disabled ?? null,
                    }})"""
                )
            )
            if state["value"] == phrase and state["searchDisabled"] is False:
                matched = True
                break
        if not matched:
            raise PhraseEntryError(
                f"Wordstat search field state {state!r} after {max_attempts} attempts, expected {phrase!r}"
            )

        await self._click(page, SEARCH_SELECTOR)
        expected_phrase = json.dumps(phrase)
        download_selector = json.dumps(DOWNLOAD_SELECTOR)
        await self._wait_for(
            page,
            f"""() => new URL(location.href).searchParams.get('words') === {expected_phrase}
                && Boolean(document.querySelector({download_selector}))""",
        )

    async def _set_region(self, page, region: str) -> None:
        await self._click(page, REGION_BUTTON_SELECTOR)
        await self._wait_for(
            page,
            "() => [...document.querySelectorAll('button')]"
            ".some((button) => button.textContent?.trim() === 'Подтвердить')",
        )
        result = await page.evaluate(
            """(...args) => {
                const [targetName] = args;
                const matches = (name) => [...document.querySelectorAll('label')].filter((label) =>
                    label.textContent?.trim() === name && label.querySelector('input[type="checkbox"]')
                );
                const all = matches('Все регионы');
                const target = matches(targetName);
                if (all.length !== 1 || target.length !== 1) {
                    return JSON.stringify({all: all.length, target: target.length});
                }
                const allInput = all[0].querySelector('input');
                if (targetName !== 'Все регионы' && allInput.checked) all[0].click();
                const targetInput = target[0].querySelector('input');
                if (!targetInput.checked) target[0].click();
                return JSON.stringify({all: 1, target: 1});
            }""",
            region,
        )
        counts = json.loads(result)
        if counts != {"all": 1, "target": 1}:
            raise InterfaceChangedError(f"Wordstat region {region!r} was not uniquely found")
        await self._click_by_text(page, "button", "Подтвердить")
        # The dialog is not removed from the DOM on close: Wordstat hides it
        # via `visibility: hidden` and moves it off-screen. A plain "is the
        # button still in the DOM" check never becomes true; check visibility
        # instead of presence.
        await self._wait_for(
            page,
            "() => ![...document.querySelectorAll('button')]"
            ".some((button) => button.textContent?.trim() === 'Подтвердить'"
            " && button.offsetParent !== null && getComputedStyle(button).visibility !== 'hidden')",
        )

    async def _select_view(self, page, selector: str, view: WordstatView) -> None:
        # The download button can remain in the DOM while Wordstat is
        # switching views. The radio input is the actual active view marker
        # (checked on the live page). Retry the click once if the marker
        # does not become active before the normal wait timeout.
        #
        # For table-based views, the radio and download button can become
        # ready before the table itself renders. Row presence therefore stays
        # in the hard gate: without it Wordstat can download a header-only
        # CSV. REGIONS is a map with no table rows, so it is exempt.
        #
        # A change in table text is useful corroborating evidence, but cannot
        # be a hard gate: different views can legitimately have the same
        # first row. Check it separately and best-effort after the hard gate.
        previous_table = await self._table_snapshot(page) if view != WordstatView.REGIONS else None
        target_selector = json.dumps(selector)
        ready_expression = f"""() => {{
            const label = document.querySelector({target_selector});
            const input = label?.querySelector('input')
                ?? (label?.htmlFor ? document.getElementById(label.htmlFor) : null);
            return Boolean(input?.checked)
                && Boolean(document.querySelector({json.dumps(DOWNLOAD_SELECTOR)}))"""
        if view != WordstatView.REGIONS:
            ready_expression += (
                f"\n                && document.querySelectorAll({json.dumps(TABLE_ROW_SELECTOR)}).length > 0"
            )
        ready_expression += ";\n        }"
        # Two attempts must not cost double the configured timeout budget
        # (issue found in review): split it so the worst case across both
        # attempts still matches self.timeout_seconds, not 2x it.
        attempt_seconds = self.timeout_seconds / 2
        for attempt in range(2):
            await self._click(page, selector)
            try:
                await self._wait_for(page, ready_expression, seconds=attempt_seconds)
                break
            except InterfaceChangedError:
                if attempt == 1:
                    raise
        if previous_table is not None:
            await self._wait_for(
                page,
                f"() => document.querySelector({json.dumps(TABLE_ROW_SELECTOR)})?.textContent"
                f" !== {json.dumps(previous_table)}",
                seconds=3.0,
                required=False,
            )

    async def _table_snapshot(self, page) -> str | None:
        return await page.evaluate(
            f"() => document.querySelector({json.dumps(TABLE_ROW_SELECTOR)})?.textContent ?? null"
        )

    async def _download_current_view(self, page, session: BrowserSession, downloads_path: Path) -> Path:
        # macOS resolves /tmp to /private/tmp; the downloads directory and
        # session.downloaded_files can report the same physical file under
        # different unresolved paths, which would otherwise look like two
        # distinct downloads. Compare resolved paths, keep the original for
        # return.
        before = self._resolved_file_snapshot(downloads_path, session)
        # "Скачать" now opens a format menu (CSV / XLSX) instead of downloading
        # directly; a second click on the CSV entry is required.
        await self._click(page, DOWNLOAD_SELECTOR)
        await self._wait_for(
            page, f"() => Boolean(document.querySelector({json.dumps(DOWNLOAD_CSV_MENU_ITEM_SELECTOR)}))"
        )
        await self._click(page, DOWNLOAD_CSV_MENU_ITEM_SELECTOR)
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            current = self._resolved_file_snapshot(downloads_path, session)
            new_resolved = set(current) - set(before)
            csv_files = [current[resolved] for resolved in new_resolved if resolved.suffix.lower() == ".csv"]
            if len(csv_files) == 1 and csv_files[0].stat().st_size > 0:
                return csv_files[0]
            if len(csv_files) > 1:
                raise DownloadTimeoutError("Wordstat produced more than one new CSV for a single export")
            await asyncio.sleep(0.25)
        raise DownloadTimeoutError("Wordstat did not produce a CSV before the download timeout")

    async def _click(self, page, selector: str) -> None:
        result = await page.evaluate(
            """(...args) => {
                const selector = args[0];
                const elements = [...document.querySelectorAll(selector)];
                if (elements.length !== 1) return JSON.stringify({count: elements.length});
                if (elements[0].disabled) return JSON.stringify({count: 1, disabled: true});
                elements[0].click();
                return JSON.stringify({count: 1});
            }""",
            selector,
        )
        parsed = json.loads(result)
        if parsed.get("disabled"):
            raise InterfaceChangedError(f"Wordstat control {selector!r} is disabled, click would be a no-op")
        if parsed != {"count": 1}:
            raise InterfaceChangedError(f"Wordstat control {selector!r} was not uniquely found")

    async def _click_by_text(self, page, selector: str, text: str) -> None:
        result = await page.evaluate(
            """(...args) => {
                const [selector, text] = args;
                const elements = [...document.querySelectorAll(selector)].filter(
                    (element) => element.textContent?.trim() === text
                );
                if (elements.length !== 1) return JSON.stringify({count: elements.length});
                elements[0].click();
                return JSON.stringify({count: 1});
            }""",
            selector,
            text,
        )
        if json.loads(result) != {"count": 1}:
            raise InterfaceChangedError(f"Wordstat control {text!r} was not uniquely found")

    async def _wait_for(self, page, expression: str, seconds: float | None = None, required: bool = True) -> None:
        """Poll `expression` until it's true, or give up after the deadline.

        `required=False` (used for table-content corroboration in
        _collect_one and _select_view) makes the timeout a no-op instead of a
        failure: different phrases or views can legally produce the same
        table content, so that signal is a useful hint but not a reliable
        gate — a timeout just means it could not confirm anything either way.
        """
        deadline = time.monotonic() + (seconds if seconds is not None else self.timeout_seconds)
        while time.monotonic() < deadline:
            if await page.evaluate(f"() => String(Boolean(({expression})()))") == "true":
                return
            await asyncio.sleep(0.25)
        if required:
            raise InterfaceChangedError(
                f"Wordstat did not reach the expected page state before the timeout: {expression}"
            )

    @staticmethod
    def _resolved_file_snapshot(directory: Path, session: BrowserSession) -> dict[Path, Path]:
        """Map each candidate file's resolved path to its original path.

        Keying by the resolved path lets set difference correctly dedupe a
        file that appears under two unresolved forms (e.g. /tmp vs
        /private/tmp on macOS) between downloads-directory globbing and
        session.downloaded_files.
        """
        paths = {path for path in directory.glob("*") if path.is_file()}
        paths |= {Path(path) for path in session.downloaded_files}
        return {path.resolve(): path for path in paths if path.exists()}
