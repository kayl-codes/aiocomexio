"""Comexio Doorstation: its detection on the IO home page and the first frame of its camera stream.

The IO home page (/admin/in_output/home) renders one tab per extension. The camera-tile elements of a Doorstation tab
carry data-extid, data-extname and data-camera_id, the ids holding the extension id (reported for Comexio OS 11.0.2 in
kayl-codes/homeassistant-comexio#180); other controls of the tab, such as the "delete pictures" button, may carry only
data-camera_id. Comexio OS 11.1.5 serves the tile without those attributes: the button is then the only element that
names the ids, and a Doorstation is told from a cameraless one by its extension's serial number (see
picture_button_ids). The IO-Server serves the camera as an MJPEG stream (multipart/x-mixed-replace,
one JPEG per part) under /proxy/cam/<camera_id>/; the Doorstation itself serves the same stream under its own address.

Reading the tags of the page is the client's job (a browser-like html.parser, see ComexioClient.get_doorstations);
this module turns their attributes into Doorstation entries and finds a complete JPEG in stream bytes.
"""

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from .config import _log_safe, _strict_id
from .exceptions import ComexioDataError

_LOGGER = logging.getLogger(__name__)

EXT_ID_ATTRIBUTE = "data-extid"
EXT_NAME_ATTRIBUTE = "data-extname"
CAMERA_ID_ATTRIBUTE = "data-camera_id"
# The attributes a Doorstation element carries that this module reads; a parser keeps only these.
DOORSTATION_ATTRIBUTES = (EXT_ID_ATTRIBUTE, EXT_NAME_ATTRIBUTE, CAMERA_ID_ATTRIBUTE)

_STREAM_PATH = "/proxy/cam/{camera_id}/"
_PICTURE_BUTTON_ID = re.compile(r"delete_doorstation_pictures_(\d+)", re.ASCII)

# JPEG markers (ITU-T T.81, B.1.1.3). A marker is 0xFF and a code; 0xFF bytes before the code are fill bytes.
_MARKER_PREFIX = 0xFF
_SOI = b"\xff\xd8"
_EOI_CODE = 0xD9
# The EOI marker that ends an image; a reader of a stream needs to look for a complete frame only once it arrived.
JPEG_EOI = bytes((_MARKER_PREFIX, _EOI_CODE))
_SOS_CODE = 0xDA
# The start-of-frame markers SOF0-SOF15 carry the image's size and components; the codes between them that are no
# frame header are DHT (0xC4), JPG (0xC8) and DAC (0xCC). A scan is read with the header of its frame (T.81, B.2.2).
_SOF_CODES = frozenset(set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC})
# Codes of markers without a length field: TEM and the restart markers RST0-RST7.
_STANDALONE_CODES = frozenset({0x01, *range(0xD0, 0xD8)})
# Codes that cannot follow the start of an image as a segment: a stuffed byte belongs into a scan only, and a second
# SOI would be a new image. Read as a segment, their made-up length would skip past the real end of the image.
_NO_SEGMENT_CODES = frozenset({0x00, _SOI[1]})
# Inside entropy-coded data 0xFF 0x00 is a stuffed 0xFF byte and RST0-RST7 belong to the scan.
_IN_SCAN_CODES = frozenset({0x00, *range(0xD0, 0xD8)})
# Restart markers with their fill bytes and nothing else: a scan of only these has no entropy-coded data.
_MARKERS_ONLY = re.compile(rb"(?:\xff+[\xd0-\xd7])*")
# A segment's length field counts itself, so it is at least 2.
_MIN_SEGMENT_LENGTH = 2
# An SOS header (T.81, B.2.3): length field, Ns, two bytes per scan component, then Ss, Se and Ah/Al, with 1 to 4
# components; its length is 6 + 2 * Ns.
_SOS_FIXED_LENGTH = 6
_SOS_BYTES_PER_COMPONENT = 2
_MAX_SCAN_COMPONENTS = 4
# Offset of Ns from the SOS code: the two bytes of the length field lie between them.
_SOS_COMPONENTS_OFFSET = 3
# An SOF header (T.81, B.2.2): length field, sample precision, height, width, Nf, then three bytes per component (id,
# the horizontal and vertical sampling factors as two nibbles, quantization table); its length is 8 + 3 * Nf. A height
# of 0 would need a DNL marker, which a still from a camera never has, so it counts as malformed.
_SOF_FIXED_LENGTH = 8
_SOF_BYTES_PER_COMPONENT = 3
# Sample precision by process (T.81, Table B.2): baseline DCT (SOF0) 8 bits; the other DCT processes (extended,
# progressive, differential, arithmetic-coded) 8 or 12; the lossless ones (SOF3, 7, 11, 15) 2 to 16.
_BASELINE_CODE = 0xC0
_LOSSLESS_CODES = frozenset({0xC3, 0xC7, 0xCB, 0xCF})
_BASELINE_PRECISION = 8
_DCT_PRECISIONS = frozenset({8, 12})
_LOSSLESS_PRECISIONS = range(2, 17)
_MAX_SAMPLING_FACTOR = 4
_MAX_QUANTIZATION_TABLE = 3
# Scan parameters Ss, Se and Ah/Al by process (T.81, B.2.3 and Table B.3, G.1.1.1.1, H.1.2.1): a sequential DCT scan
# holds the whole block (Ss 0, Se 63) without successive approximation; a progressive one a DC scan (Ss = Se = 0) or
# the AC band of one component, with Ah 0 or Al + 1 and shifts up to 13; a lossless one a predictor 0 to 7 in Ss, Se 0,
# Ah 0 and a point transform (the low nibble, 0 to 15, whatever it is).
_PROGRESSIVE_CODES = frozenset({0xC2, 0xC6, 0xCA, 0xCE})
_LAST_COEFFICIENT = 63
_MAX_SUCCESSIVE_SHIFT = 13
_MAX_PREDICTOR = 7
# Offsets from the SOF code: precision, height, width, Nf and the first component.
_SOF_PRECISION_OFFSET = 3
_SOF_HEIGHT_OFFSET = 4
_SOF_WIDTH_OFFSET = 6
_SOF_COMPONENTS_OFFSET = 8
_SOF_FIRST_COMPONENT_OFFSET = 9


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


def picture_button_ids(attributes: Mapping[str, str]) -> tuple[int, int] | None:
    """(extension id, camera id) a "delete pictures" button names, None if it is none or its ids are unusable.

    Comexio OS 11.1.5 renders the camera tile without any data-* attribute (the picture is loaded by script), so the
    only element of a Doorstation's tab that names its ids in the served page is this button:
    id="delete_doorstation_pictures_<extension id>" with data-camera_id="<camera id>" (kayl-codes/homeassistant-comexio
    #180). The button is there on a Doorstation that has a camera as much as on one that has none, so it only names a
    candidate: ComexioClient.get_doorstations asks the extension's edit form whether it is a Doorstation with a
    serial number.
    """
    match = _PICTURE_BUTTON_ID.fullmatch(attributes.get("id", ""))
    if match is None:
        return None
    ext_id = _positive_id(match.group(1))
    camera_id = _positive_id(attributes.get(CAMERA_ID_ATTRIBUTE))
    if ext_id is None or camera_id is None:
        _log_unusable_picture_button(attributes)
        return None
    return ext_id, camera_id


def _log_unusable_picture_button(attributes: Mapping[str, str]) -> None:
    """Say that a pictures button names no candidate: an empty camera id is a known state, any other failure is not."""
    if attributes.get(CAMERA_ID_ATTRIBUTE) == "":
        _LOGGER.debug("Skipped a Doorstation pictures button with an empty camera id")
    else:
        _LOGGER.warning("Skipped a Doorstation pictures button without a usable extension and camera id")


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
    skipped (counted at DEBUG). An element that carries data-camera_id but no data-extid at all names no Doorstation
    either and is skipped the same way, however many such elements appear: Comexio's "delete this Doorstation's
    pictures" button carries its own data-camera_id without a data-extid, on a Doorstation extension without a
    camera tile (none is configured, or none has ever connected) just as much as on one with a working camera -
    a page with only such buttons describes real, cameraless Doorstations, not a changed page, so it reports no
    Doorstation rather than raising (on a page of Comexio OS 11.1.5, where the tile has no ids, the client reads
    the buttons itself, see picture_button_ids). An element that does carry data-extid, but whose id (either one) is
    not a positive integer, is logged and skipped as unusable; if such elements exist and none of them is usable, the
    page itself has changed and that raises ComexioDataError instead of reporting no Doorstation.
    """
    found: dict[int, Doorstation] = {}
    unusable = 0
    without_camera = 0
    without_extid = 0
    for attributes in elements:
        if not attributes.get(CAMERA_ID_ATTRIBUTE):
            without_camera += 1
            continue
        if EXT_ID_ATTRIBUTE not in attributes:
            without_extid += 1
            continue
        doorstation = _doorstation_of(attributes)
        if doorstation is None:
            unusable += 1
            continue
        found[doorstation.ext_id] = _merged(found.get(doorstation.ext_id), doorstation)
    if without_camera:
        _LOGGER.debug("Skipped %d Doorstation elements with an empty camera id", without_camera)
    if without_extid:
        _LOGGER.debug("Skipped %d Doorstation elements without a data-extid at all", without_extid)
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


def first_jpeg_frame(data: bytes | bytearray, *, require_frame_header: bool = False) -> bytes | None:
    """The first complete JPEG image in data (e.g. the start of an MJPEG stream), None if it is not complete yet.

    The image runs from its SOI marker to the EOI marker that ends it: the segments before a scan are skipped by
    their length fields, so an EOI inside them (an embedded thumbnail) does not end the image early. Data before
    the first SOI (a multipart boundary and its headers) is skipped. A marker that is missing where the structure of
    a JPEG needs one, a segment shorter than its length field, a stuffed byte or second SOI between the segments, an
    SOS header whose length does not fit its 1 to 4 scan components, a scan without entropy-coded data (nothing but
    restart markers counts as none), or an EOI before the first scan (an image without picture data) raises
    ComexioDataError. With require_frame_header a scan before any start-of-frame marker (SOF0 to SOF15: the size and
    components a decoder needs) raises as well; a stream is read without it, a still image that is handed on as a
    picture with it.
    """
    return JpegFrameReader(require_frame_header=require_frame_header).frame(data)


class JpegFrameReader:
    """Finds the first complete JPEG image in a buffer that grows between calls, like first_jpeg_frame.

    Each call goes on where the previous one stopped, so a stream read chunk by chunk is walked once rather than once
    per chunk. Pass the same buffer to every call and only ever append to it; once a call returned a frame or raised,
    the reader is done. With require_frame_header a scan that comes before a start-of-frame marker raises.
    """

    __slots__ = (
        "_frame",
        "_framed",
        "_pos",
        "_require_frame_header",
        "_scan_from",
        "_scan_pos",
        "_scanned",
        "_start",
    )

    def __init__(self, *, require_frame_header: bool = False) -> None:
        self._require_frame_header = require_frame_header
        self._framed = False  # a start-of-frame marker has been passed
        self._frame: _FrameHeader | None = None  # the checked frame header (only with require_frame_header)
        self._start = -1  # offset of the SOI, -1 until it arrived
        self._pos = 0  # where the walk goes on: the search for the SOI, then the next marker
        self._scan_from: int | None = None  # inside a scan: where its entropy-coded data starts
        self._scan_pos = 0  # inside a scan: where the search for its end goes on
        self._scanned = False

    def frame(self, data: bytes | bytearray) -> bytes | None:
        """The first complete JPEG image in data, None if it is not complete yet; see first_jpeg_frame."""
        if self._start < 0 and not self._found_start(data):
            return None
        while True:
            if (scan_from := self._scan_from) is not None and not self._left_scan(data, scan_from):
                return None
            code_at = _marker_code_at(data, self._pos)
            if code_at is None:
                # data ends in 0xFF fill bytes (or before the marker): the next call goes on at the last of them.
                self._pos = max(self._pos, len(data) - 1)
                return None
            if data[code_at] == _EOI_CODE:
                if not self._scanned:
                    raise ComexioDataError(f"Camera frame ends at offset {code_at} before its first scan")
                return bytes(data[self._start : code_at + 1])
            if not self._passed_segment(data, code_at):
                return None

    def _found_start(self, data: bytes | bytearray) -> bool:
        """Whether the SOI arrived; the search starts one byte before where the last one ended (a split marker)."""
        start = data.find(_SOI, max(self._pos - 1, 0))
        if start < 0:
            self._pos = len(data)
            return False
        self._start = start
        self._pos = start + len(_SOI)
        return True

    def _left_scan(self, data: bytes | bytearray, scan_from: int) -> bool:
        """Whether data holds the marker that ends the current scan; a scan without entropy-coded data raises."""
        end, self._scan_pos = _scan_end(data, self._scan_pos)
        if end is None:
            return False
        # A search resumed inside fill bytes finds the last of them; the marker starts at the first one.
        while end > scan_from and data[end - 1] == _MARKER_PREFIX:
            end -= 1
        if _MARKERS_ONLY.fullmatch(data, scan_from, end):
            raise ComexioDataError(f"Camera frame has a scan without entropy-coded data at offset {scan_from}")
        self._scan_from = None
        self._pos = end
        return True

    def _passed_segment(self, data: bytes | bytearray, code_at: int) -> bool:
        """Whether data holds the whole marker segment whose code is at code_at; the walk then goes on after it."""
        code = data[code_at]
        if code in _NO_SEGMENT_CODES:
            raise ComexioDataError(f"Camera frame has the unexpected JPEG marker 0x{code:02X} at offset {code_at}")
        if code == _SOS_CODE and self._require_frame_header and not self._framed:
            raise ComexioDataError(f"Camera frame has a scan at offset {code_at} before its frame header")
        end = code_at + 1 if code in _STANDALONE_CODES else _segment_end(data, code_at + 1)
        if end is None:
            return False
        if self._require_frame_header and code in _SOF_CODES:
            if end > len(data):
                return False  # the frame header has not arrived whole yet: the next call checks it
            self._frame = _check_sof_header(data, code_at, end)
        self._framed = self._framed or code in _SOF_CODES
        if code == _SOS_CODE and not self._entered_scan(data, code_at, end):
            return False
        self._pos = end
        return True

    def _entered_scan(self, data: bytes | bytearray, code_at: int, end: int) -> bool:
        """Whether the SOS header ending at end has arrived far enough to be checked; the scan then begins at end."""
        if code_at + _SOS_COMPONENTS_OFFSET >= len(data):
            return False  # the component count has not arrived yet
        if self._frame is not None and end > len(data):
            return False  # the scan's header has not arrived whole yet: the next call checks it
        _check_sos_header(data, code_at, end, self._frame)
        self._scanned = True
        self._scan_from = self._scan_pos = end
        return True


def _segment_end(data: bytes | bytearray, pos: int) -> int | None:
    """Offset after the segment whose length field starts at pos, None if data ends before it."""
    if pos + 2 > len(data):
        return None
    length = int.from_bytes(data[pos : pos + 2])
    if length < _MIN_SEGMENT_LENGTH:
        raise ComexioDataError(f"Camera frame has a JPEG segment of length {length}")
    return pos + length


class _FrameHeader(NamedTuple):
    """What a scan is checked against: the marker code (the process) and the component ids of its frame header."""

    code: int
    ids: frozenset[int]


def _check_sos_header(data: bytes | bytearray, code_at: int, end: int, frame: _FrameHeader | None) -> None:
    """Raise ComexioDataError unless the SOS segment from code_at to end fits its 1 to 4 components.

    With its frame header each scan component must name a different component of it (T.81, B.2.3), and Ss, Se and
    Ah/Al must be values the process of the frame allows.
    """
    components = data[code_at + _SOS_COMPONENTS_OFFSET]
    length = end - code_at - 1  # the length field, which counts itself but not the code
    if (
        not 1 <= components <= _MAX_SCAN_COMPONENTS
        or length != _SOS_FIXED_LENGTH + _SOS_BYTES_PER_COMPONENT * components
    ):
        raise ComexioDataError(f"Camera frame has a malformed SOS header (length {length}) at offset {code_at}")
    if frame is None:
        return
    first = code_at + _SOS_COMPONENTS_OFFSET + 1
    after_components = first + _SOS_BYTES_PER_COMPONENT * components
    selectors = [data[first + at] for at in range(0, _SOS_BYTES_PER_COMPONENT * components, _SOS_BYTES_PER_COMPONENT)]
    if len(set(selectors)) != len(selectors) or not frame.ids.issuperset(selectors):
        raise ComexioDataError(f"Camera frame has a scan whose components do not fit its frame at offset {code_at}")
    spectral_start, spectral_end, approximation = data[after_components : after_components + 3]
    if not _scan_parameters_are_sound(frame.code, components, spectral_start, spectral_end, approximation):
        raise ComexioDataError(f"Camera frame has a scan with parameters its frame cannot have at offset {code_at}")


def _scan_parameters_are_sound(code: int, components: int, ss: int, se: int, approximation: int) -> bool:
    """Whether Ss, Se and the Ah/Al byte are values the process of the SOF marker allows (see the constants)."""
    high, low = divmod(approximation, 16)
    if code in _LOSSLESS_CODES:
        return ss <= _MAX_PREDICTOR and se == 0 and high == 0
    if code not in _PROGRESSIVE_CODES:
        return (ss, se, approximation) == (0, _LAST_COEFFICIENT, 0)
    in_band = ss <= se <= _LAST_COEFFICIENT and (ss == 0) == (se == 0)  # a DC scan is Ss = Se = 0
    refines_next_bit = high in {0, low + 1}
    return in_band and (ss == 0 or components == 1) and refines_next_bit and max(high, low) <= _MAX_SUCCESSIVE_SHIFT


def _check_sof_header(data: bytes | bytearray, code_at: int, end: int) -> _FrameHeader:
    """The code and component ids of the SOF segment from code_at to end; raises ComexioDataError unless readable."""
    length = end - code_at - 1  # the length field, which counts itself but not the code
    if length < _SOF_FIXED_LENGTH or not _frame_header_is_sound(data, code_at, length):
        raise ComexioDataError(f"Camera frame has a malformed frame header (length {length}) at offset {code_at}")
    return _FrameHeader(data[code_at], frozenset(_sof_component_ids(data, code_at)))


def _sof_component_ids(data: bytes | bytearray, code_at: int) -> list[int]:
    """The component ids, in order, of the SOF header at code_at whose length was checked."""
    components = data[code_at + _SOF_COMPONENTS_OFFSET]
    first = code_at + _SOF_FIRST_COMPONENT_OFFSET
    return [data[first + at] for at in range(0, _SOF_BYTES_PER_COMPONENT * components, _SOF_BYTES_PER_COMPONENT)]


def _precision_is_sound(code: int, precision: int) -> bool:
    """Whether the sample precision is one the process of the SOF marker allows (T.81, Table B.2)."""
    if code == _BASELINE_CODE:
        return precision == _BASELINE_PRECISION
    if code in _LOSSLESS_CODES:
        return precision in _LOSSLESS_PRECISIONS
    return precision in _DCT_PRECISIONS


def _frame_header_is_sound(data: bytes | bytearray, code_at: int, length: int) -> bool:
    """Whether the SOF header at code_at (length bytes after the code, at least the fixed part) is well-formed."""
    components = data[code_at + _SOF_COMPONENTS_OFFSET]
    if components == 0:  # the byte holds 255 at most, which the length check below then matches
        return False
    if length != _SOF_FIXED_LENGTH + _SOF_BYTES_PER_COMPONENT * components:
        return False
    precision = data[code_at + _SOF_PRECISION_OFFSET]
    height = int.from_bytes(data[code_at + _SOF_HEIGHT_OFFSET : code_at + _SOF_HEIGHT_OFFSET + 2])
    width = int.from_bytes(data[code_at + _SOF_WIDTH_OFFSET : code_at + _SOF_WIDTH_OFFSET + 2])
    if not (_precision_is_sound(data[code_at], precision) and height >= 1 and width >= 1):
        return False
    ids = _sof_component_ids(data, code_at)
    if len(set(ids)) != len(ids):  # each component has a label of its own (T.81, B.2.2)
        return False
    first = code_at + _SOF_FIRST_COMPONENT_OFFSET
    return all(
        _component_is_sound(data[first + at : first + at + _SOF_BYTES_PER_COMPONENT])
        for at in range(0, _SOF_BYTES_PER_COMPONENT * components, _SOF_BYTES_PER_COMPONENT)
    )


def _component_is_sound(component: bytes | bytearray) -> bool:
    """Whether a component of an SOF header (id, sampling factors, quantization table) holds usable values."""
    horizontal, vertical = divmod(component[1], 16)
    return (
        1 <= horizontal <= _MAX_SAMPLING_FACTOR
        and 1 <= vertical <= _MAX_SAMPLING_FACTOR
        and component[2] <= _MAX_QUANTIZATION_TABLE
    )


def _marker_code_at(data: bytes | bytearray, pos: int) -> int | None:
    """Offset of the code of the marker at pos (fill bytes skipped), None if data ends before it."""
    if pos >= len(data):
        return None
    if data[pos] != _MARKER_PREFIX:
        raise ComexioDataError(f"Camera frame has no JPEG marker at offset {pos}")
    while pos < len(data) and data[pos] == _MARKER_PREFIX:
        pos += 1
    return pos if pos < len(data) else None


def _scan_end(data: bytes | bytearray, pos: int) -> tuple[int | None, int]:
    """Offset of the marker that ends the entropy-coded data from pos (None if data ends first), and where to go on.

    A later search for the same end goes on at the second offset, so the scan is not searched again from its start.
    It never lies before pos: pos may be past the end of data (an SOS segment that has not fully arrived yet).
    """
    while True:
        found = data.find(_MARKER_PREFIX, pos)
        if found < 0:
            return None, max(pos, len(data))
        pos = found
        # Further 0xFF bytes are fill bytes before the code (T.81, B.1.1.2), also before an RST inside the scan.
        code_at = pos + 1
        while code_at < len(data) and data[code_at] == _MARKER_PREFIX:
            code_at += 1
        if code_at >= len(data):
            return None, len(data) - 1  # data ends in fill bytes: go on at the last of them, not the first
        if data[code_at] not in _IN_SCAN_CODES:
            return pos, pos
        pos = code_at + 1
