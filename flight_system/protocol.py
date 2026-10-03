"""Wire format shared by both programs; no object serialization or RPC library.

Integer fields are encoded explicitly. struct is used only for the primitive
IEEE 754 double representation, not to serialize application objects.
"""

import math
import struct
from dataclasses import dataclass
from enum import IntEnum

MAGIC = b"FL"
VERSION = 1
HEADER_SIZE = 28
MAX_PACKET = 1200
MAX_TEXT = 512


class Kind(IntEnum):
    REQUEST = 1
    REPLY = 2
    CALLBACK = 3


class Op(IntEnum):
    SEARCH = 1
    DETAILS = 2
    RESERVE = 3
    MONITOR = 4
    SET_FARE = 5
    DELAY = 6


class Status(IntEnum):
    OK = 0
    BAD_REQUEST = 1
    NOT_FOUND = 2
    INSUFFICIENT_SEATS = 3
    REQUEST_CONFLICT = 4
    TOO_LARGE = 5


class ProtocolError(ValueError):
    pass


class Writer:
    def __init__(self):
        self.data = bytearray()

    def integer(self, value, size, signed=False):
        try:
            self.data.extend(value.to_bytes(size, "big", signed=signed))
        except (OverflowError, AttributeError) as exc:
            raise ProtocolError("integer outside wire range") from exc
        return self

    def u8(self, v):
        return self.integer(v, 1)

    def u16(self, v):
        return self.integer(v, 2)

    def u32(self, v):
        return self.integer(v, 4)

    def u64(self, v):
        return self.integer(v, 8)

    def i32(self, v):
        return self.integer(v, 4, True)

    def i64(self, v):
        return self.integer(v, 8, True)

    def f64(self, v):
        if not math.isfinite(v):
            raise ProtocolError("floating point value must be finite")
        self.data.extend(struct.pack("!d", v))
        return self

    def text(self, v):
        raw = v.encode("utf-8")
        if len(raw) > MAX_TEXT:
            raise ProtocolError("UTF-8 string too long")
        self.u16(len(raw))
        self.data.extend(raw)
        return self

    def finish(self):
        return bytes(self.data)


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ProtocolError("truncated field")
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def integer(self, size, signed=False):
        return int.from_bytes(self.take(size), "big", signed=signed)

    def u8(self):
        return self.integer(1)

    def u16(self):
        return self.integer(2)

    def u32(self):
        return self.integer(4)

    def u64(self):
        return self.integer(8)

    def i32(self):
        return self.integer(4, True)

    def i64(self):
        return self.integer(8, True)

    def f64(self):
        value = struct.unpack("!d", self.take(8))[0]
        if not math.isfinite(value):
            raise ProtocolError("non-finite floating point value")
        return value

    def text(self):
        size = self.u16()
        if size > MAX_TEXT:
            raise ProtocolError("string too long")
        try:
            return self.take(size).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("invalid UTF-8") from exc

    def end(self):
        if self.pos != len(self.data):
            raise ProtocolError("unexpected trailing bytes")


@dataclass(frozen=True)
class Message:
    kind: Kind
    op: Op
    session: int
    request: int
    status: Status = Status.OK
    body: bytes = b""


def encode(msg):
    w = Writer()
    w.data.extend(MAGIC)
    w.u8(VERSION).u8(int(msg.kind)).u8(int(msg.op)).u8(0)
    w.u64(msg.session).u64(msg.request).u16(int(msg.status)).u32(len(msg.body))
    packet = w.finish() + msg.body
    if len(packet) > MAX_PACKET:
        raise ProtocolError("datagram exceeds 1200 bytes")
    return packet


def decode(packet):
    if not HEADER_SIZE <= len(packet) <= MAX_PACKET:
        raise ProtocolError("invalid datagram size")
    r = Reader(packet)
    if r.take(2) != MAGIC or r.u8() != VERSION:
        raise ProtocolError("unsupported magic/version")
    try:
        kind, op = Kind(r.u8()), Op(r.u8())
        if r.u8() != 0:
            raise ProtocolError("reserved byte must be zero")
        session, request = r.u64(), r.u64()
        status = Status(r.u16())
    except ValueError as exc:
        raise ProtocolError("invalid header") from exc
    body = r.take(r.u32())
    r.end()
    if kind != Kind.REPLY and status != Status.OK:
        raise ProtocolError("only replies can contain error status")
    return Message(kind, op, session, request, status, body)


def request_body(op, *args):
    w = Writer()
    if op == Op.SEARCH:
        w.text(args[0]).text(args[1])
    elif op == Op.DETAILS:
        w.i32(args[0])
    elif op in (Op.RESERVE, Op.DELAY):
        w.i32(args[0]).i32(args[1])
    elif op == Op.MONITOR:
        w.i32(args[0]).u32(args[1])  # milliseconds
    elif op == Op.SET_FARE:
        w.i32(args[0]).f64(args[1])
    else:
        raise ProtocolError("unknown operation")
    return w.finish()


def read_args(op, body):
    r = Reader(body)
    if op == Op.SEARCH:
        args = (r.text(), r.text())
    elif op == Op.DETAILS:
        args = (r.i32(),)
    elif op in (Op.RESERVE, Op.DELAY):
        args = (r.i32(), r.i32())
    elif op == Op.MONITOR:
        args = (r.i32(), r.u32())
    elif op == Op.SET_FARE:
        args = (r.i32(), r.f64())
    r.end()
    return args


def read_result(msg):
    r = Reader(msg.body)
    if msg.status != Status.OK:
        result = {"error": r.text()}
    elif msg.kind == Kind.CALLBACK:
        if msg.op != Op.MONITOR:
            raise ProtocolError("callback must use MONITOR")
        result = {"flight_id": r.i32(), "seats": r.i32(), "version": r.u64()}
    elif msg.op == Op.SEARCH:
        result = {"flight_ids": [r.i32() for _ in range(r.u16())]}
    elif msg.op == Op.DETAILS:
        result = {"flight_id": r.i32(), "source": r.text(), "destination": r.text(),
                  "departure": r.i64(), "fare": r.f64(), "seats": r.i32(), "version": r.u64()}
    elif msg.op == Op.RESERVE:
        result = {"seats": r.i32(), "version": r.u64()}
    elif msg.op == Op.MONITOR:
        result = {"interval_ms": r.u32()}
    elif msg.op == Op.SET_FARE:
        result = {"fare": r.f64()}
    elif msg.op == Op.DELAY:
        result = {"departure": r.i64()}
    r.end()
    return result
