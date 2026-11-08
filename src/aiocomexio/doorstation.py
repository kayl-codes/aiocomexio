"""Comexio Doorstation: its detection on the IO home page and the first frame of its camera stream.

The IO home page (/admin/in_output/home) renders one tab per extension. The elements of a Doorstation tab carry
data-extid, data-extname and data-camera_id, the ids holding the extension id (reported for Comexio OS 11.0.2 in
kayl-codes/homeassistant-comexio#180). The IO-Server serves the camera as an MJPEG stream (multipart/x-mixed-replace,
one JPEG per part) under /proxy/cam/<camera_id>/; the Doorstation itself serves the same stream under its own address.

Reading the tags of the page is the client's job (a browser-like html.parser, see ComexioClient.get_doorstations);
this module turns their attributes into Doorstation entries and finds a complete JPEG in stream bytes.
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .config import _log_safe, _strict_id
from .exceptions import ComexioDataError

_LOGGER = logging.getLogger(__name__)

EXT_ID_ATTRIBUTE = "data-extid"
EXT_NAME_ATTRIBUTE = "data-extname"
CAMERA_ID_ATTRIBUTE = "data-camera_id"
# The attributes a Doorstation element carries that this module reads; a parser keeps only these.
DOORSTATION_ATTRIBUTES = (EXT_ID_ATTRIBUTE, EXT_NAME_ATTRIBUTE, CAMERA_ID_ATTRIBUTE)

_STREAM_PATH = "/proxy/cam/{camera_id}/"

# JPEG markers (ITU-T T.81, B.1.1.3). A marker is 0xFF and a code; 0xFF bytes before the code are fill bytes.
_MARKER_PREFIX = 0xFF
_SOI = b"\xff\xd8"
_EOI_CODE = 0xD9
# The EOI marker that ends an image; a reader of a stream needs to look for a complete frame only once it arrived.
JPEG_EOI = bytes((_MARKER_PREFIX, _EOI_CODE))
_SOS_CODE = 0xDA
# Codes of markers without a length field: TEM and the restart markers RST0-RST7.
_STANDALONE_CODES = frozenset({0x01, *range(0xD0, 0xD8)})
# Codes that cannot follow the start of an image as a segment: a stuffed byte belongs into a scan only, and a second
# SOI would be a new image. Read as a segment, their made-up length would skip past the real end of the image.
_NO_SEGMENT_CODES = frozenset({0x00, _SOI[1]})
# Inside entropy-coded data 0xFF 0x00 is a stuffed 0xFF byte and RST0-RST7 belong to the scan.
_IN_SCAN_CODES = frozenset({0x00, *range(0xD0, 0xD8)})
# A segment's length field counts itself, so it is at least 2.
_MIN_SEGMENT_LENGTH = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class Doorstation:
    """One Doorstation of the IO-Server.

    ext_id is the id of its extension, camera_id the id of its camera on the IO-Server's stream proxy, name the
    extension name (None if the page carries none).
    """

    ext_id: int
    camera_id: int
    name: str | None

    @property
    def stream_path(self) -> str:
        """Path of the camera's MJPEG stream on the IO-Server."""
        return doorstation_stream_path(self.camera_id)


def doorstation_stream_path(camera_id: int) -> str:
    """Path of the MJPEG stream of camera_id on the IO-Server; camera_id must be a positive int (not a bool)."""
    if isinstance(camera_id, bool) or not isinstance(camera_id, int):
        raise TypeError(f"camera_id must be an int, not {type(camera_id).__name__}")
    if _strict_id(camera_id) is None or camera_id <= 0:
        raise ValueError("camera_id must be a positive Comexio id")
    return _STREAM_PATH.format(camera_id=camera_id)


def _positive_id(text: str | None) -> int | None:
    """An attribute value as a positive Comexio id (ASCII digits only), None otherwise."""
    canonical = _strict_id(text)
    return None if canonical is None or canonical == "0" else int(canonical)


def _doorstation_of(attributes: Mapping[str, str]) -> Doorstation | None:
    """The Doorstation an element's attributes name, None if its ids are not usable."""
    ext_id = _positive_id(attributes.get(EXT_ID_ATTRIBUTE))
    camera_id = _positive_id(attributes.get(CAMERA_ID_ATTRIBUTE))
    if ext_id is None or camera_id is None:
        return None
    return Doorstation(ext_id=ext_id, camera_id=camera_id, name=attributes.get(EXT_NAME_ATTRIBUTE) or None)


def doorstations_from_attributes(elements: Iterable[Mapping[str, str]]) -> dict[int, Doorstation]:
    """The Doorstations named by the attributes of the elements carrying data-camera_id, keyed by extension id.

    Several elements name the same Doorstation; the first one counts. An element without a name does not clash with
    a named one: the name comes from whichever element carries it. A later element that names the Doorstation with
    another camera or another name is logged. An element with an empty data-camera_id names no camera and is
    skipped (counted at DEBUG); one whose ids are not positive integers is logged and skipped. If elements exist but
    none is usable, the page has changed: that raises ComexioDataError rather than reporting no Doorstation.
    """
    found: dict[int, Doorstation] = {}
    unusable = 0
    without_camera = 0
    for attributes in elements:
        if not attributes.get(CAMERA_ID_ATTRIBUTE):
            without_camera += 1
            continue
        doorstation = _doorstation_of(attributes)
        if doorstation is None:
            unusable += 1
            continue
        found[doorstation.ext_id] = _merged(found.get(doorstation.ext_id), doorstation)
    if without_camera:
        _LOGGER.debug("Skipped %d Doorstation elements with an empty camera id", without_camera)
    if unusable:
        _LOGGER.warning("Skipped %d Doorstation elements without a usable extension and camera id", unusable)
    if unusable and not found:
        raise ComexioDataError(f"None of the {unusable} Doorstation elements has a usable extension and camera id")
    _warn_on_shared_cameras(found)
    return found


def _merged(known: Doorstation | None, doorstation: Doorstation) -> Doorstation:
    """known with what a later element of the same extension adds: its name if known has none; a clash is logged."""
    if known is None:
        return doorstation
    if known.camera_id == doorstation.camera_id and None in (known.name, doorstation.name):
        return known if known.name is not None else doorstation
    if known != doorstation:
        _LOGGER.warning(
            "Doorstation of extension %d is named twice (camera %d '%s', then camera %d '%s'), the first one counts",
            known.ext_id,
            known.camera_id,
            _log_safe(known.name or ""),
            doorstation.camera_id,
            _log_safe(doorstation.name or ""),
        )
    return known


def _warn_on_shared_cameras(doorstations: Mapping[int, Doorstation]) -> None:
    """Log Doorstations that name the same camera: both stay, but their streams are the same."""
    by_camera: dict[int, list[int]] = {}
    for doorstation in doorstations.values():
        by_camera.setdefault(doorstation.camera_id, []).append(doorstation.ext_id)
    for camera_id, ext_ids in by_camera.items():
        if len(ext_ids) > 1:
            _LOGGER.warning("Doorstations of extensions %s share the camera %d", sorted(ext_ids), camera_id)


def first_jpeg_frame(data: bytes | bytearray) -> bytes | None:
    """The first complete JPEG image in data (e.g. the start of an MJPEG stream), None if it is not complete yet.

    The image runs from its SOI marker to the EOI marker that ends it: the segments before a scan are skipped by
    their length fields, so an EOI inside them (an embedded thumbnail) does not end the image early. Data before
    the first SOI (a multipart boundary and its headers) is skipped. A marker that is missing where the structure of
    a JPEG needs one, a segment shorter than its length field, or a stuffed byte or second SOI between the segments
    raises ComexioDataError.
    """
    start = data.find(_SOI)
    if start < 0:
        return None
    end = _image_end(data, start + len(_SOI))
    return None if end is None else bytes(data[start:end])


def _image_end(data: bytes | bytearray, pos: int) -> int | None:
    """Offset after the EOI of the image whose segments start at pos, None if data ends before it."""
    while True:
        code_at = _marker_code_at(data, pos)
        if code_at is None:
            return None
        code = data[code_at]
        if code == _EOI_CODE:
            return code_at + 1
        if code in _NO_SEGMENT_CODES:
            raise ComexioDataError(f"Camera frame has the unexpected JPEG marker 0x{code:02X} at offset {code_at}")
        next_pos = code_at + 1 if code in _STANDALONE_CODES else _segment_end(data, code, code_at + 1)
        if next_pos is None:
            return None
        pos = next_pos


def _segment_end(data: bytes | bytearray, code: int, pos: int) -> int | None:
    """Offset after the segment whose length field starts at pos (after its scan for an SOS), None if data ends."""
    if pos + 2 > len(data):
        return None
    length = int.from_bytes(data[pos : pos + 2])
    if length < _MIN_SEGMENT_LENGTH:
        raise ComexioDataError(f"Camera frame has a JPEG segment of length {length}")
    pos += length
    return _scan_end(data, pos) if code == _SOS_CODE else pos


def _marker_code_at(data: bytes | bytearray, pos: int) -> int | None:
    """Offset of the code of the marker at pos (fill bytes skipped), None if data ends before it."""
    if pos >= len(data):
        return None
    if data[pos] != _MARKER_PREFIX:
        raise ComexioDataError(f"Camera frame has no JPEG marker at offset {pos}")
    while pos < len(data) and data[pos] == _MARKER_PREFIX:
        pos += 1
    return pos if pos < len(data) else None


def _scan_end(data: bytes | bytearray, pos: int) -> int | None:
    """Offset of the marker that ends the entropy-coded data starting at pos, None if data ends before it."""
    while True:
        pos = data.find(_MARKER_PREFIX, pos)
        if pos < 0 or pos + 1 >= len(data):
            return None
        # A second 0xFF is a fill byte before a marker; _marker_code_at skips it.
        if data[pos + 1] not in _IN_SCAN_CODES:
            return pos
        pos += 2
