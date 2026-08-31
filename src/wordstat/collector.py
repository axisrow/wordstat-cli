"""Deterministic interaction with the authenticated Wordstat UI."""

import asyncio
import json
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from browser_use.browser import BrowserSession

from wordstat.csv_io import parse_wordstat_csv
from wordstat.dataset_io import write_dataset
from wordstat.errors import (
    AuthenticationRequiredError,
    DownloadEscapedError,
    DownloadNoNewPathError,
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
TABLE_VIEW_SELECTOR = "label[for='table']"
GRANULARITY_SELECTOR = ".wordstat__content-type_select > button"
DATE_RANGE_SELECTOR = ".range-datepicker__selected-dates > button"
REGION_BUTTON_SELECTOR = ".settings__selected button"
TABLE_ROW_SELECTOR = ".table__wrapper tbody tr"
# Tab selectors live here with the rest of the DOM knowledge; the markup is
# inconsistent enough (id- vs for-based) that it is worth having in one place.
VIEW_SELECTORS = {
    WordstatView.TOP_POPULAR: "label:has(#popular)",
    WordstatView.TOP_RELATED: "label:has(#associations)",
    WordstatView.DYNAMICS: "label[for='graph']",
    WordstatView.REGIONS: "label[for='map']",
}

_QUOTED_PHRASE_MARKERS = (("«", "»"), ('"', '"'), ("“", "”"), ("„", "“"))


def _assert_export_phrase(dataset: CsvDataset, phrase: str, view: WordstatView) -> None:
    """Reject a CSV whose metadata identifies a different search phrase.

    Wordstat's localized metadata wording is not a contract, but the query
    itself is embedded as a quoted value in the report header.  Check that
    stable identity rather than matching the surrounding localized prose.
    Two-column synthetic/legacy exports have no metadata field and remain
    accepted for backwards compatibility; live Wordstat exports have three
    or more columns.
    """
    if len(dataset.headers) < 3:
        return
    if any(
        f"{opening}{phrase}{closing}" in header
        for header in dataset.headers
        for opening, closing in _QUOTED_PHRASE_MARKERS
    ):
        return
    raise InterfaceChangedError(
        f"Wordstat {view.value} CSV metadata does not identify the requested phrase {phrase!r}"
    )
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


def _parse_monthly_dynamics_period(value: str) -> date:
    """Parse Wordstat's nominative Russian month/year period label."""
    month_name, year_text = value.split()
    if not re.fullmatch(r"\d{4}", year_text):
        raise ValueError(value)
    month = RUSSIAN_MONTHS.index(month_name) + 1
    return date(int(year_text), month, 1)


def _is_untrustworthy_empty_export(view: WordstatView, dataset: CsvDataset) -> bool:
    """True if an empty CSV for this view can never be a legitimate export.

    Only DYNAMICS is checked. This used to also cover TOP_POPULAR/
    TOP_RELATED on the theory that _select_view's pre-download hard gate
    (TABLE_ROW_SELECTOR.length > 0, see that method's docstring and
    CLAUDE.md's issue #3/#13 section) makes an empty CSV a structural
    contradiction for every table-based view alike — "the DOM proved
    rows>0 moments earlier, so the export cannot legitimately be empty".

    Issue #22 found that premise false, with a live counterexample: issue
    #11 recorded TOP_POPULAR/TOP_RELATED returning an empty CSV from a
    manual click on the export link, entirely outside this code path — so
    the DOM showing rows before the click does *not* prove the exported
    file cannot be empty. Worse, TOP_POPULAR is the first view in
    VIEW_SELECTORS and is *always* empty on live Wordstat regardless of
    phrase — not a rare race but a permanent property of that report. Once
    this predicate covered it, every full collect() run failed closed on
    the very first view and never reached DYNAMICS/REGIONS at all: the
    fix for issue #11 (never silently swallow an anomalous empty export)
    had turned into "the tool no longer completes a single run".

    So "the DOM proved rows>0 structurally" was never the right criterion.
    The criterion that actually distinguishes these views, per issue #11's
    live measurements and PR #20's, is empirical: does Wordstat reliably
    return a non-empty file for this view at all? For TOP_POPULAR/
    TOP_RELATED, no — never, confirmed live, so an empty export from them
    is the expected, honest case, not an anomaly to fail closed on; the
    caller (._collect_one) still records it as a normal ExportSummary with
    row_count: 0, which is what makes the emptiness visible in
    manifest.json instead of silently absent (issue #16's concern, at
    least for these two views — see CLAUDE.md and this issue for the
    `status`/`missing_views` computed fields that make row_count: 0 alone
    not sufficient for the third, DYNAMICS). For DYNAMICS, yes — issue
    #11 measured 24 rows across three separate live runs, never empty, and
    PR #20 corroborates it; an empty DYNAMICS export is genuinely
    anomalous, so it stays behind the fail-closed gate below (with
    _collect_one's existing empty_export_retry_seconds retry ahead of it).

    REGIONS was never part of this set: it is the one view _select_view's
    hard gate itself exempts (a map has no TABLE_ROW_SELECTOR rows in its
    DOM at all), so there is no structural premise to begin with, and no
    live evidence of an empty regions.parquet either.

    This used to also be conditioned on a second, post-download DOM read
    (`rendered_rows > 0`) — re-querying the DOM after the download's
    unbounded polling window can observe a table that has since emptied
    (rerender, auth transition, page degradation), which let the empty
    export through as an apparently valid `row_count: 0` and silently
    corrupt the manifest into `status: "complete"` — the exact failure
    mode issue #11's fix was meant to close for DYNAMICS. That reasoning
    is unaffected by this change and still applies to DYNAMICS: no second
    opinion from a later DOM read is needed or trustworthy here. See
    tests/test_collector_view.py.
    """
    return view is WordstatView.DYNAMICS and not dataset.rows


def _should_retry_empty_export(view: WordstatView, dataset: CsvDataset) -> bool:
    """Return whether an empty CSV deserves one content-based retry.

    The table-row gate in ``_select_view`` only proves that the page rendered
    rows; it does not prove that the export blob used by the download link has
    been rebuilt.  The parsed CSV is the first signal that describes the
    export itself, so an empty result is the retry trigger rather than another
    DOM read.  Top views may still legitimately remain empty (see
    ``_is_untrustworthy_empty_export``), so retrying and rejecting an export
    are deliberately separate decisions.
    """
    return view in (WordstatView.TOP_POPULAR, WordstatView.TOP_RELATED) and not dataset.rows


def _without_traceback(error: Exception) -> Exception:
    """Drop the traceback before an exception is stashed in PhraseFailure.

    Only the exception's type/message are ever read back out (collect()'s
    re-raise, the CLI's error line); the traceback would otherwise keep every
    frame on the stack alive — page, session, a parsed CsvDataset, ... — for
    as long as the batch's failures list is (until collect_many returns and
    the CLI finishes printing it).
    """
    return error.with_traceback(None)


@dataclass(frozen=True)
class _RetryExportResult:
    """A parsed export whose path and contents are guaranteed to match."""

    source: Path
    dataset: CsvDataset


@dataclass(frozen=True)
class _PreparedRun:
    """The on-disk state and work still required for one collection run."""

    run_directory: Path
    manifest_path: Path
    manifest: CollectionManifest
    pending_views: list[WordstatView]


@dataclass(frozen=True)
class _PhraseAttempt:
    """Outcome of applying the batch's per-phrase error policy."""

    continue_batch: bool
    result: CollectionResult | None = None
    failure: PhraseFailure | None = None


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
        # returns early. Kept as constructor parameters (not hardcoded) so
        # tests against a fake, instantly-responding page can set them to 0
        # instead of actually blocking the test process for real wall-clock
        # time on every view of every phrase (140 tests went from 26.9s to
        # 0.77s once collect_many's own tests did this — see git history).
        #
        # settling_seconds (introduced in efaa9ba, issue #6 phase 2): a
        # preventive pause before downloading each non-map view, guarding
        # against a header-only CSV race first observed for issue #11
        # (Wordstat can return a checked radio + enabled download button
        # before the export blob has actually been rebuilt for the newly
        # selected phrase/view). No one has reproduced *this specific*
        # settling race with reliable repro steps — the 1.0s value was
        # chosen without a live timing measurement, not derived from one.
        # Cost: +1s per non-map view, +3s per phrase (3 of 4 views are
        # non-map), so +50s across a 50-phrase batch. Do not remove without
        # a way to verify nothing regresses against a live run.
        #
        # empty_export_retry_seconds: unlike the above, this one fires only
        # after the parsed CSV itself is empty, not preventively from a DOM
        # observation. It retries the two top views, whose second empty result
        # remains a legitimate ExportSummary; DYNAMICS keeps its existing
        # fail-closed retry path below.
        self.settling_seconds = settling_seconds
        self.empty_export_retry_seconds = empty_export_retry_seconds
        self._previous_table_snapshot: str | None = None

    async def collect(
        self, phrase: str, region: str = "Россия", resume_directory: Path | None = None
    ) -> CollectionResult:
        """Collect popular, related, dynamics and regional reports for one phrase.

        Contract change (issue #27): a phrase that collected at least one
        view but not all four (e.g. regions failed after top_popular/
        top_related/dynamics already succeeded) no longer raises here. Per
        _collect_one, that case now comes back from collect_many as a
        result, not a failure — this method's ``if batch.failures`` branch
        therefore only fires when *zero* views were collected for the
        phrase (see _collect_one's own guard) or the session's
        authentication was lost. A caller that needs to know whether every
        view was collected must inspect the returned result's
        ``manifest.missing_views``/``view_errors``, not rely on this method
        raising for a partial run the way it used to.
        """

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

        # Resource ownership and the phrase loop intentionally live in separate
        # helpers.  In particular, a failure in one phrase must not be able to
        # accidentally widen the lifetime of the shared download directory or
        # browser session.
        return await self._run_batch_session(
            cleaned_phrases,
            region,
            resume_directory,
            granularity,
            date_from,
            date_to,
        )

    async def _run_batch_session(
        self,
        phrases: list[str],
        region: str,
        resume_directory: Path | None,
        granularity: Granularity,
        date_from: date | None,
        date_to: date | None,
    ) -> BatchCollectionResult:
        """Own the batch's temporary downloads directory and browser session."""
        # create_run_directory() used to be the first thing that created
        # output_root (via mkdir(parents=True)). The shared downloads
        # directory below is now created before any run directory, so
        # output_root has to exist first, or a fresh --output-dir fails with
        # a bare FileNotFoundError instead of a WordstatError.
        self.output_root.mkdir(parents=True, exist_ok=True)

        # BrowserSession fixes downloads_path at construction time, so one
        # holding area is shared by all phrases and removed after the batch.
        with tempfile.TemporaryDirectory(dir=self.output_root, prefix=".downloads-") as downloads_dir:
            downloads_path = Path(downloads_dir)
            session = BrowserSession(
                cdp_url=self.cdp_url,
                is_local=True,
                downloads_path=downloads_path,
                allowed_domains=["wordstat.yandex.ru", "passport.yandex.ru"],
                keep_alive=True,
            )
            # Declared before the try so the finally below can check "was a
            # tab actually created" even if session.start() itself raised
            # (in which case new_page() was never reached and there is
            # nothing of ours to close).
            page = None
            try:
                await session.start()
                page = await session.new_page()
                await page.goto(WORDSTAT_URL)
                batch = await self._collect_batch_phrases(
                    page, session, downloads_path, phrases, region, resume_directory,
                    granularity, date_from, date_to,
                )
            finally:
                # Close only the tab this batch created (issue #9): new_page()
                # above opens exactly one tab for the whole batch (not one per
                # phrase), keep_alive=True correctly leaves the user's own
                # Chrome and its other tabs untouched, but that same flag also
                # means session.stop() below never closes *our* tab either —
                # left unclosed, every CLI invocation orphans one more tab.
                # Must run before session.stop(): the CDP handle backing
                # `page` may no longer be usable once the session itself has
                # stopped. Wrapped the same way as session.stop() below —
                # a failed close() (tab already gone, CDP hiccup) must not
                # discard the results/failures already collected.
                if page is not None:
                    try:
                        await session.close_page(page)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    await session.stop()
                except Exception:  # noqa: BLE001
                    # A session.stop() failure (e.g. the CDP connection is
                    # already gone) must not discard the results/failures
                    # already collected below.
                    pass

        return batch

    async def _collect_batch_phrases(
        self, page, session, downloads_path: Path, phrases: list[str], region: str,
        resume_directory: Path | None, granularity: Granularity, date_from: date | None,
        date_to: date | None,
    ) -> BatchCollectionResult:
        """Authenticate once, then traverse phrases while retaining region state."""
        await self._wait_for(page, f"() => Boolean(document.querySelector({json.dumps(QUERY_SELECTOR)}))")
        await self._assert_authenticated(page)
        results: list[CollectionResult] = []
        failures: list[PhraseFailure] = []
        region_ready = False

        def mark_region_ready() -> None:
            nonlocal region_ready
            region_ready = True

        for phrase in phrases:
            collect_kwargs = {
                "set_region": not region_ready,
                "on_region_applied": mark_region_ready,
                "resume_directory": resume_directory,
            }
            if granularity is not Granularity.MONTHLY or date_from is not None:
                collect_kwargs.update(granularity=granularity, date_from=date_from, date_to=date_to)
            attempt = await self._collect_phrase_with_policy(
                phrase,
                lambda: self._collect_one(page, session, downloads_path, phrase, region, **collect_kwargs),
            )
            if attempt.result is not None:
                results.append(attempt.result)
            else:
                assert attempt.failure is not None
                failures.append(attempt.failure)
            if not attempt.continue_batch:
                break
        return BatchCollectionResult(total=len(phrases), results=results, failures=failures)

    async def _collect_phrase_with_policy(
        self, phrase: str, collect: Callable[[], Awaitable[CollectionResult]]
    ) -> _PhraseAttempt:
        """Apply batch error policy; BaseException intentionally is not caught."""
        try:
            return _PhraseAttempt(continue_batch=True, result=await collect(), failure=None)
        except AuthenticationRequiredError as error:
            return _PhraseAttempt(
                continue_batch=False,
                result=None,
                failure=PhraseFailure(phrase=phrase, error=_without_traceback(error)),
            )
        except Exception as error:  # noqa: BLE001 - batch isolation is intentional
            return _PhraseAttempt(
                continue_batch=True,
                result=None,
                failure=PhraseFailure(phrase=phrase, error=_without_traceback(error)),
            )

    async def _prepare_run(
        self,
        page,
        phrase: str,
        region: str,
        *,
        resume_directory: Path | None,
        granularity: Granularity,
        date_from: date | None,
        date_to: date | None,
    ) -> _PreparedRun:
        """Validate or create the run state before interacting with views."""
        if resume_directory is not None:
            # prepare_resume_directory is the hard reject against the main
            # data-corruption risk here: it never reuses a directory whose
            # stored phrase/region don't match this request. The storage
            # helper also owns the manifest/file validation for resume.
            run_directory = resume_directory
            manifest = prepare_resume_directory(run_directory, phrase, region)
            requested_period = self._requested_period(date_from, date_to)
            if manifest.granularity is not granularity or manifest.requested_period != requested_period:
                raise InvalidRequestError(
                    "--resume-dir was created with a different dynamics granularity or requested period"
                )
            manifest_path = run_directory / "manifest.json"
            pending_views = views_to_collect(run_directory, manifest)
            # Do not rewrite a complete run: in particular, its source URL
            # and updated_at describe the last actual collection write.
            if not pending_views:
                return _PreparedRun(run_directory, manifest_path, manifest, pending_views)

            # Keep the manifest honest while a missing parquet is retried.
            # views_to_collect() considers the file as well as the manifest
            # entry, so remove stale entries before any later failure can
            # leave them looking complete.
            stale_pending = {view for view in pending_views if any(e.view == view for e in manifest.exports)}
            if stale_pending:
                manifest = manifest.model_copy(
                    update={
                        "exports": [e for e in manifest.exports if e.view not in stale_pending],
                        "updated_at": datetime.now(UTC),
                    }
                )
                write_manifest(manifest_path, manifest)
            return _PreparedRun(run_directory, manifest_path, manifest, pending_views)

        run_directory = create_run_directory(self.output_root, phrase)
        manifest_path = run_directory / "manifest.json"
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
        return _PreparedRun(run_directory, manifest_path, manifest, list(WordstatView))

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
        prepared = await self._prepare_run(
            page,
            phrase,
            region,
            resume_directory=resume_directory,
            granularity=granularity,
            date_from=date_from,
            date_to=date_to,
        )
        run_directory = prepared.run_directory
        manifest_path = prepared.manifest_path
        manifest = prepared.manifest
        pending_views = prepared.pending_views
        if not pending_views:
            return CollectionResult(
                run_directory=run_directory,
                manifest_path=manifest_path,
                manifest=manifest,
            )
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

        # Capture provenance only after the requested phrase and region are
        # applied. Before that point the URL still belongs to the previous
        # phrase in a multi-phrase batch, or to the previous session when
        # resuming. This is also the first successful write for a fresh run,
        # so updated_at must advance beyond its creation timestamp. For a
        # resume, created_at is deliberately left untouched: it marks when
        # this run started, not when every view was collected, and its views
        # can legitimately span more than one session.
        manifest = manifest.model_copy(
            update={"source_url": await page.get_url(), "updated_at": datetime.now(UTC)}
        )
        write_manifest(manifest_path, manifest)

        # issue #27: a failure on one view (e.g. the live escaped-download
        # race on regions, or any other InterfaceChangedError/parse failure)
        # used to propagate straight out of _collect_one, which collect_many
        # then recorded as a whole-phrase PhraseFailure — discarding the
        # other views this same phrase had already collected and written to
        # disk. view_errors accumulates a message per failed view; the loop
        # then `break`s instead of continuing to the next view, because a
        # view that failed mid-select/download/parse can leave the page in
        # an unpredictable state (stuck popup, half-applied granularity,
        # ...) that later views should not be attempted against blindly.
        # AuthenticationRequiredError is the one exception NOT caught here:
        # it means the whole session is gone, not just this view, and must
        # keep propagating so collect_many's per-phrase try/except still
        # sees it and breaks the batch (see that method's own comment on
        # why it must be checked before the generic Exception branch).
        view_errors: dict[WordstatView, str] = {}
        escaped_download_warnings: list[str] = []
        last_view_error: Exception | None = None
        for view_index, view in enumerate(pending_views):
            try:
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
                source, escape_warning = await self._download_current_view(page, session, downloads_path)
                if escape_warning is not None:
                    escaped_download_warnings.append(f"[{view.value}] {escape_warning}")
                try:
                    # Convert before disposing of the download, so a parse or
                    # write failure leaves the raw CSV on disk to inspect. The
                    # live export blob can lag the table repaint once. The
                    # parsed CSV, not the already-passing DOM row gate, is the
                    # signal used to trigger one retry. Top views can still be
                    # legitimately empty after that retry, so this is kept
                    # separate from the fail-closed predicate below.
                    dataset = parse_wordstat_csv(source, view)
                    _assert_export_phrase(dataset, phrase, view)
                    if _should_retry_empty_export(view, dataset) or _is_untrustworthy_empty_export(view, dataset):
                        retry = await self._retry_empty_export(
                            page,
                            session,
                            downloads_path,
                            source,
                            dataset,
                            view,
                            escaped_download_warnings,
                        )
                        source, dataset = retry.source, retry.dataset
                        _assert_export_phrase(dataset, phrase, view)
                    if _is_untrustworthy_empty_export(view, dataset):
                        raise InterfaceChangedError(
                            f"Wordstat returned an empty {view.value} CSV after a retry, but the page had "
                            "rendered at least one table row before the download was triggered; export is "
                            "not trustworthy"
                        )
                    if view is WordstatView.DYNAMICS:
                        self._assert_contiguous_dynamics_rows(
                            dataset, granularity, date_from=date_from, date_to=date_to
                        )
                    file_name = self._dynamics_file_name(granularity) if view is WordstatView.DYNAMICS else None
                    if file_name is None:
                        data_path, dtypes = write_dataset(dataset, run_directory)
                    else:
                        data_path, dtypes = write_dataset(dataset, run_directory, file_name=file_name)
                    raw_path = finalize_raw(
                        source, run_directory, view, self.keep_raw, output_root=self.output_root
                    )
                except Exception:  # noqa: BLE001
                    # source lives in the batch's shared, temporary downloads
                    # directory; it would otherwise vanish with that directory
                    # once the batch finishes. Rescue it into this phrase's own
                    # run directory for any failure past this point (parsing,
                    # dtype inference, the parquet write itself), not just
                    # CsvFormatError, so "the CSV stays on disk to inspect" holds
                    # regardless of which step failed.
                    # shutil.move (not Path.replace/os.rename) so this survives
                    # source and run_directory sitting on different filesystems —
                    # same reasoning as finalize_raw's own move. source here is
                    # always the file _download_current_view already confirmed
                    # lives inside downloads_path (an escaped path raises
                    # DownloadEscapedError before source is ever bound in this
                    # scope), so no containment check is needed on this path.
                    if source.exists():
                        shutil.move(str(source), str(run_directory / source.name))
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
            except AuthenticationRequiredError:
                raise
            except Exception as error:  # noqa: BLE001 - per-view isolation is intentional, see comment above
                view_errors[view] = f"{type(error).__name__}: {error}"
                last_view_error = error
                # Every view after this one was never attempted at all (the
                # loop breaks, see the comment above this loop) — record
                # that explicitly instead of leaving them silently absent
                # from both exports and view_errors. Without this, the CLI
                # (which falls back to "не собран" for any view in
                # missing_views with no view_errors entry) would print the
                # same message for "we tried and it genuinely failed" and
                # "we never even attempted this view", which reads as a
                # false claim that every view was tried.
                for skipped_view in pending_views[view_index + 1 :]:
                    view_errors[skipped_view] = f"не пробовался: сбой на виде {view.value}"
                break

        if last_view_error is not None and not manifest.exports:
            # Nothing at all was collected for this phrase (the very first
            # attempted view already failed, or a --resume-dir run whose
            # only remaining view just failed too) — this must still surface
            # as a failure, not a "partial success" CollectionResult with
            # zero exports. Without this guard, collect_many would count a
            # completely failed phrase in batch.results, and the CLI would
            # report it as collected. Re-raises the original exception type/
            # message (not a generic wrapper) so collect_many's per-phrase
            # except still records the real cause in PhraseFailure.error.
            raise last_view_error

        return CollectionResult(
            run_directory=run_directory,
            manifest_path=manifest_path,
            manifest=manifest,
            view_errors=view_errors,
            escaped_download_warnings=escaped_download_warnings,
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
    def _assert_contiguous_dynamics_rows(
        dataset: CsvDataset,
        granularity: Granularity,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> None:
        """Run both independent dynamics period checks.

        Keep this private entry point as a compatibility shim for callers and
        tests that predate the split; the checks themselves live in the two
        methods below and are also invoked independently by new code.
        """
        WordstatCollector._assert_dynamics_rows_are_contiguous(dataset, granularity)
        WordstatCollector._assert_dynamics_rows_within_requested_period(
            dataset, granularity, date_from=date_from, date_to=date_to
        )

    @staticmethod
    def _assert_dynamics_rows_are_contiguous(
        dataset: CsvDataset,
        granularity: Granularity,
    ) -> None:
        # This parses dataset.rows[field] — the exported CSV's period column,
        # not the DOM's displayed cell text. Daily and weekly exports contain
        # DD.MM.YYYY; monthly exports contain a nominative Russian month and
        # year such as "август 2024".
        # Live-measured weekly CSV (issue #6 phase 1 document — see
        # `docs/ISSUE6_PHASE1_RESEARCH.md` on the PR #20 branch,
        # `git show 4be8996:docs/ISSUE6_PHASE1_RESEARCH.md`): the exported
        # field is named "Неделя с" and holds only the week's start date
        # as a bare DD.MM.YYYY value ("22.06.2026") — no range, no week
        # number — confirmed byte-for-byte from a real downloaded CSV. The
        # DOM cell (_wait_for_table_granularity's WEEKLY pattern below)
        # shows a genitive-month date *range* instead ("22 июня 2026 – 28
        # июня 2026"); that is a different string in a different place and
        # does not describe the exported column. Do not "fix" this parse
        # to expect a range based on the DOM pattern — that would break a
        # confirmed-working live path (cycle-review round 1's R2 and round
        # 2's RR1 both raised this same claim from reading the code
        # without this doc open; both were false positives).
        if not dataset.rows:
            return
        dates = WordstatCollector._dynamics_dates(dataset, granularity)
        if len(dates) >= 2:
            for previous, current in zip(dates, dates[1:]):
                if granularity is Granularity.MONTHLY:
                    expected = date(
                        previous.year + (previous.month == 12),
                        previous.month % 12 + 1,
                        1,
                    )
                    contiguous = current == expected
                else:
                    step = timedelta(days=1 if granularity is Granularity.DAILY else 7)
                    contiguous = current - previous == step
                if not contiguous:
                    raise InterfaceChangedError(
                        f"Wordstat returned a gap in the {granularity.value} dynamics series "
                        f"between {previous:%d.%m.%Y} and {current:%d.%m.%Y}"
                    )

    @staticmethod
    def _assert_dynamics_rows_within_requested_period(
        dataset: CsvDataset,
        granularity: Granularity,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> None:
        """Reject dynamics rows outside an explicitly requested period."""
        if not dataset.rows or date_from is None or date_to is None:
            return
        dates = WordstatCollector._dynamics_dates(dataset, granularity)
        # Containment, not equality: a shorter export fully inside the
        # requested window is legitimate (the daily series' variable
        # trailing tail, confirmed live — issue #6 phase 1 document), so
        # this never compares dates[0]/dates[-1] against date_from/date_to
        # for exact equality. What it does reject is a row *outside* the
        # requested window in either direction — the live race this guards
        # against (collector.py:603, _wait_for_period_applied) confirms
        # only the first cell's format+value match date_from before
        # downloading; it has no equivalent check for date_to, so a table
        # that has not yet repainted past a previous, stale window can
        # download and be recorded as complete with data that never
        # entered the requested window at all. Deliberately runs even for
        # a single-row export (needs only dates[0]/dates[-1], which are
        # the same row — unlike the pairwise contiguity loop above, which
        # needs at least two rows): a single stale/truncated row is not
        # exempt just because it is too short to check contiguity
        # (cycle-review round 2, C2 triage — the original version gated
        # this behind the same `len(rows) < 2` early return as contiguity
        # and let a 1-row export bypass it entirely). Only applies when an
        # explicit period was requested (date_from/date_to are None for
        # the UI's default window, which this function cannot validate
        # against any specific boundary).
        # Weekly rows are keyed by the Monday of date_from's week, not
        # date_from itself, so the lower bound must be that aligned Monday.
        if granularity is Granularity.MONTHLY:
            lower_bound = date(date_from.year, date_from.month, 1)
            upper_bound = date(date_to.year, date_to.month, 1)
        else:
            lower_bound = (
                date_from - timedelta(days=date_from.weekday())
                if granularity is Granularity.WEEKLY
                else date_from
            )
            upper_bound = date_to
        if dates[0] < lower_bound:
            raise InterfaceChangedError(
                f"Wordstat {granularity.value} dynamics export is outside the requested window: "
                f"starts at {dates[0]:%d.%m.%Y}, before {lower_bound:%d.%m.%Y}"
            )
        if dates[-1] > upper_bound:
            raise InterfaceChangedError(
                f"Wordstat {granularity.value} dynamics export is outside the requested window: "
                f"ends at {dates[-1]:%d.%m.%Y}, after {upper_bound:%d.%m.%Y}"
            )

    @staticmethod
    def _dynamics_dates(dataset: CsvDataset, granularity: Granularity) -> list[date]:
        field = dataset.headers[0]
        try:
            if granularity is Granularity.MONTHLY:
                return [_parse_monthly_dynamics_period(row[field]) for row in dataset.rows]
            return [datetime.strptime(row[field], "%d.%m.%Y").date() for row in dataset.rows]
        except ValueError as error:
            raise InterfaceChangedError(
                f"Wordstat returned an unexpected {granularity.value} dynamics date format"
            ) from error


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
        if popup_type != "month":
            # day/week: independent popups per click, live-confirmed by
            # 255bca7's daily/weekly runs going through this exact
            # two-click path successfully.
            await self._click(page, DATE_RANGE_SELECTOR)
        else:
            # month: a single shared range-picker, not two independent
            # popups (live CDP check, issue #6 phase 2, cycle-review
            # round 2). Right after date_from is picked, every month
            # element already carries the "in-selecting-range" class —
            # the picker is already in range-selection mode — and the
            # date-range button's text does not update to reflect
            # date_from yet; it only updates once date_to is picked in
            # the SAME still-open popup. A second DATE_RANGE_SELECTOR
            # click here closes this popup instead of reopening it
            # (confirmed live: visibility flips to 'hidden'), so the
            # follow-up _select_calendar_date for date_to would time out
            # waiting for a popup that never reappears. Confirmed live
            # that skipping the intermediate click and picking date_to
            # directly in the still-open popup produces the correct
            # button text "Январь 2024 — Июнь 2024".
            pass
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
        #
        # The table can repaint its first row before its last row. Waiting
        # only for date_from therefore lets a stale end through when the
        # requested start happens to equal the old window's start. The
        # post-download containment check remains authoritative, but wait for
        # the visible last row to enter the requested window first as well.
        await self._wait_for_period_applied(page, granularity, date_from, date_to)

    async def _wait_for_period_applied(
        self, page, granularity: Granularity, date_from: date, date_to: date | None = None
    ) -> None:
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
        expression = (
            f"() => new RegExp({json.dumps(pattern)}).test("
            f"document.querySelector({json.dumps(TABLE_ROW_SELECTOR)})?.querySelector('td')"
            "?.textContent?.trim() ?? '')"
        )
        if date_to is not None and granularity is not Granularity.MONTHLY:
            # Daily exports may legitimately end before date_to because
            # Wordstat's trailing data tail is variable. Accept every
            # possible date in the requested range instead of waiting for an
            # exact end-date match. Weekly rows are keyed by their Monday,
            # so use the same aligned lower bound as the CSV containment
            # check below.
            lower_bound = (
                date_from - timedelta(days=date_from.weekday())
                if granularity is Granularity.WEEKLY
                else date_from
            )
            step = timedelta(days=7 if granularity is Granularity.WEEKLY else 1)
            last_row_patterns = []
            candidate = lower_bound
            while candidate <= date_to:
                month = RUSSIAN_MONTHS_GENITIVE[candidate.month - 1]
                year = f" {candidate.year}" if granularity is Granularity.WEEKLY else ""
                last_row_patterns.append(re.escape(f"{candidate.day} {month}{year}"))
                candidate += step
            last_row_pattern = "^(?:" + "|".join(last_row_patterns) + ")"
            expression += (
                f" && new RegExp({json.dumps(last_row_pattern)}).test("
                f"[...document.querySelectorAll({json.dumps(TABLE_ROW_SELECTOR)})].at(-1)"
                "?.querySelector('td')?.textContent?.trim() ?? '')"
            )
        await self._wait_for(
            page,
            expression,
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
            # Live CDP check (issue #6 phase 2, cycle-review round 2): the
            # month-text popup renders lowercase nominative month names
            # ("январь"), matching RUSSIAN_MONTHS verbatim.
            # month_label.capitalize() ("Январь") never matches any of
            # them — every explicit monthly request with
            # --date-from/--date-to failed here with InterfaceChangedError
            # ("Wordstat option 'Январь' was not uniquely found") despite
            # validate_period accepting the request and the browser
            # already being driven. Do not re-add .capitalize(): it was
            # never confirmed live and is contradicted by the confirmed
            # live DOM text.
            await self._click_visible_text(page, month_selector, month_label)
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
        # A phrase switch preserves the previously selected top-level view.
        # Return to the table view before selecting a top-level subview; on
        # the map, the popular/related radio controls are not in the DOM.
        await self._click(page, TABLE_VIEW_SELECTOR)
        await self._wait_for(
            page,
            f"() => document.querySelectorAll({json.dumps(VIEW_SELECTORS[WordstatView.TOP_POPULAR])}).length === 1",
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

    async def _retry_empty_export(
        self,
        page,
        session: BrowserSession,
        downloads_path: Path,
        source: Path,
        dataset: CsvDataset,
        view: WordstatView,
        escaped_download_warnings: list[str],
    ) -> _RetryExportResult:
        """Retry an empty export and return a matching path/dataset pair.

        A no-new-path timeout can mean Chrome reused and overwrote ``source``.
        The original is backed up before the retry; on that specific signal,
        restore it to the original path before returning. All other failures
        propagate unchanged. The single outer ``finally`` owns the temporary
        file through creation, copy, retry, restore, and cleanup.
        """
        if self.empty_export_retry_seconds > 0:
            await asyncio.sleep(self.empty_export_retry_seconds)
        retry_backup: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{view.value}-retry-",
                suffix=".csv",
                dir=self.output_root,
                delete=False,
            ) as backup_file:
                retry_backup = Path(backup_file.name)
            shutil.copy2(source, retry_backup)
            try:
                retry_source, retry_escape_warning = await self._download_current_view(
                    page, session, downloads_path
                )
            except DownloadNoNewPathError:
                if not _should_retry_empty_export(view, dataset):
                    raise
                shutil.copy2(retry_backup, source)
                return _RetryExportResult(source=source, dataset=dataset)
            if retry_escape_warning is not None:
                escaped_download_warnings.append(f"[{view.value}] {retry_escape_warning}")
            return _RetryExportResult(
                source=retry_source,
                dataset=parse_wordstat_csv(retry_source, view),
            )
        finally:
            if retry_backup is not None:
                retry_backup.unlink(missing_ok=True)

    async def _download_current_view(
        self, page, session: BrowserSession, downloads_path: Path
    ) -> tuple[Path, str | None]:
        """Download the CSV for the currently selected view.

        Returns ``(path, escape_warning)``. ``escape_warning`` is ``None`` on
        the common path; it carries a message when a stray escaped download
        (see ``DownloadEscapedError`` below) was observed in the very same
        poll tick as the legitimate CSV (issue #27 follow-up). That case
        must not fail the view — the view's own CSV is fine and already on
        disk — but the operator still needs to know Chrome dropped a file
        outside ``downloads_path`` somewhere. Checking ``new_escaped`` only
        applies when no legitimate CSV was found in this tick would silently
        lose that signal forever: ``session.downloaded_files`` is
        session-lifetime and append-only, so the same path is already inside
        next call's ``before_escaped`` baseline and would never show up in a
        future ``new_escaped`` diff either — this is not a "check it next
        time" gap, the escape is gone from view for good once masked by a
        same-tick success.
        """
        # macOS resolves /tmp to /private/tmp; the downloads directory and
        # session.downloaded_files can report the same physical file under
        # different unresolved paths, which would otherwise look like two
        # distinct downloads. Compare resolved paths, keep the original for
        # return.
        before = self._resolved_file_snapshot(downloads_path, session)
        before_escaped = self._escaped_download_paths(downloads_path, session)
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
            # Evaluated every tick, including the one that finds the
            # legitimate CSV: a stray escaped path can land in the exact
            # same tick as a good download, and (per the docstring above)
            # that escape would never be detected on any later call either
            # once masked here — this is the only tick in which it is ever
            # observable at all.
            new_escaped = self._escaped_download_paths(downloads_path, session) - before_escaped
            escape_warning = (
                "Chrome reported a download outside the run's downloads directory "
                f"({downloads_path}): {sorted(str(path) for path in new_escaped)}. "
                "The file was left untouched; it is not safe to move or delete "
                "automatically. Move it manually if it belongs to this run."
                if new_escaped
                else None
            )
            if len(csv_files) == 1 and csv_files[0].stat().st_size > 0:
                # The view's own download succeeded — a stray escape seen in
                # this same tick is reported as a warning, not a failure: the
                # data this view needed is safely on disk, and raising here
                # would discard it for no reason (see the docstring above for
                # why this is the only chance to report the escape at all).
                return csv_files[0], escape_warning
            if len(csv_files) > 1:
                raise DownloadTimeoutError("Wordstat produced more than one new CSV for a single export")
            # Chrome can report a download at a path outside downloads_path
            # despite Browser.setDownloadBehavior having been configured for
            # this session (issue #27 — observed live on the fourth view of a
            # phrase, landing under the real ~/Downloads). Root-cause
            # investigated live (CDP :9223, issue #27 fix): every table
            # view's export link is `a[download]` with an `href="blob:..."`
            # and `target="_self"` — DYNAMICS and REGIONS are structurally
            # identical on this point (dumped both live), so a per-target
            # blob/`_self` explanation was ruled out; browser-use's own
            # Browser.setDownloadBehavior call (downloads_watchdog.py) is
            # also browser-level, not per-target, so it should not degrade
            # between views either. Several live full 4-view runs (single
            # phrase and a 2-phrase batch, both with --keep-raw, one against
            # --output-dir inside the repo and one outside it) all completed
            # cleanly with every file landing inside downloads_path — the
            # escape did not reproduce on demand, meaning it's an
            # intermittent Chrome-side race (not deterministically tied to
            # "the fourth view" or any specific view), not a bug in how this
            # collector configures downloads_path. So this containment check
            # can't be "fixed away" upstream; treating the intermittent
            # escape as an honest, loud failure instead of a silent
            # mistargeted move/delete is the correct and sufficient fix. This
            # is never treated as "the" download for this view — that file
            # is not ours to move or delete (it may not even be from this
            # run) — but it must fail loudly and specifically instead of a
            # generic DownloadTimeoutError that leaves the operator guessing
            # whether Wordstat ever produced anything at all.
            if new_escaped:
                raise DownloadEscapedError(
                    "Chrome reported a download outside the run's downloads directory "
                    f"({downloads_path}): {sorted(str(path) for path in new_escaped)}. "
                    "The file was left untouched; it is not safe to move or delete "
                    "automatically. Move it manually if it belongs to this run."
                )
            await asyncio.sleep(0.25)
        raise DownloadNoNewPathError("Wordstat did not produce a new CSV before the download timeout")

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

        session.downloaded_files (issue #27) is browser-use's own
        session-lifetime log of every CDP downloadWillBegin/downloadProgress
        event, not a list scoped to this collector's downloads_path — it can
        legitimately (or by a Chrome quirk not yet root-caused, see
        _download_current_view's docstring) name a path outside directory
        entirely, or a path inside a downloads directory from an earlier,
        already-cleaned-up phrase in the same batch. Filtering it down to
        paths resolving under directory keeps the escaped-path case fully
        out of this snapshot instead of letting set difference "detect" it
        as a legitimate new download — the escape is instead surfaced
        explicitly by _download_current_view below.
        """
        resolved_directory = directory.resolve()
        paths = {path for path in directory.glob("*") if path.is_file()}
        for reported in session.downloaded_files:
            candidate = Path(reported)
            if candidate.exists() and candidate.resolve().is_relative_to(resolved_directory):
                paths.add(candidate)
        return {path.resolve(): path for path in paths if path.exists()}

    @staticmethod
    def _escaped_download_paths(directory: Path, session: BrowserSession) -> set[Path]:
        """Paths session.downloaded_files reports that fall outside directory.

        A companion to _resolved_file_snapshot: that method silently drops
        these paths from its result so they are never mistaken for our own
        download, but _download_current_view still needs to know they exist
        in order to fail loudly (DownloadEscapedError) instead of just timing
        out with a confusing "no CSV appeared" when Chrome in fact downloaded
        something, just not where it was told to.
        """
        resolved_directory = directory.resolve()
        escaped = set()
        for reported in session.downloaded_files:
            candidate = Path(reported)
            if candidate.exists() and not candidate.resolve().is_relative_to(resolved_directory):
                escaped.add(candidate)
        return escaped
