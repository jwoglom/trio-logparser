"""Message templating (for grouping) and masking (for display)."""

from __future__ import annotations

import re

_TEMPLATE = [
    (re.compile(r"cargo=[0-9a-fA-F]*"), "cargo=…"),
    (re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"), "<UUID>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<HEX>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?: ?[+-]\d{4}|Z)?"), "<TS>"),
    (re.compile(r"Tandem Mobi \d+|t:slim X2 \d+"), "<PUMP>"),
    (re.compile(r"(?<![A-Za-z])-?\d+(?:\.\d+)?"), "<N>"),
]

# Identifiers that must not appear in shared output: pump BLE names (contain the serial
# suffix), CGM transmitter IDs, pump serials, emails.
_MASK = [
    (re.compile(r"(Tandem Mobi|t:slim X2) \d+"), r"\1 <SN>"),
    (re.compile(r"(?i)\b(transmitter(?:ID| id)?[=:]\s*)\w+"), r"\1<TX>"),
    (re.compile(r"(?i)\b(serial(?:Number)?[=:]\s*)\w+"), r"\1<SN>"),
    (re.compile(r"(\b\w+Serial\w*=)\w+"), r"\1<SN>"),  # pcbSerialNumberA=
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<EMAIL>"),
    # A TandemKit dose's raw (``0101…``) embeds the pump serial in bytes 2-5 but is left as Trio's
    # export prints it, so findings and ``show`` use the same ids as the log.
]
# A message's cargo is its raw bytes and stays visible: it is how a decoder is checked. The one
# exception is a message with a serial field (PumpVersionBResponse): its cargo encodes that serial.
_SERIAL_FIELD = re.compile(r"(?i)serial\w*=")
_CARGO = re.compile(r"cargo=[0-9a-fA-F]+")
CARGO_MASKED = "<SN>…"


def has_serial_field(names) -> bool:
    """Whether any of a message's field names is a serial number (so its cargo carries one)."""
    return any("serial" in str(k).lower() for k in names)


def template(msg: str, limit: int = 200) -> str:
    msg = msg[:limit * 2]
    for rx, sub in _TEMPLATE:
        msg = rx.sub(sub, msg)
    return msg[:limit]


def mask(msg: str) -> str:
    for rx, sub in _MASK:
        msg = rx.sub(sub, msg)
    if _SERIAL_FIELD.search(msg):
        msg = _CARGO.sub(f"cargo={CARGO_MASKED}", msg)
    return msg
