"""Pictures a Comexio Doorstation stores when its bell is pressed or its motion sensor fires.

The Doorstation keeps the pictures as files under /images/doorstation/ and names each after the moment it was taken:
YY-MM-DD-HH-MM-SS_<kind>.jpg, with the kind `bell` (bell button), `pir` (motion sensor) or a name that holds `shot` (a
screenshot asked for by hand). The IO-Server lists them with POST /board/doorstation/getPictureList (form field
cameraId, the extension id) and answers with a JSON array of file names, or `null` (or nothing) for none. The files
are served by the Doorstation itself (/images/doorstation/<name>, no login) and by the IO-Server's file proxy
(/proxy/file/<extension id>/images/doorstation/<name>). Read from the page logic of Comexio OS 11.1.5 and the report
in kayl-codes/homeassistant-comexio#180; the order of the list is not known, so the pictures are sorted here.

Reading the answer is this module's job (parse_picture_list); asking for it and fetching a file is the client's.
A file name comes from the device and ends up in a URL path, so only a plain name (letters, digits, `.`, `_`, `-`,
ending in `.jpg`) counts: anything else is dropped from the list and never reaches a path.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .exceptions import ComexioDataError
from .extension import require_ext_id

_LOGGER = logging.getLogger(__name__)

PICTURE_DIRECTORY = "/images/doorstation/"
_PROXY_PATH = "/proxy/file/{ext_id}" + PICTURE_DIRECTORY

# A plain file name: it starts with a letter or digit (no dot-only name), has no slash, backslash, percent sign or
# other character a URL path treats specially, and ends in .jpg. ASCII only: \d and \w would take other scripts' digits.
_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.jpg", re.ASCII)
# The name the Doorstation gives a picture: year (two digits), month, day, hour, minute, second, the kind.
_PICTURE_NAME = re.compile(r"(\d\d)-(\d\d)-(\d\d)-(\d\d)-(\d\d)-(\d\d)_([A-Za-z0-9._-]+)\.jpg", re.ASCII)
_YEAR_BASE = 2000
# What the kind token of a name says; a name that holds "shot" anywhere is a screenshot, as Comexio's page reads it.
_BELL_TOKEN = "bell"
_MOTION_TOKEN = "pir"
_SCREENSHOT_TOKEN = "shot"
# The empty answers: no picture saved yet.
_EMPTY_ANSWERS = frozenset({"", "null"})


class DoorstationPictureKind(StrEnum):
    """What made the Doorstation take a picture."""

    BELL = "bell"
    MOTION = "motion"
    SCREENSHOT = "screenshot"
    OTHER = "other"


@dataclass(frozen=True, slots=True, kw_only=True)
class DoorstationPicture:
    """One stored picture of a Doorstation.

    name is the file name as the Doorstation lists it. kind says what made the picture (OTHER for a name that does
    not follow the pattern). taken_at is the moment in the name, without a time zone: it is the Doorstation's own
    clock, which is the local time of the installation when the device is set up right; None if the name carries no
    valid moment.
    """

    name: str
    kind: DoorstationPictureKind
    taken_at: datetime | None


def doorstation_picture_file_path(name: str) -> str:
    """Path of a picture file on the Doorstation itself (served without a login); name must be a plain file name."""
    return f"{PICTURE_DIRECTORY}{_require_plain_name(name)}"


def doorstation_picture_path(ext_id: int, name: str) -> str:
    """Path of a picture file on the IO-Server's file proxy; ext_id must be a positive int (not a bool)."""
    return _PROXY_PATH.format(ext_id=require_ext_id(ext_id)) + _require_plain_name(name)


def is_plain_picture_name(name: object) -> bool:
    """Whether name is a plain picture file name (see the module text): the only kind that is put into a path."""
    return isinstance(name, str) and _PLAIN_NAME.fullmatch(name) is not None


def _require_plain_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"name must be a str, not {type(name).__name__}")
    if _PLAIN_NAME.fullmatch(name) is None:
        raise ValueError("name must be a plain .jpg file name (letters, digits, '.', '_', '-')")
    return name


def picture_of(name: str) -> DoorstationPicture:
    """The DoorstationPicture a plain file name stands for; a name that is not plain raises ValueError."""
    _require_plain_name(name)
    match = _PICTURE_NAME.fullmatch(name)
    if match is None:
        return DoorstationPicture(name=name, kind=_kind_of(name, None), taken_at=None)
    *moment, token = match.groups()
    year, month, day, hour, minute, second = (int(part) for part in moment)  # two ASCII digits each: no failure
    try:
        taken_at: datetime | None = datetime(_YEAR_BASE + year, month, day, hour, minute, second)
    except ValueError:  # a month 13, a day 31 of April: the name keeps its kind, the moment is not known
        taken_at = None
    return DoorstationPicture(name=name, kind=_kind_of(name, token), taken_at=taken_at)


def _kind_of(name: str, token: str | None) -> DoorstationPictureKind:
    """The kind a name stands for: a name that holds `shot` anywhere is a screenshot (as the page of Comexio reads
    it), otherwise the kind token of the pattern says bell or motion; token is None for a name off the pattern."""
    if _SCREENSHOT_TOKEN in name.lower():
        return DoorstationPictureKind.SCREENSHOT
    lowered = (token or "").lower()
    if lowered == _MOTION_TOKEN:
        return DoorstationPictureKind.MOTION
    if lowered == _BELL_TOKEN:
        return DoorstationPictureKind.BELL
    return DoorstationPictureKind.OTHER


def parse_picture_list(text: str) -> tuple[DoorstationPicture, ...]:
    """The pictures of an answer of getPictureList, the newest first (empty: the Doorstation saved none).

    `null` and an empty answer mean no picture. Anything else must be a JSON array: another answer (an error page, a
    changed format) raises ComexioDataError, naming what is wrong but not quoting the answer. Entries that are not a
    plain file name (see the module text) are dropped and counted in one warning; if the list is not empty and none
    of its entries is usable, the format has changed and that raises ComexioDataError instead of reporting no
    picture. A name listed twice counts once. The order is by the moment in the name, the newest first; names with
    no moment come last, in name order, which is not a chronological order: a caller that wants the latest picture
    of a kind takes the first one of that kind that has a taken_at.
    """
    stripped = text.strip()
    if stripped in _EMPTY_ANSWERS:
        return ()
    try:
        listed = json.loads(stripped)
    except json.JSONDecodeError as err:
        # Neither the answer nor its decoder's message is quoted: an error page is not the device's to put into a log.
        raise ComexioDataError(f"Doorstation picture list is not JSON (offset {err.pos})") from None
    except RecursionError:  # an array nested deeper than the parser follows
        raise ComexioDataError("Doorstation picture list is nested too deeply") from None
    except ValueError:  # an integer of more than sys.get_int_max_str_digits() digits is no JSONDecodeError
        raise ComexioDataError("Doorstation picture list holds a number too long to read") from None
    if not isinstance(listed, list):
        raise ComexioDataError(f"Doorstation picture list is a JSON {type(listed).__name__}, not an array")
    usable = [entry for entry in listed if is_plain_picture_name(entry)]
    names = set(usable)
    if unusable := len(listed) - len(usable):
        _LOGGER.warning("Skipped %d entries of the Doorstation picture list that are no plain file name", unusable)
    if listed and not names:
        raise ComexioDataError(f"None of the {len(listed)} entries of the Doorstation picture list is a file name")
    return tuple(sorted((picture_of(name) for name in names), key=_newest_first))


def _newest_first(picture: DoorstationPicture) -> tuple[int, float, str]:
    """Sort key: pictures with a moment before those without, the later moment first, then by name."""
    if picture.taken_at is None:
        return (1, 0.0, picture.name)
    # Seconds from the smallest datetime: the moment is a wall-clock time, so no time zone enters the order.
    return (0, -(picture.taken_at - datetime.min).total_seconds(), picture.name)
