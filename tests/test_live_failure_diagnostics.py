"""Error-message diagnostics for live Wordstat failures, without a browser.

Live incident (2026-08-31, main @ 5d24e51): two runs died in ``_set_phrase``
with a bare ``{'value': ..., 'searchDisabled': True}`` dump, and two more died
in ``_select_view`` waiting on the dynamics tab — which the raw predicate text
made look like a markup change, while the page actually showed Wordstat's
"Нет подходящих запросов" banner (a Yandex-side data outage that recovered by
the next morning). Both error types must carry a page-state snapshot so the
next incident is readable from the CLI output alone.
"""

import asyncio
import json

import pytest

from wordstat.collector import WordstatCollector
from wordstat.errors import InterfaceChangedError, PhraseEntryError

SNAPSHOT_MARK = "readyState"  # only the snapshot expression reads this

BANNER_SNAPSHOT = {
    "url": "https://wordstat.yandex.ru/?region=213&view=graph&words=%D1%80",
    "readyState": "complete",
    "emptyResultBanner": "Нет подходящих запросов",
    "tableRows": 0,
    "saveButton": False,
}


class _Page:
    """Serves predicate answers and (once the fix lands) page snapshots."""

    def __init__(self, predicate_answer='"false"', snapshot=None, snapshot_error=None):
        self.predicate_answer = predicate_answer
        self.snapshot = snapshot
        self.snapshot_error = snapshot_error
        self.snapshot_requested = False

    async def get_elements_by_css_selector(self, selector):
        return [_Element()]

    async def evaluate(self, expression):
        if SNAPSHOT_MARK in expression:
            self.snapshot_requested = True
            if self.snapshot_error is not None:
                raise self.snapshot_error
            return json.dumps(self.snapshot, ensure_ascii=False)
        return self.predicate_answer


class _Element:
    async def fill(self, phrase):
        pass


def test_wait_for_timeout_reports_empty_result_banner(tmp_path):
    # The 2026-08-31 dynamics outage: the predicate (radio checked + save
    # button + table rows) never became true because Yandex returned no data.
    # The timeout error must show that banner — "markup changed" and
    # "Yandex-side outage" must not look identical in the logs.
    page = _Page(snapshot=BANNER_SNAPSHOT)
    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=0.1)

    with pytest.raises(InterfaceChangedError) as excinfo:
        asyncio.run(collector._wait_for(page, "() => Boolean(document.querySelector('label'))"))

    message = str(excinfo.value)
    assert "Нет подходящих запросов" in message
    assert "view=graph" in message
    assert '"tableRows": 0' in message


def test_set_phrase_failure_reports_empty_result_banner(monkeypatch, tmp_path):
    # The 2026-08-31 PhraseEntryError runs: the field held the right phrase
    # but the button stayed disabled. Whether React saw the input or the page
    # was degraded is exactly what the snapshot answers.
    page = _Page(
        predicate_answer='{"value":"ремонт квартир","searchDisabled":true}',
        snapshot=BANNER_SNAPSHOT,
    )

    async def get_elements(self, selector):
        return [_Element()]

    async def click(self, page, selector):
        pass

    async def wait(self, page, expression, seconds=None, required=True):
        pass

    monkeypatch.setattr(WordstatCollector, "get_elements_by_css_selector", get_elements, raising=False)
    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)

    collector = WordstatCollector("cdp", tmp_path)

    with pytest.raises(PhraseEntryError) as excinfo:
        asyncio.run(collector._set_phrase(page, "ремонт квартир"))

    message = str(excinfo.value)
    assert "Wordstat search field state" in message
    assert "Нет подходящих запросов" in message
    assert "view=graph" in message


def test_wait_for_timeout_survives_a_dead_cdp_connection(tmp_path):
    # The snapshot is taken on the failure path, where the CDP connection
    # itself may already be gone. Its evaluation error must not replace the
    # caller's original InterfaceChangedError.
    page = _Page(snapshot_error=RuntimeError("Target closed"))

    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=0.1)

    with pytest.raises(InterfaceChangedError, match="did not reach the expected page state"):
        asyncio.run(collector._wait_for(page, "() => false"))


def test_set_phrase_failure_survives_a_dead_cdp_connection(monkeypatch, tmp_path):
    page = _Page(
        predicate_answer='{"value":"ремонт квартир","searchDisabled":true}',
        snapshot_error=RuntimeError("Target closed"),
    )

    async def get_elements(self, selector):
        return [_Element()]

    async def click(self, page, selector):
        pass

    async def wait(self, page, expression, seconds=None, required=True):
        pass

    monkeypatch.setattr(WordstatCollector, "get_elements_by_css_selector", get_elements, raising=False)
    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)

    collector = WordstatCollector("cdp", tmp_path)

    with pytest.raises(PhraseEntryError, match="Wordstat search field state"):
        asyncio.run(collector._set_phrase(page, "ремонт квартир"))
