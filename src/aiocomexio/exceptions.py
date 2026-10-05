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
    """The server answered with an HTTP error status.

    body is a whitespace-collapsed, length-capped excerpt of the error page (Comexio's error pages
    name the failing check), also appended to the message; None when there was none to read.
    """

    def __init__(self, message: str, *, status: int, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ComexioDataError(ComexioError):
    """The server answered, but the payload was not in the expected shape."""


class ComexioRequestRejectedError(ComexioError):
    """The server understood a write request but refused it (e.g. a name already in use)."""


class ComexioCreatedWithoutIdError(ComexioError):
    """The server confirmed a create request, but the new object's id could not be read back.

    The object exists — creating it again would leave a duplicate. __cause__ is the error that
    stopped the read-back.
    """
