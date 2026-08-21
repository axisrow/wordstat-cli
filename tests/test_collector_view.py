"""View-selection retry behavior without touching a browser."""

import asyncio

import pytest

from wordstat.collector import WordstatCollector
from wordstat.errors import InterfaceChangedError


def test_select_view_retries_once_when_active_marker_does_not_change(monkeypatch, tmp_path):
    clicks = []
    waits = []

    async def click(self, page, selector):
        clicks.append(selector)

    async def wait(self, page, expression, seconds=None, required=True):
        waits.append((expression, seconds, required))
        if len(waits) == 1:
            raise InterfaceChangedError("view did not change")

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)

    asyncio.run(WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='map']"))

    assert clicks == ["label[for='map']", "label[for='map']"]
    assert len(waits) == 2
    assert "input" in waits[0][0]
    assert ".table__wrapper" not in waits[0][0]


def test_select_view_raises_after_one_retry(monkeypatch, tmp_path):
    clicks = 0

    async def click(self, page, selector):
        nonlocal clicks
        clicks += 1

    async def wait(self, page, expression, seconds=None, required=True):
        raise InterfaceChangedError("view did not change")

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)

    with pytest.raises(InterfaceChangedError, match="view did not change"):
        asyncio.run(WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='graph']"))

    assert clicks == 2
