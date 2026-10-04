"""Tests for `wordstat login` cookie decryption and mapping (pure, no browser).

Encryption vectors are generated inside the tests with the same primitives
Chrome uses (AES-128-CBC + the PBKDF2-derived key), so no keychain access and
no real cookies are ever involved.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wordstat import auth, cli
from wordstat.auth import (
    ImportReport,
    LogoutReport,
    chrome_time_to_unix,
    decrypt_cookie_value,
    derive_key,
    to_cdp_cookie,
)
from wordstat.errors import SessionImportError

KEY = derive_key(b"peanuts")


def encrypt_cookie(plaintext: bytes, prefix: bytes = b"v10", pad: bool = True) -> bytes:
    """Build an encrypted_value blob the way macOS Chrome does."""

    if pad:
        pad_len = 16 - len(plaintext) % 16
        plaintext = plaintext + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(b" " * 16)).encryptor()
    return prefix + encryptor.update(plaintext) + encryptor.finalize()


def digest(host: str) -> bytes:
    return hashlib.sha256(host.encode()).digest()


@pytest.fixture()
def cookies_db(tmp_path: Path) -> Path:
    """A Chrome-like cookies database with synthetic rows (10 columns used by auth)."""

    path = tmp_path / "Cookies"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE cookies (
            host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, path TEXT,
            expires_utc INTEGER, has_expires INTEGER, is_secure INTEGER,
            is_httponly INTEGER, samesite INTEGER, top_frame_site_key TEXT
        )"""
    )
    connection.commit()
    connection.close()
    return path


def insert_cookie(
    db: Path, host: str, name: str = "cookie", encrypted: bytes = b"", value: str = "",
    expires_utc: int = 0, has_expires: int = 0, is_secure: int = 1, samesite: int = -1,
    top_frame_site_key: str = "",
) -> None:
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO cookies (host_key, name, value, encrypted_value, path, expires_utc,"
        " has_expires, is_secure, is_httponly, samesite, top_frame_site_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (host, name, value, encrypted, "/", expires_utc, has_expires, is_secure, 0, samesite,
         top_frame_site_key),
    )
    connection.commit()
    connection.close()


def test_derive_key_matches_known_pbkdf2_vector() -> None:
    assert KEY.hex() == "d9a09d499b4e1b7461f28e67972c6dbd"


def test_decrypt_strips_domain_digest_with_dotted_host() -> None:
    blob = encrypt_cookie(digest("yandex.ru") + b"session-value")
    assert decrypt_cookie_value(blob, KEY, ".yandex.ru") == "session-value"


def test_decrypt_strips_host_digest_without_dot() -> None:
    blob = encrypt_cookie(digest("passport.ya.ru") + b"login")
    assert decrypt_cookie_value(blob, KEY, "passport.ya.ru") == "login"


def test_decrypt_legacy_plaintext_without_digest() -> None:
    blob = encrypt_cookie(b"old-format")
    assert decrypt_cookie_value(blob, KEY, ".yandex.ru") == "old-format"


def test_decrypt_digest_only_means_empty_value() -> None:
    blob = encrypt_cookie(digest("yandex.ru"))
    assert decrypt_cookie_value(blob, KEY, ".yandex.ru") == ""


def test_decrypt_rejects_non_v10_prefix() -> None:
    blob = encrypt_cookie(b"whatever", prefix=b"v11")
    with pytest.raises(SessionImportError, match="unsupported Chrome cookie encryption"):
        decrypt_cookie_value(blob, KEY, ".yandex.ru")


def test_decrypt_rejects_invalid_padding() -> None:
    blob = encrypt_cookie(b"B" * 16, pad=False)
    with pytest.raises(SessionImportError, match="could not be decrypted"):
        decrypt_cookie_value(blob, KEY, ".yandex.ru")


def test_decrypt_rejects_non_utf8_value() -> None:
    blob = encrypt_cookie(digest("yandex.ru") + b"\xff\xfe")
    with pytest.raises(SessionImportError, match="not valid UTF-8"):
        decrypt_cookie_value(blob, KEY, ".yandex.ru")


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "host_key": ".yandex.ru", "name": "Session_id", "_decrypted": "abc",
        "path": "/", "expires_utc": 0, "has_expires": 0,
        "is_secure": 1, "is_httponly": 1, "samesite": -1,
    }
    row.update(overrides)
    return row


def test_samesite_mapping_matrix() -> None:
    assert "sameSite" not in to_cdp_cookie(_row(samesite=-1))
    assert to_cdp_cookie(_row(samesite=0, is_secure=1))["sameSite"] == "None"
    # SameSite=None without Secure would be rejected by Chrome — drop the field.
    assert "sameSite" not in to_cdp_cookie(_row(samesite=0, is_secure=0))
    assert to_cdp_cookie(_row(samesite=1))["sameSite"] == "Lax"
    assert to_cdp_cookie(_row(samesite=2))["sameSite"] == "Strict"
    assert "sameSite" not in to_cdp_cookie(_row(samesite=None))


def test_expires_omitted_for_session_cookies() -> None:
    assert "expires" not in to_cdp_cookie(_row(has_expires=0, expires_utc=0))
    assert "expires" not in to_cdp_cookie(_row(has_expires=1, expires_utc=0))


def test_expires_converted_from_chrome_epoch() -> None:
    unix = 1_756_684_800.0  # 2026-09-01T00:00:00Z
    chrome_utc = int((unix + 11_644_473_600) * 1_000_000)
    assert chrome_time_to_unix(chrome_utc) == unix
    assert to_cdp_cookie(_row(has_expires=1, expires_utc=chrome_utc))["expires"] == unix


def _future_chrome_utc() -> int:
    return int((time.time() + 3_600 + 11_644_473_600) * 1_000_000)


def test_read_profile_cookies_filters_and_counts(cookies_db: Path) -> None:
    future = _future_chrome_utc()
    insert_cookie(cookies_db, ".yandex.ru", encrypted=encrypt_cookie(digest("yandex.ru") + b"one"),
                  expires_utc=future, has_expires=1)
    insert_cookie(cookies_db, "passport.ya.ru", encrypted=encrypt_cookie(digest("passport.ya.ru") + b"two"),
                  expires_utc=future, has_expires=1)
    insert_cookie(cookies_db, ".mc.yandex.com", encrypted=encrypt_cookie(digest("mc.yandex.com") + b"three"),
                  expires_utc=future, has_expires=1)
    insert_cookie(cookies_db, ".yandex.ru", name="session",
                  encrypted=encrypt_cookie(digest("yandex.ru") + b"s"), has_expires=0)
    # Foreign hosts that merely contain "yandex" must be excluded.
    insert_cookie(cookies_db, "yandex.zoom.us", encrypted=encrypt_cookie(digest("yandex.zoom.us") + b"junk"))
    insert_cookie(cookies_db, ".datalens.yandex", encrypted=encrypt_cookie(b"junk"))
    insert_cookie(cookies_db, ".example.com", encrypted=encrypt_cookie(b"junk"))
    # Expired, empty, plain-value, and undecryptable rows.
    insert_cookie(cookies_db, ".yandex.ru", name="old", encrypted=encrypt_cookie(digest("yandex.ru") + b"x"),
                  expires_utc=1, has_expires=1)
    insert_cookie(cookies_db, ".yandex.ru", name="blank", encrypted=encrypt_cookie(digest("yandex.ru")))
    insert_cookie(cookies_db, ".yandex.ru", name="plain", value="plaintext",
                  expires_utc=future, has_expires=1)
    insert_cookie(cookies_db, ".yandex.ru", name="v11", encrypted=encrypt_cookie(b"x", prefix=b"v11"))

    cookies, report = auth.read_profile_cookies(cookies_db, KEY)

    assert {(c["name"], c["domain"]) for c in cookies} == {
        ("cookie", ".yandex.ru"), ("cookie", "passport.ya.ru"), ("cookie", ".mc.yandex.com"),
        ("session", ".yandex.ru"), ("plain", ".yandex.ru"),
    }
    assert cookies[0]["value"] == "one"
    assert report.skipped_domains == 3  # zoom.us, datalens.yandex, example.com
    assert report.skipped_expired == 1
    assert report.skipped_empty == 1
    assert report.skipped_undecryptable == 1
    assert report.session_count == 1  # only the has_expires=0 row
    # The report is counters only — there is no field a cookie value could hide in.
    assert set(report.__dataclass_fields__) >= {"imported", "session_count", "skipped_domains"}


def test_read_profile_cookies_skips_partitioned_rows(cookies_db: Path) -> None:
    future = _future_chrome_utc()
    # The same cookie name in both flavours: only the unpartitioned row is
    # the first-party session's business; the partitioned (CHIPS) copy is
    # third-party-scoped and must not be broadened to a global cookie.
    insert_cookie(cookies_db, ".yandex.ru", name="pi",
                  encrypted=encrypt_cookie(digest("yandex.ru") + b"first-party"),
                  expires_utc=future, has_expires=1)
    insert_cookie(cookies_db, ".yandex.ru", name="pi",
                  encrypted=encrypt_cookie(digest("yandex.ru") + b"partitioned"),
                  expires_utc=future, has_expires=1, top_frame_site_key="https://example.com")
    insert_cookie(cookies_db, ".yandex.ru", name="Session_id",
                  encrypted=encrypt_cookie(digest("yandex.ru") + b"x"),
                  expires_utc=future, has_expires=1, top_frame_site_key="https://shop.example")

    cookies, report = auth.read_profile_cookies(cookies_db, KEY)

    imported = [(c["name"], c["value"]) for c in cookies]
    assert imported == [("pi", "first-party")]
    assert report.skipped_partitioned == 2


def test_read_profile_cookies_rejects_reduced_key_collision(cookies_db: Path) -> None:
    future = _future_chrome_utc()
    # Two distinct Chrome rows (the real unique index also keys on
    # source_scheme/source_port) that collapse onto the same CDP key.
    insert_cookie(cookies_db, ".yandex.ru", name="dupe",
                  encrypted=encrypt_cookie(digest("yandex.ru") + b"one"),
                  expires_utc=future, has_expires=1)
    insert_cookie(cookies_db, ".yandex.ru", name="dupe",
                  encrypted=encrypt_cookie(digest("yandex.ru") + b"two"),
                  expires_utc=future, has_expires=1)

    with pytest.raises(SessionImportError, match="collide"):
        auth.read_profile_cookies(cookies_db, KEY)


def test_read_profile_cookies_retries_transient_select_failure(cookies_db: Path, monkeypatch) -> None:
    insert_cookie(cookies_db, ".yandex.ru", encrypted=encrypt_cookie(digest("yandex.ru") + b"one"),
                  expires_utc=_future_chrome_utc(), has_expires=1)

    class FlakyConnection:
        """Real connection whose first execute() raises like a live-DB lock.

        The failure counter is a class attribute: the retry loop opens a fresh
        connection per attempt, and the simulated hiccup must fire once in
        total, not once per attempt.
        """

        failures_left = 1

        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        @property
        def row_factory(self):
            return self._real.row_factory

        @row_factory.setter
        def row_factory(self, value) -> None:
            self._real.row_factory = value

        def execute(self, *args: object):
            if FlakyConnection.failures_left:
                FlakyConnection.failures_left -= 1
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(*args)

        def close(self) -> None:
            self._real.close()

    real_connect = sqlite3.connect
    monkeypatch.setattr(
        auth.sqlite3, "connect", lambda *a, **k: FlakyConnection(real_connect(*a, **k))
    )
    cookies, _report = auth.read_profile_cookies(cookies_db, KEY)
    assert [c["value"] for c in cookies] == ["one"]


def test_read_profile_cookies_requires_usable_rows(cookies_db: Path) -> None:
    insert_cookie(cookies_db, ".example.com", encrypted=encrypt_cookie(b"junk"))
    with pytest.raises(SessionImportError, match="No usable Yandex cookies"):
        auth.read_profile_cookies(cookies_db, KEY)


def test_read_profile_cookies_missing_db(tmp_path: Path) -> None:
    with pytest.raises(SessionImportError, match="No Chrome cookie database"):
        auth.read_profile_cookies(tmp_path / "Cookies", KEY)


def test_login_check_reports_true(monkeypatch, tmp_path: Path) -> None:
    async def fake_check_auth(cdp_url: str) -> bool:
        return True

    monkeypatch.setattr(auth, "check_auth", fake_check_auth)
    result = CliRunner().invoke(cli.main, ["login", "--check", "--from-chrome-profile", str(tmp_path)])
    assert result.exit_code == 0
    assert "подтверждена" in result.output


def test_login_check_reports_false(monkeypatch, tmp_path: Path) -> None:
    async def fake_check_auth(cdp_url: str) -> bool:
        return False

    monkeypatch.setattr(auth, "check_auth", fake_check_auth)
    result = CliRunner().invoke(cli.main, ["login", "--check", "--from-chrome-profile", str(tmp_path)])
    assert result.exit_code == 1
    assert "not authorized" in result.output


def test_login_import_prints_counters(monkeypatch, tmp_path: Path) -> None:
    report = ImportReport(
        imported=373, session_count=11, skipped_domains=4082, skipped_expired=1,
        skipped_empty=1, skipped_undecryptable=0, mismatched_after_set=0,
        domains=["ya.ru", "yandex.com", "yandex.ru"],
    )

    async def fake_import_session(cdp_url: str, profile_dir: Path) -> ImportReport:
        return report

    monkeypatch.setattr(auth, "import_session", fake_import_session)
    result = CliRunner().invoke(cli.main, ["login", "--from-chrome-profile", str(tmp_path)])
    assert result.exit_code == 0
    assert "Перенесено кук: 373" in result.output
    assert "сессионных" in result.output
    assert "подтверждена" in result.output


def test_login_import_error_becomes_click_exception(monkeypatch, tmp_path: Path) -> None:
    async def fake_import_session(cdp_url: str, profile_dir: Path) -> ImportReport:
        raise SessionImportError("boom: keychain denied")

    monkeypatch.setattr(auth, "import_session", fake_import_session)
    result = CliRunner().invoke(cli.main, ["login", "--from-chrome-profile", str(tmp_path)])
    assert result.exit_code == 1
    assert "boom: keychain denied" in result.output
    assert result.exception is not None and "Traceback" not in result.output


def test_fetch_keychain_password_rejects_non_darwin(monkeypatch) -> None:
    monkeypatch.setattr(auth.sys, "platform", "linux")
    with pytest.raises(SessionImportError, match="only supported on macOS"):
        auth.fetch_keychain_password()


def test_logout_prints_counters(monkeypatch) -> None:
    async def fake_logout_session(cdp_url: str) -> LogoutReport:
        return LogoutReport(deleted=369, remaining=0, partitioned_left=4)

    monkeypatch.setattr(auth, "logout_session", fake_logout_session)
    result = CliRunner().invoke(cli.main, ["logout"])
    assert result.exit_code == 0
    assert "Удалено Yandex-кук: 369" in result.output
    assert "оставлено партиционированных, не относящихся к сессии: 4" in result.output
    assert "отсутствует" in result.output


def test_logout_still_authorized_fails(monkeypatch) -> None:
    async def fake_logout_session(cdp_url: str) -> object:
        raise SessionImportError("still reports a session")

    monkeypatch.setattr(auth, "logout_session", fake_logout_session)
    result = CliRunner().invoke(cli.main, ["logout"])
    assert result.exit_code == 1
    assert "still reports a session" in result.output
