"""Domain errors surfaced by the Wordstat collector."""


class WordstatError(RuntimeError):
    """Base class for a failed Wordstat collection."""


class InvalidRequestError(WordstatError):
    """The requested phrase or region is unusable before the browser is touched."""


class InvalidPeriodError(InvalidRequestError):
    """The requested dynamics granularity/window is not supported."""


class AuthenticationRequiredError(WordstatError):
    """Wordstat is reachable but the attached browser is not authenticated."""


class InterfaceChangedError(WordstatError):
    """A required, uniquely identified Wordstat control was not found."""


class PhraseEntryError(WordstatError):
    """The search phrase could not be typed into the Wordstat input reliably."""


class DownloadTimeoutError(WordstatError):
    """The UI accepted an export request but did not produce a CSV file."""


class DownloadEscapedError(WordstatError):
    """Chrome reported a download outside the run's own downloads directory.

    session.downloaded_files (browser-use) accumulates every path Chrome has
    ever reported as downloaded across the whole CDP session, regardless of
    where it actually landed — see issue #27, where the fourth view of a
    phrase (regions) was reported at an absolute path under the user's real
    ~/Downloads instead of the collector's own temporary downloads_path. That
    path must never be moved or deleted (it may be a file the user cares
    about, and may not even belong to this tool's run at all): this error is
    raised instead, so the file is left untouched and the failure is loud
    rather than a silent Errno 1/2 from finalize_raw trying to relocate it.
    """


class CsvFormatError(WordstatError):
    """A downloaded file cannot be decoded as a headed CSV report."""


class ResumeMismatchError(WordstatError):
    """--resume-dir does not belong to the requested phrase/region, or is unusable."""
