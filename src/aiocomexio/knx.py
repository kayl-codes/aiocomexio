"""KNX datapoint type (DPT) tables and the K-element -> DPT resolution chain.

Keys are the official KNX Association datapoint type numbering (KnxBaseTypeId, KnxSubId),
e.g. (9, 7) = DPT9.007 Humidity. Comexio's own $FubModules["11"]/$IOTypesBinary catalogs
carry no usable value range for KNX objects (min/max come back as a 0/0 placeholder), so the
real range is resolved per K-element via the $KnxPoints -> $KnxDevices -> $KnxDpt chain
scraped from the KNX admin page (/admin/knx_one_wire/knx/), see resolve_knx_dpt.
"""

from collections.abc import Mapping
from typing import Any

# Analog value range, unit and native step per DPT.
# Composite DPTs (DPT3.x control+step, DPT18.001 control+scene number) split into two Comexio
# Points from one Device; only each composite's analog component is listed here.
# Deliberately excluded: DPT1 (binary), DPT10/11/19 (Time/Date/DateTime — Comexio refuses to
# create K-elements for these), and DPT14 (4-byte float — no tighter standard range than the
# IEEE754 span).
# Step: raw N-octet integer DPTs use step=1; the KNX "scaled" 1-byte types (DPT5.1 Scaling,
# DPT5.3 Angle) map a raw byte 0-255 onto min..max, so their step is (max-min)/255; DPT9
# (2-octet float) uses a flat 0.1.
# Format: {(base_type_id, sub_id): (min, max, unit, step)}
KNX_DPT_ANALOG_RANGES: dict[tuple[int, int], tuple[float, float, str, float]] = {
    # DPT3 - 1-Bit control + 3-Bit step code (Dimming/Blinds), analog half is the step value.
    (3, 7): (0, 7, "", 1),
    (3, 8): (0, 7, "", 1),
    # DPT5 - 8-Bit unsigned value.
    (5, 1): (0, 100, "%", 100 / 255),  # Scaling
    (5, 3): (0, 360, "°", 360 / 255),  # Angle
    (5, 4): (0, 255, "%", 1),  # Percent_U8
    (5, 5): (0, 255, "", 1),  # DecimalFactor
    (5, 6): (0, 254, "", 1),  # Tariff
    (5, 10): (0, 255, "", 1),  # Value_1_Ucount (pulse counter)
    # DPT6 - 8-Bit signed value.
    (6, 1): (-128, 127, "%", 1),  # Percent_V8
    (6, 10): (-128, 127, "", 1),  # Value_1_Count
    # DPT7 - 2-Octet unsigned value.
    (7, 1): (0, 65535, "", 1),
    (7, 2): (0, 65535, "ms", 1),
    (7, 3): (0, 65535, "10ms", 1),
    (7, 4): (0, 65535, "100ms", 1),
    (7, 5): (0, 65535, "s", 1),
    (7, 6): (0, 65535, "min", 1),
    (7, 7): (0, 65535, "h", 1),
    (7, 10): (0, 65535, "", 1),
    # DPT8 - 2-Octet signed value.
    (8, 1): (-32768, 32767, "", 1),
    (8, 2): (-32768, 32767, "ms", 1),
    (8, 3): (-32768, 32767, "10ms", 1),
    (8, 4): (-32768, 32767, "100ms", 1),
    (8, 5): (-32768, 32767, "s", 1),
    (8, 6): (-32768, 32767, "min", 1),
    (8, 7): (-32768, 32767, "h", 1),
    (8, 10): (-32768, 32767, "%", 1),
    # DPT9 - 2-Octet float value (KNX floating-point-16, format range -671088.64..670760.96).
    (9, 1): (-273, 670760, "°C", 0.1),  # Value_Temp
    (9, 2): (-670760, 670760, "K", 0.1),  # Value_Tempd (temperature difference)
    (9, 4): (0, 670760, "lx", 0.1),  # Value_Lux
    (9, 5): (0, 670760, "m/s", 0.1),  # Value_Wsp
    (9, 6): (0, 670760, "Pa", 0.1),  # Value_Pres
    (9, 7): (0, 100, "%", 0.1),  # Value_Humidity (physically bounded)
    (9, 8): (0, 670760, "ppm", 0.1),  # Value_AirQuality
    (9, 20): (-670760, 670760, "V", 0.1),  # Value_Volt
    (9, 21): (-670760, 670760, "mA", 0.1),  # Value_Curr
    (9, 24): (-670760, 670760, "kW", 0.1),  # Power
    # DPT12 - 4-Octet unsigned value.
    (12, 1): (0, 4294967295, "", 1),
    # DPT13 - 4-Octet signed value.
    (13, 1): (-2147483648, 2147483647, "", 1),
    (13, 10): (-2147483648, 2147483647, "Wh", 1),
    (13, 11): (-2147483648, 2147483647, "VAh", 1),
    (13, 12): (-2147483648, 2147483647, "VARh", 1),
    (13, 13): (-2147483648, 2147483647, "kWh", 1),
    (13, 14): (-2147483648, 2147483647, "kVAh", 1),
    (13, 15): (-2147483648, 2147483647, "kVARh", 1),
    (13, 100): (-2147483648, 2147483647, "s", 1),
    # DPT17 - Scene number.
    (17, 1): (0, 63, "", 1),
    # DPT18 - Scene control (1-Bit learn/execute + 6-Bit scene number); analog half is the
    # scene-number component.
    (18, 1): (0, 63, "", 1),
}


_DC_DURATION = "duration"

# Device class (Home Assistant's plain NumberDeviceClass/SensorDeviceClass value string) for
# the analog DPTs whose unit exactly matches one of that device class's allowed units.
# Deliberately excludes every "%"-unit DPT except 9.7 Humidity (a shared "%" unit does not
# imply a shared meaning), the composite control DPTs (3.7/3.8), the *Ah/*ARh energy variants
# and the 10ms/100ms DPT7/8 sub-types. Missing from this table == no device class.
KNX_DPT_DEVICE_CLASS: dict[tuple[int, int], str] = {
    (7, 2): _DC_DURATION,  # Value_2_Ucount, ms
    (7, 5): _DC_DURATION,  # s
    (7, 6): _DC_DURATION,  # min
    (7, 7): _DC_DURATION,  # h
    (8, 2): _DC_DURATION,  # ms
    (8, 5): _DC_DURATION,  # s
    (8, 6): _DC_DURATION,  # min
    (8, 7): _DC_DURATION,  # h
    (9, 1): "temperature",  # Value_Temp
    (9, 2): "temperature_delta",  # Value_Tempd
    (9, 4): "illuminance",  # Value_Lux
    (9, 5): "wind_speed",  # Value_Wsp
    (9, 6): "pressure",  # Value_Pres
    (9, 7): "humidity",  # Value_Humidity
    (9, 20): "voltage",  # Value_Volt
    (9, 21): "current",  # Value_Curr
    (9, 24): "power",  # Power
    (13, 10): "energy",  # Wh
    (13, 13): "energy",  # kWh
    (13, 100): _DC_DURATION,  # LongDeltaTimeSec, s
}


# DPT1.x (binary) device class (Home Assistant's plain BinarySensorDeviceClass value string).
# DPT1.019 ("door/window") can't be told apart from the DPT alone, so "door" is a best-effort
# default. Missing from this table == no device class (plain on/off).
KNX_DPT_DIGITAL_DEVICE_CLASS: dict[tuple[int, int], str] = {
    (1, 5): "problem",  # Alarm
    (1, 18): "occupancy",  # Anwesenheit
    (1, 19): "door",  # Tür/Fenster (best-effort, see comment above)
}


# The remaining DPT1.x subtypes (switch/bool/enable/ramp/binary value) are physically
# ambivalent: KNX uses the identical DPT for a real toggle switch, a momentary push button and
# a pure status readback alike — only the installer knows the real wiring. config.parse_config
# flags such digital items with dpt_ambiguous=True instead of classifying them.
KNX_DPT_DIGITAL_AMBIGUOUS: set[tuple[int, int]] = {(1, 1), (1, 2), (1, 3), (1, 4), (1, 6)}


# DPT3.x (Dimmer 3.007 / Blinds 3.008) composite objects: Comexio splits each into two
# K-elements sharing one KnxDeviceId — a digital control bit (direction) and an analog 3-bit
# step code (0=break, 1-7=move). Maps the DPT to the composite entity domain.
KNX_DPT3_COMPOSITE_DOMAIN: dict[tuple[int, int], str] = {
    (3, 7): "light",
    (3, 8): "cover",
}

# KNX Association DPT3 control-bit encoding (public standard, not Comexio-specific).
KNX_DPT3_COVER_DIRECTION_UP = 0
KNX_DPT3_COVER_DIRECTION_DOWN = 1
KNX_DPT3_LIGHT_DIRECTION_DECREASE = 0
KNX_DPT3_LIGHT_DIRECTION_INCREASE = 1
KNX_DPT3_STEPCODE_BREAK = 0
KNX_DPT3_STEPCODE_MOVE = 1


def resolve_knx_dpt(knx_dpt_catalog: Mapping[str, Any], k_id: str) -> tuple[int, int] | None:
    """Resolve a K-element's real KNX DPT (KnxBaseTypeId, KnxSubId) via Point -> Device -> Dpt.

    knx_dpt_catalog is the scraped KNX admin page ($KnxPoints/$KnxDevices/$KnxDpt, each
    id-keyed — a K-element's own id IS its $KnxPoints entry's id). Returns None if any link
    in the chain is missing or malformed, so callers can fall back to a generic range rather
    than crash on an unexpected shape.

    Unlike $FubModules groups, these three never need the JSON-array-vs-object handling: their
    ids are 1-based with gaps, so the "keys are exactly 0..N-1" shape PHP's json_encode needs
    to emit an array can't occur here.
    """
    points = knx_dpt_catalog.get("KnxPoints")
    devices = knx_dpt_catalog.get("KnxDevices")
    dpts = knx_dpt_catalog.get("KnxDpt")
    if not isinstance(points, dict) or not isinstance(devices, dict) or not isinstance(dpts, dict):
        return None

    point = points.get(k_id)
    if not isinstance(point, dict):
        return None
    device = devices.get(str(point.get("KnxDeviceId")))
    if not isinstance(device, dict):
        return None
    dpt = dpts.get(str(device.get("KnxDptId")))
    if not isinstance(dpt, dict):
        return None

    base_type_id, sub_id = dpt.get("KnxBaseTypeId"), dpt.get("KnxSubId")
    if not isinstance(base_type_id, int) or not isinstance(sub_id, int):
        return None
    return base_type_id, sub_id
