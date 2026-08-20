from pathlib import Path

import wordstat.collector as collector_module
from wordstat.collector import WordstatCollector
from wordstat.models import CollectionManifest, CollectionResult


class _Page:
    async def goto(self, url):
        pass


class _Session:
    instances = 0

    def __init__(self, **kwargs):
        type(self).instances += 1
        self.downloaded_files = []

    async def start(self):
        pass

    async def new_page(self):
        return _Page()

    async def stop(self):
        pass


def test_collect_many_reuses_session_and_isolates_phrase_failures(monkeypatch, tmp_path: Path):
    _Session.instances = 0
    monkeypatch.setattr(collector_module, "BrowserSession", _Session)
    monkeypatch.setattr(WordstatCollector, "_wait_for", lambda *args: _async_none())
    monkeypatch.setattr(WordstatCollector, "_assert_authenticated", lambda *args: _async_none())
    monkeypatch.setattr(WordstatCollector, "_set_region", lambda *args: _async_none())

    async def collect_phrase(self, page, session, phrase, region):
        if phrase == "сломается":
            raise RuntimeError("broken phrase")
        return CollectionResult(
            run_directory=tmp_path / phrase,
            manifest_path=tmp_path / phrase / "manifest.json",
            manifest=CollectionManifest.model_construct(),
        )

    monkeypatch.setattr(WordstatCollector, "_collect_phrase", collect_phrase)
    import asyncio

    result = asyncio.run(WordstatCollector("cdp", tmp_path).collect_many(["первый", "сломается", "второй"]))

    assert _Session.instances == 1
    assert [item.manifest_path.parent.name for item in result.results] == ["первый", "второй"]
    assert [(item.phrase, item.error) for item in result.failures] == [("сломается", "broken phrase")]
    assert result.total == 3


async def _async_none():
    return None
