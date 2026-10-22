"""SECS-II (SEMI E5) data item encoding / decoding.

Every SECS-II item is encoded as:

    +-------------+------------------+----------------+
    | format byte | length (1-3 B)   | payload        |
    +-------------+------------------+----------------+

The format byte holds a 6-bit format code (upper bits) and a 2-bit count of
how many length bytes follow. For a List the "length" is the number of child
items, for every other type it is the payload size in bytes.

Only the item types used by this project are implemented, but adding one is a
single entry in ``_NUMERIC``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, List, Tuple, Union


class Fmt(IntEnum):
    """SECS-II format codes (octal, as written in SEMI E5)."""

    L = 0o00
    B = 0o10
    BOOLEAN = 0o11
    A = 0o20
    I8 = 0o30
    I1 = 0o31
    I2 = 0o32
    I4 = 0o34
    F8 = 0o40
    F4 = 0o44
    U8 = 0o50
    U1 = 0o51
    U2 = 0o52
    U4 = 0o54


# format -> (struct code, byte size)
_NUMERIC = {
    Fmt.I1: ("b", 1),
    Fmt.I2: ("h", 2),
    Fmt.I4: ("i", 4),
    Fmt.I8: ("q", 8),
    Fmt.U1: ("B", 1),
    Fmt.U2: ("H", 2),
    Fmt.U4: ("I", 4),
    Fmt.U8: ("Q", 8),
    Fmt.F4: ("f", 4),
    Fmt.F8: ("d", 8),
}

MAX_LENGTH = 0xFFFFFF


class SecsDecodeError(ValueError):
    """Raised when a byte stream is not a valid SECS-II item."""


@dataclass
class Item:
    """A SECS-II data item. ``value`` is a list of children for ``Fmt.L``,
    ``str`` for ``A``, ``bytes`` for ``B`` and a list of numbers / bools for
    numeric and boolean formats (SECS-II items are always arrays)."""

    fmt: Fmt
    value: Any = field(default_factory=list)

    # ---- convenience constructors -------------------------------------------------
    @staticmethod
    def L(*children: Item) -> Item:  # noqa: N802 - mirrors SML notation
        return Item(Fmt.L, list(children))

    @staticmethod
    def A(text: str) -> Item:  # noqa: N802
        return Item(Fmt.A, text)

    @staticmethod
    def B(data: Union[bytes, int]) -> Item:  # noqa: N802
        return Item(Fmt.B, bytes([data]) if isinstance(data, int) else data)

    @staticmethod
    def BOOL(*values: bool) -> Item:  # noqa: N802
        return Item(Fmt.BOOLEAN, list(values))

    @staticmethod
    def U4(*values: int) -> Item:  # noqa: N802
        return Item(Fmt.U4, list(values))

    @staticmethod
    def U1(*values: int) -> Item:  # noqa: N802
        return Item(Fmt.U1, list(values))

    @staticmethod
    def I4(*values: int) -> Item:  # noqa: N802
        return Item(Fmt.I4, list(values))

    @staticmethod
    def F4(*values: float) -> Item:  # noqa: N802
        return Item(Fmt.F4, list(values))

    @staticmethod
    def F8(*values: float) -> Item:  # noqa: N802
        return Item(Fmt.F8, list(values))

    # ---- accessors ----------------------------------------------------------------
    def __getitem__(self, idx: int) -> Item:
        if self.fmt is not Fmt.L:
            raise TypeError(f"{self.fmt.name} item is not a list")
        return self.value[idx]

    def __len__(self) -> int:
        return len(self.value)

    @property
    def scalar(self) -> Any:
        """First value of a numeric/boolean array, or the text/bytes itself."""
        if self.fmt in (Fmt.A, Fmt.B):
            return self.value
        if self.fmt is Fmt.L:
            raise TypeError("list item has no scalar value")
        return self.value[0] if self.value else None

    def to_python(self) -> Any:
        """Recursively convert to plain Python (lists, str, numbers)."""
        if self.fmt is Fmt.L:
            return [c.to_python() for c in self.value]
        if self.fmt in (Fmt.A, Fmt.B):
            return self.value
        return self.value[0] if len(self.value) == 1 else list(self.value)

    def to_sml(self, indent: int = 0) -> str:
        """Render in SML (SECS Message Language), the notation used in tool
        vendor manuals and SECS log viewers."""
        pad = "  " * indent
        if self.fmt is Fmt.L:
            if not self.value:
                return f"{pad}<L[0]>"
            inner = "\n".join(c.to_sml(indent + 1) for c in self.value)
            return f"{pad}<L[{len(self.value)}]\n{inner}\n{pad}>"
        if self.fmt is Fmt.A:
            return f'{pad}<A "{self.value}">'
        if self.fmt is Fmt.B:
            return f"{pad}<B {' '.join(f'0x{b:02X}' for b in self.value)}>"
        if self.fmt is Fmt.BOOLEAN:
            vals = " ".join("T" if v else "F" for v in self.value)
            return f"{pad}<BOOLEAN {vals}>"
        if self.fmt in (Fmt.F4, Fmt.F8):
            vals = " ".join(f"{v:.4g}" for v in self.value)
        else:
            vals = " ".join(str(v) for v in self.value)
        return f"{pad}<{self.fmt.name} {vals}>"


# ------------------------------------------------------------------------------------
# Encoding
# ------------------------------------------------------------------------------------
def _header(fmt: Fmt, length: int) -> bytes:
    if length > MAX_LENGTH:
        raise ValueError(f"item length {length} exceeds SECS-II maximum")
    n = 1 if length <= 0xFF else 2 if length <= 0xFFFF else 3
    return bytes([(fmt << 2) | n]) + length.to_bytes(n, "big")


def encode(item: Item) -> bytes:
    fmt = item.fmt
    if fmt is Fmt.L:
        return _header(fmt, len(item.value)) + b"".join(encode(c) for c in item.value)
    if fmt is Fmt.A:
        payload = item.value.encode("ascii")
    elif fmt is Fmt.B:
        payload = bytes(item.value)
    elif fmt is Fmt.BOOLEAN:
        payload = bytes(1 if v else 0 for v in item.value)
    elif fmt in _NUMERIC:
        code, _ = _NUMERIC[fmt]
        payload = struct.pack(f">{len(item.value)}{code}", *item.value)
    else:  # pragma: no cover - enum is exhaustive
        raise ValueError(f"unsupported format {fmt}")
    return _header(fmt, len(payload)) + payload


# ------------------------------------------------------------------------------------
# Decoding
# ------------------------------------------------------------------------------------
def _decode_at(data: bytes, pos: int) -> Tuple[Item, int]:
    if pos >= len(data):
        raise SecsDecodeError("unexpected end of data")
    fb = data[pos]
    n_len = fb & 0b11
    if n_len == 0:
        raise SecsDecodeError("format byte declares zero length bytes")
    try:
        fmt = Fmt(fb >> 2)
    except ValueError as exc:
        raise SecsDecodeError(f"unknown format code 0o{fb >> 2:o}") from exc
    pos += 1
    if pos + n_len > len(data):
        raise SecsDecodeError("truncated length field")
    length = int.from_bytes(data[pos : pos + n_len], "big")
    pos += n_len

    if fmt is Fmt.L:
        children: List[Item] = []
        for _ in range(length):
            child, pos = _decode_at(data, pos)
            children.append(child)
        return Item(fmt, children), pos

    end = pos + length
    if end > len(data):
        raise SecsDecodeError(f"{fmt.name} payload truncated")
    raw = data[pos:end]
    if fmt is Fmt.A:
        return Item(fmt, raw.decode("ascii", errors="replace")), end
    if fmt is Fmt.B:
        return Item(fmt, raw), end
    if fmt is Fmt.BOOLEAN:
        return Item(fmt, [b != 0 for b in raw]), end
    code, size = _NUMERIC[fmt]
    if length % size:
        raise SecsDecodeError(f"{fmt.name} payload length {length} not a multiple of {size}")
    return Item(fmt, list(struct.unpack(f">{length // size}{code}", raw))), end


def decode(data: bytes) -> Item:
    item, pos = _decode_at(data, 0)
    if pos != len(data):
        raise SecsDecodeError(f"{len(data) - pos} trailing bytes after item")
    return item
