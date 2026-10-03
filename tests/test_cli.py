"""Exercise the documented commands as separate processes, not imported APIs."""

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class CommandLineTests(unittest.TestCase):
    def test_server_and_interactive_client_processes(self):
        root = Path(__file__).resolve().parents[1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, PYTHONUTF8="1")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "server.log"
            with log_path.open("w", encoding="utf-8") as logfile:
                server = subprocess.Popen(
                    [sys.executable, "-u", "-m", "flight_system.server", "--host", "127.0.0.1",
                     "--port", str(port), "--semantics", "at-most-once"],
                    cwd=root, stdout=logfile, stderr=subprocess.STDOUT,
                    env=env, creationflags=flags)
                try:
                    deadline = time.monotonic() + 5
                    while "LISTEN" not in log_path.read_text(encoding="utf-8"):
                        if server.poll() is not None or time.monotonic() >= deadline:
                            self.fail("Server did not start: " + log_path.read_text(encoding="utf-8"))
                        time.sleep(0.02)
                    # All six menu operations, one server error, and graceful exit.
                    commands = "\n".join([
                        "1", "Singapore", "Tokyo", "2", "101", "3", "101", "2",
                        "5", "101", "700", "6", "101", "10", "4", "101", "0.05",
                        "2", "999", "0", ""])
                    result = subprocess.run(
                        [sys.executable, "-m", "flight_system.client", "--host", "127.0.0.1",
                         "--port", str(port)], input=commands, text=True, encoding="utf-8",
                        capture_output=True, timeout=10, cwd=root, env=env, creationflags=flags)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for expected in ("flight_ids: [101, 102]", "seats: 8", "fare: 700.0",
                                     "2026-10-20T08:10:00+00:00", "Monitor finished.", "NOT_FOUND"):
                        self.assertIn(expected, result.stdout)
                finally:
                    server.terminate()
                    server.wait(timeout=5)
            logs = log_path.read_text(encoding="utf-8")
            self.assertIn("ARGUMENTS", logs)
            self.assertIn("result={'seats': 8", logs)
            self.assertNotIn("Traceback", logs)
