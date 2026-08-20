"""Domain errors surfaced by the Wordstat collector."""


class WordstatError(RuntimeError):
    """Base class for a failed Wordstat collection."""


class AuthenticationRequiredError(WordstatError):
    """Wordstat is reachable but the attached browser is not authenticated."""


class InterfaceChangedError(WordstatError):
    """A required, uniquely identified Wordstat control was not found."""


class PhraseEntryError(WordstatError):
    """The search phrase could not be typed into the Wordstat input reliably."""


class DownloadTimeoutError(WordstatError):
    """The UI accepted an export request but did not produce a CSV file."""


class CsvFormatError(WordstatError):
    """A downloaded file cannot be decoded as a headed CSV report."""
