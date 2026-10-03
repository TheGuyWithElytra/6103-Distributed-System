"""Loopback harness for tests/experiments only; production server has one thread."""

import threading
from contextlib import contextmanager

from .server import FlightServer


@contextmanager
def running_server(semantics="at-most-once", faults=None, logger=lambda _: None):
    server = FlightServer(port=0, semantics=semantics, faults=faults, logger=logger)
    stop = threading.Event()
    errors = []

    def serve():
        try:
            server.serve(stop)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        stop.set()
        thread.join(timeout=2)
        server.close()
        if thread.is_alive():
            raise RuntimeError("test server did not stop")
        if errors:
            raise RuntimeError("test server failed") from errors[0]
