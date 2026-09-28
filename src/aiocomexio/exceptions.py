"""Exceptions raised by the Comexio client.

Every client call either returns real data or raises one of these — never an empty
placeholder that a caller could mistake for "the server has nothing to report".
Invalid arguments are the caller's bug, not the server's: they raise the usual
ValueError / KeyError / TypeError, never a ComexioError.
"""


class ComexioError(Exception):
    """Base class for every error the client raises."""


class ComexioConnectionError(ComexioError):
    """The server could not be reached, or the connection broke or timed out mid-request."""


class ComexioAuthenticationError(ComexioError):
    """The server rejected the admin credentials."""


class ComexioResponseError(ComexioError):
    """The server answered with an HTTP error status."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class ComexioDataError(ComexioError):
    """The server answered, but the payload was not in the expected shape."""


class ComexioRequestRejectedError(ComexioError):
    """The server understood a write request but refused it (e.g. a name already in use)."""
