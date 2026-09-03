"""Shared knowledge about the Wordstat web UI.

Single source of truth for the URL, DOM selectors, and page probes used by
both the collector (`collector.py`) and the session commands (`auth.py`) —
kept here so the two cannot drift apart silently.
"""

from __future__ import annotations

WORDSTAT_URL = "https://wordstat.yandex.ru/"

QUERY_SELECTOR = 'input[placeholder="Введите слово или словосочетание"]'

# Page probe: current URL plus whether the «Выйти» link is rendered.
# The collector treats its absence (or a passport redirect) as
# AuthenticationRequiredError; `wordstat login`/`logout` use it to verify the
# effect of a cookie transfer.
AUTH_PROBE = """() => JSON.stringify({
    url: location.href,
    hasLogout: [...document.querySelectorAll('a')].some((link) => link.textContent?.trim() === 'Выйти'),
})"""
