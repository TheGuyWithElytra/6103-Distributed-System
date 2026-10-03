"""Synchronous console client with bounded UDP retries and callback reception."""

import argparse
import math
import secrets
import socket
import time
from collections import deque
from datetime import datetime, timezone

from .protocol import (Kind, Message, Op, ProtocolError, Status, decode, encode,
                       read_result, request_body)


class RemoteError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"{status.name}: {message}")


class OutcomeUnknown(TimeoutError):
    """No reply received; this does not mean the operation was not executed."""


class FlightClient:
    def __init__(self, host="127.0.0.1", port=6789, timeout=0.5, attempts=4, logger=print):
        if not math.isfinite(timeout) or timeout <= 0 or attempts < 1:
            raise ValueError("timeout must be finite and positive; attempts must be >= 1")
        self.server = (socket.gethostbyname(host), port)
        self.timeout, self.attempts, self.log = timeout, attempts, logger
        self.session = secrets.randbits(64)
        self.sequence = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.pending = deque(maxlen=1024)
        self.last_attempts = 0

    def close(self):
        self.sock.close()

    def receive(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                packet, address = self.sock.recvfrom(65535)
            except socket.timeout:
                return None
            except ConnectionResetError:
                continue
            if address != self.server:
                continue
            try:
                msg = decode(packet)
            except ProtocolError:
                continue
            if msg.session == self.session:
                return msg

    def invoke(self, op, *args):
        self.sequence += 1
        request = self.sequence
        packet = encode(Message(Kind.REQUEST, op, self.session, request,
                                body=request_body(op, *args)))
        for attempt in range(1, self.attempts + 1):
            self.last_attempts = attempt
            self.log(f"SEND session={self.session} id={request} op={op.name} attempt={attempt}")
            self.sock.sendto(packet, self.server)
            deadline = time.monotonic() + self.timeout
            while True:
                msg = self.receive(deadline)
                if msg is None:
                    break
                if msg.kind == Kind.CALLBACK:
                    if op == Op.MONITOR and msg.request == request:
                        self.pending.append(msg)
                    continue
                if msg.kind != Kind.REPLY or msg.request != request or msg.op != op:
                    continue  # late response to another operation
                try:
                    result = read_result(msg)
                except ProtocolError:
                    continue
                if msg.status != Status.OK:
                    raise RemoteError(msg.status, result["error"])
                return result
            self.log(f"TIMEOUT id={request} attempt={attempt}")
        raise OutcomeUnknown(
            f"No reply after {self.attempts} attempts (session={self.session}, id={request}). "
            "Outcome unknown: the server may already have executed the operation. "
            "Do not blindly submit a new reservation/delay; query the state first.")

    def monitor(self, flight_id, interval_ms, on_update=print):
        self.pending.clear()
        result = self.invoke(Op.MONITOR, flight_id, interval_ms)
        subscription = self.sequence
        # Server expiry is authoritative. Local wait starts at ACK receipt, so a
        # delayed/replayed ACK can leave a quiet tail after the server has expired.
        deadline = time.monotonic() + result["interval_ms"] / 1000
        latest_version = -1
        updates = []
        while time.monotonic() < deadline:
            msg = self.pending.popleft() if self.pending else self.receive(deadline)
            if msg is None:
                break
            if msg.kind != Kind.CALLBACK or msg.op != Op.MONITOR or msg.request != subscription:
                continue
            try:
                update = read_result(msg)
            except ProtocolError:
                continue
            if update["flight_id"] != flight_id or update["version"] <= latest_version:
                continue
            latest_version = update["version"]
            updates.append(update)
            on_update(update)
        self.pending.clear()
        return updates


def display(result):
    result = dict(result)
    if "departure" in result:
        result["departure"] = datetime.fromtimestamp(result["departure"], timezone.utc).isoformat()
    for key, value in result.items():
        print(f"  {key}: {value}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6789)
    p.add_argument("--timeout", type=float, default=0.5)
    p.add_argument("--attempts", type=int, default=4, help="total attempts including the first send")
    args = p.parse_args()
    try:
        client = FlightClient(args.host, args.port, args.timeout, args.attempts)
    except (ValueError, OSError) as exc:
        p.error(str(exc))
    print("Flight information client. Dates use UTC; semantics are selected on the server.")
    try:
        while True:
            print("\n1 Search  2 Details  3 Reserve  4 Monitor  5 Set fare  6 Delay  0 Exit")
            try:
                choice = input("> ").strip()
                if choice == "0":
                    break
                if choice == "1":
                    result = client.invoke(Op.SEARCH, input("Source: "), input("Destination: "))
                elif choice in ("2", "3", "4", "5", "6"):
                    flight_id = int(input("Flight ID: "))
                    op = Op(int(choice))
                    if op == Op.DETAILS:
                        result = client.invoke(op, flight_id)
                    elif op == Op.MONITOR:
                        seconds = float(input("Monitor seconds (up to 3600): "))
                        print("Waiting for callbacks; input resumes after the interval.")
                        client.monitor(flight_id, int(seconds * 1000), display)
                        print("Monitor finished.")
                        continue
                    elif op == Op.SET_FARE:
                        result = client.invoke(op, flight_id, float(input("New fare: ")))
                    else:
                        value = int(input("Seats: " if op == Op.RESERVE else "Delay minutes: "))
                        result = client.invoke(op, flight_id, value)
                else:
                    print("Choose 0..6.")
                    continue
                display(result)
            except (ValueError, OverflowError, RemoteError, OutcomeUnknown, OSError) as exc:
                print(f"Error: {exc}")
    except (KeyboardInterrupt, EOFError):
        print("\nClient stopped.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
