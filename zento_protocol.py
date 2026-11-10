"""Zento D8 RS485 protocol core.

Based on Zento RS485 V1.10 and physically verified
NaboSpot barrier tests, November 2026.

This module performs no network I/O and cannot move a barrier.
"""

from __future__ import annotations

from dataclasses import dataclass


HEADER = b"\xAA\x55"
TERMINATOR = 0xFF

COMMAND_CODES = {
    "open": 0x02,
    "close": 0x03,
    "stop": 0x04,
    "firmware": 0x09,
    "status": 0x0A,
}

# Physical verification established reversed direction
# for the tested Zento D8 installation.
REVERSED_COMMAND_CODES = {
    "open": 0x03,
    "close": 0x02,
    "stop": 0x04,
    "firmware": 0x09,
    "status": 0x0A,
}

DOCUMENTED_STATES = {
    0x03: "closing",
    0x04: "closed",
    0x05: "opening",
    0x06: "open",
    0x07: "stopped",
    0x08: "locked",
}

VERIFIED_REVERSED_STATES = {
    0x03: "opening",
    0x04: "open",
    0x05: "closing",
    0x06: "closed",
    0x07: "stopped",
    # 0x08 has not been physically verified.
}


class ZentoProtocolError(ValueError):
    """Invalid Zento binary protocol frame."""


@dataclass(frozen=True)
class ZentoFrame:
    address: int
    command: int
    payload: bytes
    raw: bytes

    @property
    def status_code(self) -> int | None:
        if self.command == 0x0A and self.payload:
            return self.payload[0]
        return None


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_frame(
    command: str,
    address: int = 0,
    *,
    reversed_direction: bool = False,
) -> bytes:
    if not isinstance(address, int) or not 0 <= address <= 255:
        raise ValueError("RS485 address must be between 0 and 255")

    codes = (
        REVERSED_COMMAND_CODES
        if reversed_direction
        else COMMAND_CODES
    )

    command = command.lower().strip()

    if command not in codes:
        raise ValueError(f"Unsupported command: {command}")

    # Length includes the two length bytes, address,
    # command, checksum and terminator.
    body = bytes([
        0x07, 0x00,
        address,
        codes[command],
        0x00,
    ])

    return (
        HEADER
        + body
        + bytes([checksum(body), TERMINATOR])
    )


def parse_frame(raw: bytes) -> ZentoFrame:
    raw = bytes(raw)

    if len(raw) < 9:
        raise ZentoProtocolError("Frame too short")

    if raw[:2] != HEADER:
        raise ZentoProtocolError("Invalid header")

    length = int.from_bytes(raw[2:4], "little")

    if length < 7 or length > 1024:
        raise ZentoProtocolError("Invalid frame length")

    if len(raw) != length + 2:
        raise ZentoProtocolError("Frame length mismatch")

    if raw[-1] != TERMINATOR:
        raise ZentoProtocolError("Invalid terminator")

    expected = checksum(raw[2:-2])

    if raw[-2] != expected:
        raise ZentoProtocolError("Invalid checksum")

    return ZentoFrame(
        address=raw[4],
        command=raw[5],
        payload=raw[6:-2],
        raw=raw,
    )


def decode_status(
    frame: ZentoFrame,
    *,
    reversed_direction: bool = False,
) -> str:
    if frame.command != 0x0A:
        raise ZentoProtocolError("Not a status response")

    if frame.status_code is None:
        raise ZentoProtocolError("Missing status code")

    mapping = (
        VERIFIED_REVERSED_STATES
        if reversed_direction
        else DOCUMENTED_STATES
    )

    return mapping.get(frame.status_code, "unknown")


class ZentoFrameBuffer:
    """Incremental parser for a TCP byte stream.

    Handles partial frames, multiple frames, and leading noise.
    """

    def __init__(self, max_buffer: int = 4096):
        self.buffer = bytearray()
        self.max_buffer = max_buffer

    def feed(self, data: bytes) -> list[ZentoFrame]:
        self.buffer.extend(data)
        frames = []

        while self.buffer:
            start = self.buffer.find(HEADER)

            if start < 0:
                # Preserve a possible partial header.
                if self.buffer[-1] == 0xAA:
                    self.buffer[:] = b"\xAA"
                else:
                    self.buffer.clear()
                break

            if start:
                del self.buffer[:start]

            if len(self.buffer) < 4:
                break

            length = int.from_bytes(
                self.buffer[2:4], "little"
            )

            if length < 7 or length > 1024:
                del self.buffer[0]
                continue

            total = length + 2

            if len(self.buffer) < total:
                break

            candidate = bytes(self.buffer[:total])

            try:
                frame = parse_frame(candidate)
            except ZentoProtocolError:
                # Resynchronize without accepting corrupt data.
                del self.buffer[0]
                continue

            frames.append(frame)
            del self.buffer[:total]

        if len(self.buffer) > self.max_buffer:
            self.buffer.clear()
            raise ZentoProtocolError(
                "TCP receive buffer exceeded limit"
            )

        return frames
