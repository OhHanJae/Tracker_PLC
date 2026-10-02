"""XGT direct-variable address helpers.

Continuous XGT requests use BYTE variables.  For D-word shorthand, the word
index is converted to the corresponding byte index: D1000 -> %DB2000.
"""

from __future__ import annotations

import re

from .errors import ConfigurationError

_DIRECT_BYTE_RE = re.compile(r"^%([A-Z])B(\d+)$")
_DIRECT_WORD_RE = re.compile(r"^%([A-Z])W(\d+)$")
_D_WORD_RE = re.compile(r"^D(\d+)$")
_BYTE_SHORT_RE = re.compile(r"^([A-Z])B(\d+)$")


def normalize_continuous_byte_address(value: str) -> str:
    """Return a canonical XGT BYTE direct-variable address.

    Accepted examples:
      - ``%DB2000``: already a byte address.
      - ``DB2000``: leading percent is added.
      - ``D1000`` or ``%DW1000``: word address converted to ``%DB2000``.

    Other device areas should be entered explicitly as BYTE addresses, such as
    ``%MB0``.  This avoids silently applying the wrong device-unit conversion.
    """

    if not isinstance(value, str):
        raise ConfigurationError("PLC address must be a string")
    address = value.strip().upper().replace(" ", "")

    match = _D_WORD_RE.fullmatch(address)
    if match:
        address = f"%DB{int(match.group(1)) * 2}"
    else:
        match = _DIRECT_WORD_RE.fullmatch(address)
        if match:
            device, index = match.groups()
            if device != "D":
                raise ConfigurationError(
                    "Only D-word shorthand is auto-converted. "
                    "Enter other areas as a BYTE address such as %MB0."
                )
            address = f"%DB{int(index) * 2}"
        else:
            match = _BYTE_SHORT_RE.fullmatch(address)
            if match:
                address = f"%{match.group(1)}B{int(match.group(2))}"

    match = _DIRECT_BYTE_RE.fullmatch(address)
    if not match:
        raise ConfigurationError(
            f"Invalid continuous BYTE address: {value!r}. "
            "Use %DB2000, DB2000, or D1000 format."
        )
    if len(address.encode("ascii")) > 16:
        raise ConfigurationError("XGT direct-variable names are limited to 16 ASCII bytes")
    return address


def address_summary(value: str, byte_count: int) -> str:
    """Return a concise human-readable address summary."""

    normalized = normalize_continuous_byte_address(value)
    return f"{normalized}, {byte_count} bytes ({byte_count / 2:g} words)"

