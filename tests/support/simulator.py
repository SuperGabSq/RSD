"""Harness for running the uC simulator as a real subprocess.

Used by Phase 1 (which tests the simulator itself) and Phase 3 (which streams it through
the whole backend). Shared rather than duplicated, because "start it and wait until the
port answers" is exactly the sort of setup that quietly diverges between two copies and
then makes one suite flaky for reasons the other never sees.

Ports are always ephemeral (bind 0, let the OS choose) so the suite never collides with
a simulator someone left running, and never fails because a hard-coded port was busy.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from dataclasses import dataclass

from tests.support import REPO_ROOT, SIMULATOR_PATH

STARTUP_TIMEOUT_S = 15.0


@dataclass
class Simulator:
    process: subprocess.Popen
    port: int

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - only if it wedges
            self.process.kill()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_port(port: int, timeout: float = STARTUP_TIMEOUT_S) -> None:
    """Poll rather than sleep a fixed amount: on a loaded machine a fixed sleep is
    either too short (flaky) or too long (slow), and there is no value that is both."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"simulator never came up on port {port}")


def start_simulator(*extra_args: str) -> Simulator:
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(SIMULATOR_PATH), "--host", "127.0.0.1", "--port", str(port),
         *extra_args],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_port(port)
    return Simulator(process, port)
