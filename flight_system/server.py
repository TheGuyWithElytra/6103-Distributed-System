"""Single-threaded UDP server. Run with python -m flight_system.server."""

import argparse
import socket
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from .faults import Faults
from .protocol import (Kind, Message, Op, ProtocolError, Status, Writer,
                       decode, encode, read_args, read_result)


@dataclass
class Flight:
    identifier: int
    source: str
    destination: str
    departure: int
    fare: float
    seats: int
    version: int = 0


def sample_flights():
    departure = int(datetime(2026, 10, 20, 8, tzinfo=timezone.utc).timestamp())
    return {
        102: Flight(102, "SIN", "HND", departure, 600.0, 10),
        104: Flight(104, "SIN", "NRT", departure + 3600, 650.0, 20),
        828: Flight(828, "SIN", "PVG", departure + 7200, 350.5, 8),
        800: Flight(800, "SIN", "PEK", departure + 10800, 420.25, 12),
    }


@dataclass
class Subscription:
    flight_id: int
    address: tuple
    session: int
    request: int
    expires: float


class FlightServer:
    def __init__(self, host="127.0.0.1", port=6789, semantics="at-most-once",
                 faults=None, logger=print):
        if semantics not in ("at-least-once", "at-most-once"):
            raise ValueError("invalid invocation semantics")
        self.semantics = semantics
        self.faults = faults or Faults()
        self.log = logger
        self.flights = sample_flights()
        self.history = {}  # key -> (original packet, encoded reply)
        self.subscriptions = {}
        self.executions = Counter()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((host, port))
            self.sock.settimeout(0.1)
        except OSError:
            self.sock.close()
            raise
        self.address = self.sock.getsockname()

    def close(self):
        self.sock.close()

    def expire(self):
        now = time.monotonic()
        for key, sub in list(self.subscriptions.items()):
            if sub.expires <= now:
                del self.subscriptions[key]
                self.log(f"EXPIRE session={sub.session} subscription={sub.request}")

    def notify(self, flight):
        self.expire()
        body = Writer().i32(flight.identifier).i32(flight.seats).u64(flight.version).finish()
        for sub in list(self.subscriptions.values()):
            if sub.flight_id == flight.identifier:
                packet = encode(Message(Kind.CALLBACK, Op.MONITOR, sub.session, sub.request, body=body))
                try:
                    self.sock.sendto(packet, sub.address)
                    self.log(f"CALLBACK flight={flight.identifier} seats={flight.seats} "
                             f"version={flight.version} to={sub.address}")
                except OSError as exc:
                    self.log(f"CALLBACK_SEND_ERROR {exc}")

    def dispatch(self, msg, address):
        args = read_args(msg.op, msg.body)  # validate entire payload before mutation
        self.log(f"ARGUMENTS id={msg.request} values={args!r}")
        w = Writer()
        if msg.op == Op.SEARCH:
            source, destination = (s.strip() for s in args)
            if not source or not destination:
                return Status.BAD_REQUEST, Writer().text("Route cannot be empty").finish(), None
            ids = [f.identifier for f in self.flights.values()
                   if f.source.casefold() == source.casefold()
                   and f.destination.casefold() == destination.casefold()]
            if not ids:
                return Status.NOT_FOUND, Writer().text("No matching flights").finish(), None
            # 28 header + 2 count + 4 bytes per id must fit in one datagram.
            if len(ids) > 292:
                return Status.TOO_LARGE, Writer().text("Too many results for one datagram").finish(), None
            w.u16(len(ids))
            for identifier in ids:
                w.i32(identifier)
            self.executions[msg.op.name] += 1
            return Status.OK, w.finish(), None

        flight = self.flights.get(args[0])
        if flight is None:
            return Status.NOT_FOUND, Writer().text("Flight does not exist").finish(), None
        if msg.op == Op.DETAILS:
            w.i32(flight.identifier).text(flight.source).text(flight.destination)
            w.i64(flight.departure).f64(flight.fare).i32(flight.seats).u64(flight.version)
        elif msg.op == Op.RESERVE:
            count = args[1]
            if count <= 0:
                return Status.BAD_REQUEST, Writer().text("Seat count must be positive").finish(), None
            if count > flight.seats:
                return Status.INSUFFICIENT_SEATS, Writer().text("Insufficient available seats").finish(), None
            flight.seats -= count
            flight.version += 1
            w.i32(flight.seats).u64(flight.version)
        elif msg.op == Op.MONITOR:
            interval = args[1]
            if not 1 <= interval <= 3_600_000:
                return Status.BAD_REQUEST, Writer().text("Monitor interval must be 1..3600000 ms").finish(), None
            # A repeated at-least-once registration replaces the same subscription.
            # At-most-once retries are intercepted by the history before dispatch.
            self.subscriptions[(msg.session, msg.request)] = Subscription(
                flight.identifier, address, msg.session, msg.request,
                time.monotonic() + interval / 1000)
            w.u32(interval)
        elif msg.op == Op.SET_FARE:
            if args[1] < 0:
                return Status.BAD_REQUEST, Writer().text("Fare cannot be negative").finish(), None
            flight.fare = args[1]
            w.f64(flight.fare)
        elif msg.op == Op.DELAY:
            minutes = args[1]
            # Keep dates representable by the console's UTC datetime formatter.
            if minutes <= 0 or flight.departure + minutes * 60 > 253402300799:
                return Status.BAD_REQUEST, Writer().text("Invalid delay or departure beyond year 9999").finish(), None
            flight.departure += minutes * 60
            w.i64(flight.departure)
        self.executions[msg.op.name] += 1
        return Status.OK, w.finish(), flight if msg.op == Op.RESERVE else None

    def handle(self, packet, address):
        try:
            msg = decode(packet)
        except ProtocolError as exc:
            self.log(f"MALFORMED from={address}: {exc}")
            return
        if msg.kind != Kind.REQUEST:
            return
        self.log(f"REQUEST session={msg.session} id={msg.request} op={msg.op.name} from={address}")
        if self.faults.drop("request"):
            self.log(f"DROP_REQUEST id={msg.request}")
            return
        key = (msg.session, msg.request)
        changed = None
        if self.semantics == "at-most-once" and key in self.history:
            original, reply = self.history[key]
            if original != packet:
                reply = encode(Message(Kind.REPLY, msg.op, msg.session, msg.request,
                                       Status.REQUEST_CONFLICT,
                                       Writer().text("Request ID reused with different content").finish()))
                self.log(f"REQUEST_CONFLICT id={msg.request}")
            else:
                self.log(f"CACHE_HIT id={msg.request}")
        else:
            try:
                status, body, changed = self.dispatch(msg, address)
            except ProtocolError as exc:
                status, body = Status.BAD_REQUEST, Writer().text(str(exc)).finish()
            reply = encode(Message(Kind.REPLY, msg.op, msg.session, msg.request, status, body))
            if self.semantics == "at-most-once":
                self.history[key] = (packet, reply)
            self.log(f"EXECUTE id={msg.request} op={msg.op.name} status={status.name}")
        # Record before simulated loss. Cache replay never causes a new callback.
        if changed is not None:
            self.notify(changed)
        reply_msg = decode(reply)
        self.log(f"REPLY id={msg.request} status={reply_msg.status.name} result={read_result(reply_msg)!r}")
        if self.faults.drop("reply"):
            self.log(f"DROP_REPLY id={msg.request}")
        else:
            try:
                self.sock.sendto(reply, address)
            except OSError as exc:
                self.log(f"REPLY_SEND_ERROR {exc}")

    def serve(self, stop=None):
        self.log(f"LISTEN {self.address[0]}:{self.address[1]} semantics={self.semantics}")
        while stop is None or not stop.is_set():
            self.expire()
            try:
                # Read the complete UDP datagram so oversize messages can be rejected.
                packet, address = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows may report ICMP port-unreachable after a client exits.
                continue
            self.handle(packet, address)


def positive_indices(value):
    try:
        indices = {int(item) for item in value.split(",") if item}
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use comma-separated positive integers") from exc
    if any(i < 1 for i in indices):
        raise argparse.ArgumentTypeError("packet indices start at 1")
    return indices


def loss_rate(value):
    try:
        rate = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("loss rate must be a number") from exc
    if not 0 <= rate <= 1:
        raise argparse.ArgumentTypeError("loss rate must be 0..1")
    return rate


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=6789)
    p.add_argument("--semantics", choices=["at-least-once", "at-most-once"], default="at-most-once")
    p.add_argument("--drop-requests", type=positive_indices, default=set())
    p.add_argument("--drop-replies", type=positive_indices, default=set())
    p.add_argument("--request-loss", type=loss_rate, default=0.0)
    p.add_argument("--reply-loss", type=loss_rate, default=0.0)
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()
    server = FlightServer(args.host, args.port, args.semantics,
                          Faults(args.drop_requests, args.drop_replies,
                                 args.request_loss, args.reply_loss, args.seed))
    try:
        server.serve()
    except KeyboardInterrupt:
        print("Server stopped.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
