"""Build small LevelDB tables and Claude-like IndexedDB records for tests."""
from __future__ import annotations


def v8(value: str) -> bytes:
    raw = value.encode()
    assert len(raw) < 0x80
    return b'"' + bytes([len(raw)]) + raw


def current_use(call: str, name: str, *, skill: str | None = None, when: str = "2026-10-13T07:20:01.000Z") -> bytes:
    tool_input = b"o" + (v8("skill") + v8(skill) if skill else b"") + b"{" + (b"\x01" if skill else b"\x00")
    return (
        v8("start_timestamp") + v8(when) + v8("flags") + b"{\x02"
        + b"o" + v8("type") + v8("tool_use") + v8("id") + v8(call) + v8("name") + v8(name)
        + v8("tool_display_name") + v8("Display Name") + v8("input") + tool_input
        + v8("message") + v8("user visible text") + b"{\x06"
    )


def current_result(call: str, name: str, *, failed: bool = False) -> bytes:
    return (
        b"o" + v8("type") + v8("tool_result") + v8("tool_use_id") + v8(call) + v8("name") + v8(name)
        + v8("is_error") + (b"T" if failed else b"F") + v8("content") + b"A\x01" + v8("private result") + b"$\x00\x01{\x05"
    )


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def snappy_compress(data: bytes) -> bytes:
    """Small but real Snappy encoder: literals plus 2-byte-offset copies."""
    out = bytearray(varint(len(data)))
    table: dict[bytes, int] = {}
    literal_start = position = 0

    def literal(end: int) -> None:
        start = literal_start
        while start < end:
            chunk = data[start:min(end, start + 65536)]
            length = len(chunk) - 1
            if length < 60:
                out.append(length << 2)
            elif length < 256:
                out.extend([60 << 2, length])
            else:
                out.extend([61 << 2, length & 0xFF, length >> 8])
            out.extend(chunk)
            start += len(chunk)

    while position + 4 <= len(data):
        key = data[position:position + 4]
        candidate = table.get(key)
        table[key] = position
        if candidate is not None and 0 < position - candidate < 65536:
            length = 4
            while position + length < len(data) and length < 64 and data[candidate + length] == data[position + length]:
                length += 1
            literal(position)
            offset = position - candidate
            out.append(((length - 1) << 2) | 2)
            out.extend(offset.to_bytes(2, "little"))
            position += length
            literal_start = position
            continue
        position += 1
    literal(len(data))
    return bytes(out)


def block(entries: list[tuple[bytes, bytes]]) -> bytes:
    body = bytearray()
    for key, value in entries:  # no prefix sharing, one restart point
        body += varint(0) + varint(len(key)) + varint(len(value)) + key + value
    body += (0).to_bytes(4, "little") + (1).to_bytes(4, "little")
    return bytes(body)


def table(values: list[bytes], *, compress: bool = True, per_block: int = 2) -> bytes:
    """A LevelDB table (.ldb) holding ``values`` under ascending internal keys."""
    out = bytearray()
    index_entries: list[tuple[bytes, bytes]] = []
    for start in range(0, len(values), per_block):
        entries = [
            (b"key-%06d" % (start + offset) + b"\x01" + b"\x00" * 7, value)
            for offset, value in enumerate(values[start:start + per_block])
        ]
        contents = block(entries)
        stored = snappy_compress(contents) if compress else contents
        handle = varint(len(out)) + varint(len(stored))
        out += stored + bytes([1 if compress else 0]) + b"\x00\x00\x00\x00"
        index_entries.append((entries[-1][0], handle))
    meta_handle = varint(len(out)) + varint(len(block([])))
    out += block([]) + b"\x00" + b"\x00\x00\x00\x00"
    index = block(index_entries)
    index_handle = varint(len(out)) + varint(len(index))
    out += index + b"\x00" + b"\x00\x00\x00\x00"
    footer = meta_handle + index_handle
    footer += b"\x00" * (40 - len(footer)) + (0xDB4775248B80FB57).to_bytes(8, "little")
    out += footer
    return bytes(out)
