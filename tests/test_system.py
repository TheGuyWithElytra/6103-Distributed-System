import socket
import struct
import threading
import time
import unittest

from flight_system.client import FlightClient, OutcomeUnknown, RemoteError
from flight_system.faults import Faults
from flight_system.harness import running_server
from flight_system.protocol import (HEADER_SIZE, Kind, Message, Op, ProtocolError,
                                    Reader, Status, Writer, decode, encode,
                                    read_result, request_body)


def client_for(server, **kwargs):
    return FlightClient(*server.address, timeout=0.05, logger=lambda _: None, **kwargs)


class ProtocolTests(unittest.TestCase):
    def test_independent_golden_packet(self):
        # Fixed external byte fixture, not just encoder/decoder round-trip.
        expected = bytes.fromhex(
            "464c 01 01 02 00 0000000000000001 0000000000000002 0000 00000004 00000065")
        msg = Message(Kind.REQUEST, Op.DETAILS, 1, 2, body=request_body(Op.DETAILS, 101))
        self.assertEqual(encode(msg), expected)
        self.assertEqual(decode(expected), msg)
        self.assertEqual(HEADER_SIZE, 28)

    def test_utf8_byte_length_and_floating_point(self):
        self.assertEqual(Writer().text("新").finish(), b"\x00\x03\xe6\x96\xb0")
        self.assertEqual(Writer().f64(500).finish().hex(), "407f400000000000")
        r = Reader(Writer().text("新加坡").f64(420.25).i32(-1).finish())
        self.assertEqual((r.text(), r.f64(), r.i32()), ("新加坡", 420.25, -1))
        r.end()

    def test_every_truncation_rejected(self):
        packet = encode(Message(Kind.REQUEST, Op.SEARCH, 1, 1, body=request_body(Op.SEARCH, "a", "b")))
        for size in range(len(packet)):
            with self.subTest(size=size), self.assertRaises(ProtocolError):
                decode(packet[:size])
        with self.assertRaises(ProtocolError):
            decode(packet + b"extra")

    def test_invalid_fields(self):
        for body in (b"\x00\x01\xff", b"\xff\xff"):
            with self.assertRaises(ProtocolError):
                Reader(body).text()
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(ProtocolError):
                Writer().f64(value)
            with self.assertRaises(ProtocolError):
                Reader(struct.pack("!d", value)).f64()
        with self.assertRaises(ProtocolError):
            Writer().u32(-1)
        with self.assertRaises(ProtocolError):
            encode(Message(Kind.REQUEST, Op.SEARCH, 1, 1, body=b"x" * 1200))


class ServiceTests(unittest.TestCase):
    def test_six_operations_and_utf8(self):
        with running_server() as server:
            client = client_for(server)
            self.addCleanup(client.close)
            self.assertEqual(client.invoke(Op.SEARCH, "Singapore", "Tokyo")["flight_ids"], [101, 102])
            self.assertEqual(client.invoke(Op.SEARCH, "新加坡", "北京")["flight_ids"], [104])
            initial = client.invoke(Op.DETAILS, 101)
            self.assertEqual(client.invoke(Op.RESERVE, 101, 2)["seats"], 8)
            self.assertEqual(client.invoke(Op.SET_FARE, 101, 700.5)["fare"], 700.5)
            self.assertEqual(client.invoke(Op.SET_FARE, 101, 700.5)["fare"], 700.5)
            self.assertEqual(client.invoke(Op.DELAY, 101, 10)["departure"], initial["departure"] + 600)
            self.assertEqual(client.invoke(Op.DELAY, 101, 10)["departure"], initial["departure"] + 1200)
            self.assertEqual(client.invoke(Op.MONITOR, 101, 50)["interval_ms"], 50)

    def test_business_errors_do_not_mutate(self):
        cases = [(Op.SEARCH, ("Nowhere", "Tokyo"), Status.NOT_FOUND),
                 (Op.SEARCH, ("", "Tokyo"), Status.BAD_REQUEST),
                 (Op.DETAILS, (999,), Status.NOT_FOUND),
                 (Op.RESERVE, (101, 0), Status.BAD_REQUEST),
                 (Op.RESERVE, (101, -2), Status.BAD_REQUEST),
                 (Op.RESERVE, (101, 11), Status.INSUFFICIENT_SEATS),
                 (Op.SET_FARE, (101, -1.0), Status.BAD_REQUEST),
                 (Op.DELAY, (101, 0), Status.BAD_REQUEST),
                 (Op.MONITOR, (101, 0), Status.BAD_REQUEST),
                 (Op.MONITOR, (101, 3600001), Status.BAD_REQUEST)]
        with running_server() as server:
            client = client_for(server)
            self.addCleanup(client.close)
            before = client.invoke(Op.DETAILS, 101)
            for op, args, status in cases:
                with self.subTest(op=op, args=args), self.assertRaises(RemoteError) as error:
                    client.invoke(op, *args)
                self.assertEqual(error.exception.status, status)
            self.assertEqual(client.invoke(Op.DETAILS, 101), before)

    def test_malformed_packets_and_payload_do_not_kill_server(self):
        with running_server() as server:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(sock.close)
            sock.settimeout(1)
            for packet in (b"bad", b"x" * 2000):
                sock.sendto(packet, server.address)
            malformed = Message(Kind.REQUEST, Op.RESERVE, 99, 1, body=request_body(Op.RESERVE, 101, 2) + b"x")
            sock.sendto(encode(malformed), server.address)
            reply = decode(sock.recvfrom(65535)[0])
            self.assertEqual(reply.status, Status.BAD_REQUEST)
            client = client_for(server)
            self.addCleanup(client.close)
            self.assertEqual(client.invoke(Op.DETAILS, 101)["seats"], 10)


class SemanticsTests(unittest.TestCase):
    def test_lost_reply_non_idempotent(self):
        for mode, expected, executions in [("at-least-once", 6, 2), ("at-most-once", 8, 1)]:
            with self.subTest(mode=mode), running_server(mode, Faults(drop_replies={1})) as server:
                client = client_for(server)
                try:
                    self.assertEqual(client.invoke(Op.RESERVE, 101, 2)["seats"], expected)
                    self.assertEqual(client.last_attempts, 2)
                    self.assertEqual(server.executions["RESERVE"], executions)
                finally:
                    client.close()

    def test_lost_request(self):
        for mode in ("at-least-once", "at-most-once"):
            with self.subTest(mode=mode), running_server(mode, Faults(drop_requests={1})) as server:
                client = client_for(server)
                try:
                    self.assertEqual(client.invoke(Op.RESERVE, 101, 2)["seats"], 8)
                    self.assertEqual(client.last_attempts, 2)
                    self.assertEqual(server.executions["RESERVE"], 1)
                finally:
                    client.close()

    def test_all_replies_lost_means_unknown_not_failed(self):
        with running_server(faults=Faults(reply_rate=1)) as server:
            client = client_for(server, attempts=3)
            self.addCleanup(client.close)
            with self.assertRaises(OutcomeUnknown):
                client.invoke(Op.RESERVE, 101, 2)
            self.assertEqual(server.flights[101].seats, 8)
            self.assertEqual(server.executions["RESERVE"], 1)

    def test_all_requests_lost(self):
        with running_server(faults=Faults(request_rate=1)) as server:
            client = client_for(server, attempts=2)
            self.addCleanup(client.close)
            with self.assertRaises(OutcomeUnknown):
                client.invoke(Op.RESERVE, 101, 2)
            self.assertEqual(server.flights[101].seats, 10)

    def test_clients_have_separate_request_namespaces(self):
        with running_server() as server:
            clients = [client_for(server), client_for(server)]
            for client in clients:
                self.addCleanup(client.close)
            clients[0].invoke(Op.RESERVE, 101, 2)
            self.assertEqual(clients[1].invoke(Op.RESERVE, 101, 2)["seats"], 6)
            self.assertEqual(server.executions["RESERVE"], 2)

    def test_conflict_and_original_cached_reply(self):
        with running_server() as server:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(sock.close)
            sock.settimeout(1)

            def send(count):
                sock.sendto(encode(Message(Kind.REQUEST, Op.RESERVE, 1, 1,
                                          body=request_body(Op.RESERVE, 101, count))), server.address)
                return decode(sock.recvfrom(65535)[0])

            first = send(2)
            self.assertEqual(send(3).status, Status.REQUEST_CONFLICT)
            self.assertEqual(send(2), first)
            self.assertEqual(server.flights[101].seats, 8)

    def test_cached_business_failure_is_not_reexecuted(self):
        with running_server() as server:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(sock.close)
            sock.settimeout(1)
            packet = encode(Message(Kind.REQUEST, Op.RESERVE, 1, 1,
                                    body=request_body(Op.RESERVE, 101, 11)))
            sock.sendto(packet, server.address)
            first = sock.recvfrom(65535)[0]
            sock.sendto(packet, server.address)
            self.assertEqual(sock.recvfrom(65535)[0], first)
            self.assertEqual(decode(first).status, Status.INSUFFICIENT_SEATS)


class CallbackTests(unittest.TestCase):
    def test_callback_arrives_before_retried_registration_ack(self):
        # Lose the registration ACK. A different client reserves while the
        # watcher is still inside invoke(); the callback must survive ACK retry.
        with running_server(faults=Faults(drop_replies={1})) as server:
            watcher = FlightClient(*server.address, timeout=0.15, logger=lambda _: None)
            booker = client_for(server)
            self.addCleanup(watcher.close)
            self.addCleanup(booker.close)
            results, errors = [], []

            def monitor():
                try:
                    results.extend(watcher.monitor(101, 350, lambda _: None))
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=monitor)
            thread.start()
            deadline = time.monotonic() + 1
            while not server.subscriptions and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(server.subscriptions)
            booker.invoke(Op.RESERVE, 101, 2)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([r["seats"] for r in results], [8])
            self.assertEqual(server.executions["MONITOR"], 1)

    def test_multiple_subscribers_expiry_and_no_duplicate_effect(self):
        with running_server() as server:
            watchers = [client_for(server), client_for(server)]
            booker = client_for(server)
            for client in [*watchers, booker]:
                self.addCleanup(client.close)
            for watcher in watchers:
                watcher.invoke(Op.MONITOR, 101, 250)
            # Drop the reservation's first reply: duplicate must not notify twice.
            server.faults.indices["reply"].add(3)
            booker.invoke(Op.RESERVE, 101, 2)
            for watcher in watchers:
                callback = watcher.receive(time.monotonic() + 0.2)
                self.assertIsNotNone(callback)
                self.assertEqual(read_result(callback)["seats"], 8)
                self.assertEqual(callback.request, 1)
                self.assertIsNone(watcher.receive(time.monotonic() + 0.02))
            time.sleep(0.28)
            booker.invoke(Op.RESERVE, 101, 1)
            for watcher in watchers:
                self.assertIsNone(watcher.receive(time.monotonic() + 0.03))
            self.assertEqual(len(server.subscriptions), 0)

    def test_monitor_api_and_wrong_flight(self):
        with running_server() as server:
            watcher, booker = client_for(server), client_for(server)
            self.addCleanup(watcher.close)
            self.addCleanup(booker.close)
            results, errors = [], []

            def monitor():
                try:
                    results.extend(watcher.monitor(101, 250, lambda _: None))
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=monitor)
            thread.start()
            deadline = time.monotonic() + 1
            while not server.subscriptions and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(server.subscriptions)
            booker.invoke(Op.RESERVE, 102, 1)
            booker.invoke(Op.RESERVE, 101, 3)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([r["seats"] for r in results], [7])


if __name__ == "__main__":
    unittest.main()
