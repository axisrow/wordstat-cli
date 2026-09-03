"""Transfer an existing Yandex session into the attached Chrome (`wordstat login`).

Reads cookies for Yandex domains from another local Chrome profile (macOS:
SQLite `Cookies` database, values encrypted with the "Chrome Safe Storage"
Keychain key), decrypts them, and installs them into the attached Chrome via
the CDP ``Storage.setCookies`` domain. Nothing is written to disk, no cookie
name or value ever reaches a log line or a CLI message, and the target profile
is never cleared — ``setCookies`` only overwrites cookies with the exact same
(name, domain, path) key, so the import is idempotent and never destroys
unrelated cookies of the attached profile.

Deliberately not the retracted 2026-08-20 approach: that one copied the whole
``Cookies`` *file* over. The owner reversed the decision on 2026-09-01 in this
different, pointwise shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wordstat import dom
from wordstat.errors import SessionImportError

if TYPE_CHECKING:
    from cdp_use.cdp.network.types import CookieParam

# Suffix allowlist, matched exactly (`host == suffix or host.endswith("." + suffix)`).
# A `LIKE '%yandex%'` filter would be wrong twice: it admits junk such as
# `yandex.zoom.us` and `.datalens.yandex`, while the real session also needs
# hosts under yandex.com (metrica) and ya.ru (passport).
YANDEX_COOKIE_DOMAIN_SUFFIXES = ("yandex.ru", "ya.ru", "yandex.com", "yandex.net")

KEYCHAIN_SERVICE = "Chrome Safe Storage"

# Microseconds between 1601-01-01 (Chrome's epoch) and 1970-01-01 (Unix).
_CHROME_EPOCH_TO_UNIX_SECONDS = 11_644_473_600


@dataclass
class ImportReport:
    """Counters only — never cookie names or values."""

    imported: int = 0
    session_count: int = 0
    skipped_domains: int = 0
    skipped_expired: int = 0
    skipped_empty: int = 0
    skipped_partitioned: int = 0
    skipped_undecryptable: int = 0
    mismatched_after_set: int = 0
    domains: list[str] = field(default_factory=list)


@dataclass
class LogoutReport:
    """Counters only — never cookie names or values."""

    deleted: int = 0
    remaining: int = 0
    partitioned_left: int = 0


def is_supported_platform() -> bool:
    """Cookie decryption is only implemented for macOS Chrome profiles."""

    return sys.platform == "darwin"


def fetch_keychain_password() -> bytes:
    """Read the Chrome Safe Storage password from the macOS Keychain.

    May show a one-time GUI prompt; "Always Allow" silences it for future
    runs. Any failure (denied, missing item, non-macOS) is a SessionImportError
    with the tool's stderr tail — never the password itself.
    """

    if not is_supported_platform():
        raise SessionImportError(
            "wordstat login is only supported on macOS (Chrome stores cookie "
            "encryption keys in the macOS Keychain)"
        )
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:  # pragma: no cover - `security` missing entirely
        raise SessionImportError(f"Could not run the macOS `security` tool: {error}") from error
    if completed.returncode != 0 or not completed.stdout.strip():
        detail = completed.stderr.strip()[-200:]
        raise SessionImportError(
            f"Could not read the {KEYCHAIN_SERVICE!r} Keychain item (allow the "
            f"access prompt if it appeared). {detail}".strip()
        )
    # `security -w` prints the password followed by a single newline.
    return completed.stdout.rstrip("\n").encode("utf-8")


def derive_key(password: bytes) -> bytes:
    """Derive the 16-byte AES key Chrome uses for cookie values (macOS scheme)."""

    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)


def chrome_time_to_unix(expires_utc: int) -> float:
    """Convert Chrome's microsecond-since-1601 timestamps to Unix seconds."""

    return expires_utc / 1_000_000 - _CHROME_EPOCH_TO_UNIX_SECONDS


def _pkcs7_unpad(plaintext: bytes) -> bytes:
    if not plaintext or len(plaintext) % 16 != 0:
        raise ValueError("decrypted block is not a multiple of the AES block size")
    pad = plaintext[-1]
    if not 1 <= pad <= 16 or plaintext[-pad:] != bytes([pad]) * pad:
        raise ValueError("invalid PKCS7 padding")
    return plaintext[:-pad]


def decrypt_cookie_value(blob: bytes, key: bytes, host_key: str) -> str:
    """Decrypt one macOS Chrome ``encrypted_value``.

    Layout: ``b"v10"`` prefix, then AES-128-CBC with the 16-space IV, then
    PKCS7 padding. Since Chrome ~104 the plaintext starts with a 32-byte
    SHA-256 digest of the cookie's host key; Chromium hashes the canonical
    domain (no leading dot), while ``host_key`` stores domain cookies with a
    leading dot, so both spellings are tried. A plaintext whose prefix matches
    neither digest is treated as the pre-104 format and used whole.
    """

    if blob[:3] != b"v10":
        raise SessionImportError(
            "unsupported Chrome cookie encryption prefix "
            f"{bytes(blob[:3])!r}; only v10 (macOS Keychain scheme) is supported"
        )
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    try:
        plaintext = _pkcs7_unpad(decryptor.update(blob[3:]) + decryptor.finalize())
    except ValueError as error:
        raise SessionImportError(f"cookie value could not be decrypted: {error}") from error
    for host_variant in (host_key, host_key.lstrip(".")):
        digest = hashlib.sha256(host_variant.encode("utf-8")).digest()
        if plaintext[:32] == digest and len(plaintext) >= 32:
            plaintext = plaintext[32:]
            break
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SessionImportError(f"cookie value is not valid UTF-8: {error}") from error


def _samesite_param(samesite: int | None, is_secure: bool) -> str | None:
    """Map Chromium's CookieSameSite int to the CDP literal, defensively.

    -1 (unspecified) and unknown values map to an omitted field; SameSite=None
    without Secure would be rejected by Chrome (SameSiteNoneInsecure), so the
    field is dropped instead of forcing it.
    """

    if samesite is None:
        return None
    mapping = {0: "None", 1: "Lax", 2: "Strict"}
    value = mapping.get(samesite)
    if value == "None" and not is_secure:
        return None
    return value


def to_cdp_cookie(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one cookies-table row to a Storage.setCookies CookieParam.

    The domain is passed through as stored (leading dot = domain cookie,
    no dot = host-only) so Chrome's own canonicalization — including any
    __Secure-/__Host- prefix rules — applies. Fields Chrome can re-derive
    (sourceScheme, sourcePort, priority) and partitioning keys are omitted.
    """

    is_secure = bool(row["is_secure"])
    cookie: dict[str, Any] = {
        "name": row["name"],
        "value": row["_decrypted"],
        "domain": row["host_key"],
        "path": row["path"] or "/",
        "secure": is_secure,
        "httpOnly": bool(row["is_httponly"]),
    }
    raw_samesite = row["samesite"]
    samesite = _samesite_param(int(raw_samesite) if raw_samesite is not None else None, is_secure)
    if samesite is not None:
        cookie["sameSite"] = samesite
    has_expires = bool(row["has_expires"]) and row["expires_utc"]
    if has_expires:
        cookie["expires"] = chrome_time_to_unix(int(row["expires_utc"]))
    return cookie


def _is_yandex_host(host_key: str) -> bool:
    return any(host_key == suffix or host_key.endswith("." + suffix) for suffix in YANDEX_COOKIE_DOMAIN_SUFFIXES)


def _is_expired(row: dict[str, Any], now: float) -> bool:
    expires_utc = int(row["expires_utc"] or 0)
    return bool(row["has_expires"]) and expires_utc != 0 and chrome_time_to_unix(expires_utc) < now


def read_profile_cookies(cookies_db: Path, key: bytes) -> tuple[list[dict[str, Any]], ImportReport]:
    """Read, filter, and decrypt Yandex cookies from a Chrome profile.

    The database is opened read-only with a busy timeout, and the retry loop
    wraps the connect *and* the SELECT: the source is a live Chrome that keeps
    writing, and a lock can bite at either point. CHIPS-partitioned rows are
    skipped (counted, never imported) — see the loop below.
    """

    report = ImportReport()
    if not cookies_db.is_file():
        raise SessionImportError(f"No Chrome cookie database at {cookies_db}")

    # mode=ro, NOT immutable=1: immutable mode promises SQLite the file cannot
    # change, which is a lie for an open Chrome profile — it bypasses WAL
    # locking and can observe a mid-write snapshot. A plain read-only
    # connection takes proper shared locks; busy_timeout plus the retry
    # absorbs the busy/locked moments a live writer creates.
    uri = f"file:{cookies_db}?mode=ro"
    rows: list[sqlite3.Row] | None = None
    locked_error: sqlite3.OperationalError | None = None
    for attempt in range(3):
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 2000")
            rows = connection.execute(
                "SELECT host_key, name, value, encrypted_value, path, expires_utc,"
                " has_expires, is_secure, is_httponly, samesite, top_frame_site_key FROM cookies"
            ).fetchall()
            break
        except sqlite3.OperationalError as error:
            locked_error = error
            if attempt < 2:
                time.sleep(0.2)
        except sqlite3.DatabaseError as error:
            raise SessionImportError(f"{cookies_db} is not a readable Chrome cookie database: {error}") from error
        finally:
            if connection is not None:
                connection.close()
    if rows is None:
        raise SessionImportError(
            f"Could not read {cookies_db} while Chrome was writing it ({locked_error}); retry shortly"
        )

    now = time.time()
    cookies: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for raw in rows:
        row = dict(raw)
        if not _is_yandex_host(row["host_key"]):
            report.skipped_domains += 1
            continue
        # CHIPS-partitioned cookies are third-party-scoped (analytics/ads);
        # the Yandex *session* cookies are unpartitioned. Chrome's unique row
        # index keys on the partition columns too, so dropping the partition
        # key would both broaden a partitioned cookie into a globally visible
        # one and collide it with its unpartitioned namesake — skip instead.
        if row["top_frame_site_key"]:
            report.skipped_partitioned += 1
            continue
        if _is_expired(row, now):
            report.skipped_expired += 1
            continue
        blob = row["encrypted_value"]
        if blob:
            try:
                row["_decrypted"] = decrypt_cookie_value(bytes(blob), key, row["host_key"])
            except SessionImportError:
                report.skipped_undecryptable += 1
                continue
        else:
            row["_decrypted"] = row["value"] or ""
        if not row["_decrypted"]:
            report.skipped_empty += 1
            continue
        if "expires" not in (param := to_cdp_cookie(row)):
            report.session_count += 1
        # Two distinct Chrome rows that collapse onto the same CDP key would
        # be applied in arbitrary order, the second overwriting the first —
        # refuse instead of picking a winner nondeterministically. Keyed on
        # the raw (name, host_key, path): leading-dot host-only vs domain
        # spellings are distinct cookies in Chrome and must not merge here.
        key_tuple = (str(row["name"]), str(row["host_key"]), str(row["path"]))
        if key_tuple in seen_keys:
            raise SessionImportError(
                f"two source rows collide on the same cookie key {key_tuple!r} after dropping"
                " Chrome's full row identity; refusing to pick one arbitrarily"
            )
        seen_keys.add(key_tuple)
        cookies.append(param)

    if not cookies:
        raise SessionImportError(
            f"No usable Yandex cookies found in {cookies_db} "
            f"({report.skipped_undecryptable} undecryptable, "
            f"{report.skipped_expired} expired, {report.skipped_empty} empty)"
        )
    return cookies, report


def _cookie_key(cookie: dict[str, Any]) -> tuple[str, str, str]:
    domain = str(cookie["domain"]).lstrip(".")
    return str(cookie["name"]), domain, str(cookie["path"])


async def _is_authenticated(page) -> bool:
    """Same probe the collector uses (collector._assert_authenticated)."""

    state = json.loads(await page.evaluate(dom.AUTH_PROBE))
    return "passport.yandex.ru" not in state["url"] and bool(state["hasLogout"])


async def _wait_for_wordstat(page, seconds: float = 20.0) -> None:
    """Wait until the Wordstat UI has rendered (its search input exists)."""

    deadline = time.monotonic() + seconds
    selector = json.dumps(dom.QUERY_SELECTOR)
    while time.monotonic() < deadline:
        found = await page.evaluate(f"() => Boolean(document.querySelector({selector}))")
        if str(found).lower() in ("true", "1"):
            return
        await asyncio.sleep(0.25)
    raise SessionImportError(f"Wordstat did not render its search input within {seconds:g}s")


@asynccontextmanager
async def _attached_session(cdp_url: str) -> AsyncGenerator[tuple[Any, Any], None]:
    """One started BrowserSession plus one page, torn down the collector way.

    The single place in auth.py that knows how to open and (fail-safely)
    close an attached-Chrome session: page first, then session, both wrapped
    in ``except Exception`` so a dead CDP connection cannot mask the result.
    """

    from browser_use.browser import BrowserSession

    session = BrowserSession(
        cdp_url=cdp_url,
        is_local=True,
        downloads_path=None,
        allowed_domains=["wordstat.yandex.ru", "passport.yandex.ru"],
        keep_alive=True,
    )
    try:
        await session.start()
        page = None
        try:
            page = await session.new_page()
            yield session, page
        finally:
            if page is not None:
                try:
                    await session.close_page(page)
                except Exception:  # noqa: BLE001 - teardown must not mask the result
                    pass
    finally:
        try:
            await session.stop()
        except Exception:  # noqa: BLE001
            pass


async def check_auth(cdp_url: str) -> bool:
    """Open Wordstat in the attached Chrome and report whether it is authorized."""

    async with _attached_session(cdp_url) as (_, page):
        await page.goto(dom.WORDSTAT_URL)
        await _wait_for_wordstat(page)
        return await _is_authenticated(page)


async def import_session(cdp_url: str, profile_dir: Path) -> ImportReport:
    """Transfer Yandex cookies from a local Chrome profile into the attached Chrome."""

    key = derive_key(fetch_keychain_password())
    cookies, report = read_profile_cookies(profile_dir / "Cookies", key)

    async with _attached_session(cdp_url) as (session, page):
        # Storage.setCookies acts on the whole default browser context from the
        # root CDP client — no page session or url is needed. browser_use's own
        # _cdp_set_cookies is deliberately avoided: it silently no-ops without
        # an agent focus target.
        await session.cdp_client.send.Storage.setCookies(
            params={"cookies": cast("list[CookieParam]", cookies)}
        )

        stored = await session.cookies()
        # Only unpartitioned stored cookies count as confirmation: a stored
        # partitioned cookie shares the reduced (name, domain, path) triple and
        # would mask a failed unpartitioned write.
        stored_keys = {
            (cookie["name"], cookie["domain"].lstrip("."), cookie["path"])
            for cookie in stored
            if not cookie.get("partitionKey")
        }
        report.mismatched_after_set = sum(1 for cookie in cookies if _cookie_key(cookie) not in stored_keys)

        await page.goto(dom.WORDSTAT_URL)
        await _wait_for_wordstat(page)
        if not await _is_authenticated(page):
            raise SessionImportError(
                "cookies were installed but Wordstat still reports no session — "
                "the source profile is probably logged out of Yandex"
            )
        report.imported = len(cookies)
        report.domains = sorted({str(cookie["domain"]).lstrip(".") for cookie in cookies})
        return report


async def logout_session(cdp_url: str) -> LogoutReport:
    """Remove Yandex cookies — and only those — from the attached Chrome.

    The mirror of import_session, and partition-symmetric with it: only
    *unpartitioned* Yandex cookies (the session itself — exactly what login
    installs) are deleted. `Storage.deleteCookies` does not exist on this
    Chrome's browser endpoint, and its `Storage.setCookies` rejects any
    ``partitionKey`` outright (live-probed: every well-formed shape fails with
    ``Invalid cookie fields``), so partitioned cookies cannot be overwritten
    per-partition — they are third-party analytics, left in place and reported
    as ``partitioned_left`` instead of silently miscounted as deleted.
    Unpartitioned deletion is the proven expired-overwrite via root
    ``Storage.setCookies``, keyed by the exact stored (name, domain, path);
    unrelated cookies of the profile survive. Afterwards the attached Chrome
    is a logged-out profile again, restorable with `wordstat login`.
    """

    async with _attached_session(cdp_url) as (session, page):
        yandex_cookies = [
            cookie for cookie in await session.cookies() if _is_yandex_host(str(cookie["domain"]))
        ]
        doomed = [cookie for cookie in yandex_cookies if not cookie.get("partitionKey")]
        partitioned_left = len(yandex_cookies) - len(doomed)
        if doomed:
            expired = [
                {
                    "name": cookie["name"],
                    "value": "",
                    "domain": cookie["domain"],
                    "path": cookie["path"],
                    "expires": 1,  # 1970-01-01: Chrome drops the cookie on set
                }
                for cookie in doomed
            ]
            await session.cdp_client.send.Storage.setCookies(
                params={"cookies": cast("list[CookieParam]", expired)}
            )
        # Re-read: open Yandex tabs can recreate cookies between the overwrite
        # and this check, so `remaining` must be measured, not derived.
        remaining = sum(
            1
            for cookie in await session.cookies()
            if _is_yandex_host(str(cookie["domain"])) and not cookie.get("partitionKey")
        )

        await page.goto(dom.WORDSTAT_URL)
        await _wait_for_wordstat(page)
        if await _is_authenticated(page):
            raise SessionImportError(
                "Yandex cookies were removed but Wordstat still reports a session — "
                "try closing Wordstat tabs and running wordstat logout again"
            )
        return LogoutReport(deleted=len(doomed), remaining=remaining, partitioned_left=partitioned_left)
