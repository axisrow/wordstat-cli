"""Deterministic interaction with the authenticated Wordstat UI."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from browser_use.browser import BrowserSession

from wordstat.csv_io import parse_wordstat_csv
from wordstat.errors import (
    AuthenticationRequiredError,
    DownloadTimeoutError,
    InterfaceChangedError,
    PhraseEntryError,
)
from wordstat.models import CollectionManifest, CollectionResult, ExportSummary, WordstatView
from wordstat.storage import create_run_directory, preserve_export, write_manifest

WORDSTAT_URL = "https://wordstat.yandex.ru/"
QUERY_SELECTOR = 'input[placeholder="Введите слово или словосочетание"]'
SEARCH_SELECTOR = ".wordstat__search-button"
DOWNLOAD_SELECTOR = "button.save-button"
DOWNLOAD_CSV_MENU_ITEM_SELECTOR = "a[download]:has(button.save-csv-button)"
REGION_BUTTON_SELECTOR = ".settings__selected button"
TABLE_ROW_SELECTOR = ".table__wrapper tbody tr"


class WordstatCollector:
    """Export four Wordstat reports through an already authenticated CDP session."""

    def __init__(self, cdp_url: str, output_root: Path, timeout_seconds: float = 45.0) -> None:
        self.cdp_url = cdp_url
        self.output_root = output_root
        self.timeout_seconds = timeout_seconds

    async def collect(self, phrase: str, region: str = "Россия") -> CollectionResult:
        """Collect popular, related, dynamics and regional CSV reports for one phrase."""

        phrase = phrase.strip()
        region = region.strip()
        if not phrase:
            raise ValueError("The search phrase must not be empty")
        if not region:
            raise ValueError("The region must not be empty")

        run_directory = create_run_directory(self.output_root, phrase)
        session = BrowserSession(
            cdp_url=self.cdp_url,
            is_local=True,
            downloads_path=run_directory,
            allowed_domains=["wordstat.yandex.ru", "passport.yandex.ru"],
            keep_alive=True,
        )
        try:
            await session.start()
            page = await session.new_page()
            await page.goto(WORDSTAT_URL)
            await self._wait_for(page, f"() => Boolean(document.querySelector({json.dumps(QUERY_SELECTOR)}))")
            await self._assert_authenticated(page)
            await self._set_phrase(page, phrase)
            await self._set_region(page, region)

            datasets = []
            original_downloads = []
            for view, selector in (
                (WordstatView.TOP_POPULAR, "label[for='table']"),
                (WordstatView.TOP_RELATED, "label:has(#associations)"),
                (WordstatView.DYNAMICS, "label[for='graph']"),
                (WordstatView.REGIONS, "label[for='map']"),
            ):
                await self._select_view(page, selector)
                source = await self._download_current_view(page, session, run_directory)
                destination = preserve_export(source, run_directory, view)
                original_downloads.append(source)
                datasets.append(parse_wordstat_csv(destination, view))

            manifest = CollectionManifest(
                phrase=phrase,
                region=region,
                created_at=datetime.now(UTC),
                source_url=await page.get_url(),
                exports=[
                    ExportSummary(
                        view=dataset.view,
                        file=dataset.source_file.name,
                        source_file=source.name,
                        row_count=len(dataset.rows),
                        headers=dataset.headers,
                    )
                    for dataset, source in zip(datasets, original_downloads, strict=True)
                ],
            )
            manifest_path = run_directory / "manifest.json"
            write_manifest(manifest_path, manifest)
            return CollectionResult(
                run_directory=run_directory,
                manifest_path=manifest_path,
                manifest=manifest,
                datasets=datasets,
            )
        finally:
            await session.stop()

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

    async def _download_current_view(self, page, session: BrowserSession, run_directory: Path) -> Path:
        # macOS resolves /tmp to /private/tmp; the run directory and
        # session.downloaded_files can report the same physical file under
        # different unresolved paths, which would otherwise look like two
        # distinct downloads. Compare resolved paths, keep the original for
        # return.
        before = self._resolved_file_snapshot(run_directory, session)
        # "Скачать" now opens a format menu (CSV / XLSX) instead of downloading
        # directly; a second click on the CSV entry is required.
        await self._click(page, DOWNLOAD_SELECTOR)
        await self._wait_for(
            page, f"() => Boolean(document.querySelector({json.dumps(DOWNLOAD_CSV_MENU_ITEM_SELECTOR)}))"
        )
        await self._click(page, DOWNLOAD_CSV_MENU_ITEM_SELECTOR)
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            current = self._resolved_file_snapshot(run_directory, session)
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

    async def _wait_for(self, page, expression: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if await page.evaluate(f"() => String(Boolean(({expression})()))") == "true":
                return
            await asyncio.sleep(0.25)
        raise InterfaceChangedError(f"Wordstat did not reach the expected page state before the timeout: {expression}")

    @staticmethod
    def _resolved_file_snapshot(directory: Path, session: BrowserSession) -> dict[Path, Path]:
        """Map each candidate file's resolved path to its original path.

        Keying by the resolved path lets set difference correctly dedupe a
        file that appears under two unresolved forms (e.g. /tmp vs
        /private/tmp on macOS) between run_directory globbing and
        session.downloaded_files.
        """
        paths = {path for path in directory.glob("*") if path.is_file()}
        paths |= {Path(path) for path in session.downloaded_files}
        return {path.resolve(): path for path in paths if path.exists()}
