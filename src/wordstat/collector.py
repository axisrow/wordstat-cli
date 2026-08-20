"""Deterministic interaction with the authenticated Wordstat UI."""

import asyncio
import json
import tempfile
import time
from datetime import UTC, datetime
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
    ExportSummary,
    PhraseFailure,
    WordstatView,
)
from wordstat.storage import create_run_directory, finalize_raw, write_manifest

WORDSTAT_URL = "https://wordstat.yandex.ru/"
QUERY_SELECTOR = 'input[placeholder="Введите слово или словосочетание"]'
SEARCH_SELECTOR = ".wordstat__search-button"
DOWNLOAD_SELECTOR = "button.save-button"
DOWNLOAD_CSV_MENU_ITEM_SELECTOR = "a[download]:has(button.save-csv-button)"
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
    ) -> None:
        self.cdp_url = cdp_url
        self.output_root = output_root
        self.timeout_seconds = timeout_seconds
        self.keep_raw = keep_raw

    async def collect(self, phrase: str, region: str = "Россия") -> CollectionResult:
        """Collect popular, related, dynamics and regional reports for one phrase."""

        batch = await self.collect_many([phrase], region=region)
        if batch.failures:
            raise batch.failures[0].error
        if not batch.results:
            # Unreachable given a single input phrase (collect_many always
            # returns exactly one result or one failure for it), but this
            # keeps the contract from degrading into a bare IndexError if
            # that ever changes.
            raise InvalidRequestError("Collecting the phrase produced neither a result nor a failure")
        return batch.results[0]

    async def collect_many(self, phrases: list[str], region: str = "Россия") -> BatchCollectionResult:
        """Collect reports for several phrases inside a single browser session.

        A failure on one phrase is recorded and does not stop the remaining
        phrases, so a large batch does not throw away everything it already
        collected because of one bad phrase.  A lost authentication, however,
        means the session itself is no longer usable, so it aborts the batch
        instead of repeating the same failure for every remaining phrase.
        """

        region = region.strip()
        if not phrases:
            raise InvalidRequestError("At least one search phrase is required")
        if not region:
            raise InvalidRequestError("The region must not be empty")

        cleaned_phrases = [phrase.strip() for phrase in phrases]
        if not all(cleaned_phrases):
            raise InvalidRequestError("The search phrase must not be empty")

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

                # Tracks whether some phrase has actually completed with the
                # region applied — not just "was this the first phrase" by
                # index. If phrase #1 fails before _set_region succeeds
                # (_collect_one raises anywhere between entering the loop and
                # the _set_region call), region_ready stays False and the
                # next phrase retries it instead of the batch silently
                # collecting the rest under whatever region the browser
                # already had (Codex review finding, PR #5).
                region_ready = False
                for phrase in cleaned_phrases:
                    try:
                        # set_region: only until some phrase confirms it —
                        # see _collect_one for why the region control can't
                        # be re-selected once a phrase's view loop has run
                        # (and doesn't need to be after that).
                        result = await self._collect_one(
                            page, session, downloads_path, phrase, region, set_region=not region_ready
                        )
                        region_ready = True
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
    ) -> CollectionResult:
        # Checked once before the batch starts (collect_many), but a session
        # can lose authentication mid-batch (e.g. Yandex logs it out); check
        # again per phrase so that loss surfaces as AuthenticationRequiredError
        # instead of a confusing InterfaceChangedError from a control that
        # silently stopped working.
        await self._assert_authenticated(page)

        run_directory = create_run_directory(self.output_root, phrase)
        # In a batch, the previous phrase's table can still be sitting in the
        # DOM when _set_phrase's own waits (new `words=` in the URL, download
        # button present) are satisfied — those don't check the table itself.
        # Snapshot it before switching phrases so we can give Wordstat a
        # short extra moment to re-render for the new phrase. This is a best
        # effort nudge, not a hard gate: two different (e.g. related) phrases
        # can legally produce the same first row, or an empty table, so a
        # timeout here must never fail the phrase — see _wait_for(required=False).
        previous_table = await self._table_snapshot(page)
        await self._set_phrase(page, phrase)
        if set_region:
            # Only until some phrase has completed with the region applied
            # (collect_many tracks this via region_ready, not a plain "first
            # phrase" index — a failure before this call succeeds must not
            # permanently give up on setting the region for the rest of the
            # batch). The region control lives on the table/graph/
            # associations tabs but not on the map tab (WordstatView.REGIONS)
            # — and every phrase's view loop ends on the map, so calling this
            # again for a later phrase that already has it applied would fail
            # with InterfaceChangedError (confirmed live). It also isn't
            # needed again once applied: Wordstat keeps `region=` in the URL
            # across a phrase switch without re-selecting it (confirmed live too).
            await self._set_region(page, region)
        if previous_table is not None:
            await self._wait_for(
                page,
                f"() => document.querySelector({json.dumps(TABLE_ROW_SELECTOR)})?.textContent"
                f" !== {json.dumps(previous_table)}",
                seconds=3.0,
                required=False,
            )

        exports = []
        for view, selector in VIEW_SELECTORS.items():
            await self._select_view(page, selector)
            source = await self._download_current_view(page, session, downloads_path)
            try:
                # Convert before disposing of the download, so a parse or
                # write failure leaves the raw CSV on disk to inspect.
                dataset = parse_wordstat_csv(source, view)
                data_path, dtypes = write_dataset(dataset, run_directory)
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
            exports.append(
                ExportSummary(
                    view=view,
                    file=data_path.name,
                    raw_file=raw_path.name if raw_path else None,
                    row_count=len(dataset.rows),
                    dtypes=dtypes,
                )
            )

        manifest = CollectionManifest(
            phrase=phrase,
            region=region,
            created_at=datetime.now(UTC),
            source_url=await page.get_url(),
            exports=exports,
        )
        manifest_path = run_directory / "manifest.json"
        write_manifest(manifest_path, manifest)
        return CollectionResult(
            run_directory=run_directory,
            manifest_path=manifest_path,
            manifest=manifest,
        )

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

    async def _select_view(self, page, selector: str) -> None:
        await self._click(page, selector)
        # The download button appears as soon as the tab switches, before the
        # table has actually loaded its rows; downloading at that point
        # produces a header-only CSV. Wait for at least one data row too.
        await self._wait_for(
            page,
            f"() => Boolean(document.querySelector({json.dumps(DOWNLOAD_SELECTOR)}))"
            f" && document.querySelectorAll({json.dumps(TABLE_ROW_SELECTOR)}).length > 0",
        )
        await asyncio.sleep(0.5)

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

        `required=False` (used for the staleness nudge in _collect_one) makes
        the timeout a no-op instead of a failure: two different phrases can
        legally produce the same table content, so that signal is a useful
        hint but not a reliable gate — a timeout there just means the nudge
        could not confirm anything either way, and the caller proceeds
        regardless.
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
