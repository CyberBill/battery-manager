#!/usr/bin/env python3
"""High-level manager for discovering and monitoring Daly BMS units across multiple serial buses."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

try:
    import serial.tools.list_ports as list_ports
except ImportError:  # pragma: no cover - pyserial is required for runtime use.
    list_ports = None


DEFAULT_DISCOVER_MASK = 0xFFFFFFFF


def filter_serial_ports(raw_ports: Iterable[str]) -> list[str]:
    """Return the subset of /dev names likely to be serial adapters or UART devices."""
    candidates: list[str] = []
    seen: set[str] = set()

    for port in raw_ports:
        if not port:
            continue
        path = str(port)
        if path in seen:
            continue
        seen.add(path)

        if any(
            token in path
            for token in ("/ttyUSB", "/ttyACM", "/ttyAMA", "/ttyS", "/ttyXRUSB", "/rfcomm")
        ):
            candidates.append(path)
            continue

        if re.search(r"(^|/)(tty[A-Za-z0-9_.-]+|cu\.[A-Za-z0-9_.-]+)$", path):
            candidates.append(path)

    return candidates


def enumerate_serial_ports() -> list[str]:
    """Collect available serial ports, preferring pyserial and falling back to /dev discovery."""
    ports: list[str] = []

    if list_ports is not None:
        try:
            ports.extend(port.device for port in list_ports.comports())
        except Exception:
            ports = []

    if not ports:
        for base in ("/dev", "/dev/serial/by-id", "/dev/serial/by-path"):
            base_path = Path(base)
            if not base_path.exists():
                continue
            for path in sorted(base_path.iterdir()):
                ports.append(str(path))

        for pattern in ("/dev/tty*", "/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.*"):
            ports.extend(sorted(Path().glob(pattern)))

    return filter_serial_ports(ports)


def find_daly_cli() -> str:
    """Locate the daly-bms-cli executable in the active environment or nearby venvs."""
    strict_candidates = []

    workspace_dir = Path(__file__).resolve().parent
    venv_paths = [
        workspace_dir / ".venv" / "bin" / "daly-bms-cli",
        workspace_dir / ".venv-1" / "bin" / "daly-bms-cli",
        workspace_dir / "venv" / "bin" / "daly-bms-cli",
    ]
    for candidate in venv_paths:
        if candidate.exists():
            strict_candidates.append(str(candidate))

    for candidate in strict_candidates:
        return candidate

    from_path = shutil.which("daly-bms-cli")
    if from_path:
        return from_path

    python_bin = Path(sys.executable).parent
    cli_candidate = python_bin / "daly-bms-cli"
    if cli_candidate.exists():
        return str(cli_candidate)

    raise FileNotFoundError(
        "Unable to locate daly-bms-cli. Install the dalybms package or ensure the CLI is on PATH."
    )


def parse_discover_output(output: str) -> list[int]:
    """Extract discovered BMS IDs from the CLI's human-readable discovery output."""
    ids: list[int] = []
    for line in output.splitlines():
        match = re.search(r"^\s*\[(\d+)\]\s*$", line)
        if match:
            ids.append(int(match.group(1)))
            continue

        match = re.search(r"^\s*ID\s+(\d+)\s*$", line)
        if match:
            ids.append(int(match.group(1)))

    # Preserve first-seen order while avoiding duplicates from repeated output blocks.
    ordered: list[int] = []
    seen: set[int] = set()
    for item in ids:
        if item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered


def bitmask_to_ids(bitmask: int, max_ids: int = 32) -> list[int]:
    """Convert a 32-bit unsigned bitmask to the list of BMS IDs represented by the set bits."""
    bitmask &= 0xFFFFFFFF
    ids: list[int] = []
    for bms_id in range(1, max_ids + 1):
        if bitmask & (1 << (bms_id - 1)):
            ids.append(bms_id)
    return ids


def _stream_subprocess_output(
    process: subprocess.Popen[str],
    logger: logging.Logger,
    prefix: str,
) -> tuple[str, str]:
    """Read subprocess output line-by-line and log it while the scan is still running."""
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def reader(stream, sink, label):
        for line in iter(stream.readline, ""):
            if not line:
                break
            text = line.rstrip()
            if text:
                logger.info("%s %s", prefix, text)
            sink.append(line)
        stream.close()

    stdout_thread = None
    stderr_thread = None

    if process.stdout is not None:
        stdout_thread = threading.Thread(target=reader, args=(process.stdout, stdout_lines, "STDOUT"), daemon=True)
        stdout_thread.start()

    if process.stderr is not None:
        stderr_thread = threading.Thread(target=reader, args=(process.stderr, stderr_lines, "STDERR"), daemon=True)
        stderr_thread.start()

    process.wait()

    if stdout_thread is not None:
        stdout_thread.join(timeout=2)
    if stderr_thread is not None:
        stderr_thread.join(timeout=2)

    return "".join(stdout_lines), "".join(stderr_lines)


def run_discovery_for_port(
    port: str,
    bitmask: int = DEFAULT_DISCOVER_MASK,
    timeout: int = 60,
    logger: logging.Logger | None = None,
) -> list[int]:
    """Run the DALY discovery scan against one serial port and return the discovered BMS IDs."""
    logger = logger or logging.getLogger("battery_manager")
    cli = find_daly_cli()
    cmd = [cli, "-d", port, "--discover", hex(bitmask)]
    logger.info("Scanning port %s with bitmask 0x%08X", port, bitmask)

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdout_text, stderr_text = _stream_subprocess_output(process, logger, f"[{port}]")
        if process.returncode is None:
            process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if process.poll() is None:
            process.kill()
        output = (exc.stdout or "") + (exc.stderr or "")
        logger.error("Discovery for %s timed out after %ss: %s", port, timeout, output.strip() or "no output")
        raise RuntimeError(f"Discovery timed out for {port} after {timeout}s") from exc

    combined_output = (stdout_text or "") + (stderr_text or "")
    if process.returncode not in (0, 1):
        raise RuntimeError(f"Discovery failed for {port}: {combined_output.strip() or 'unknown error'}")

    discovered = parse_discover_output(combined_output)
    if discovered:
        logger.info("Port %s discovered BMS IDs: %s", port, discovered)
    else:
        logger.info("Port %s discovered no BMS IDs", port)
    return discovered


def discover_all_ports(
    bitmask: int = DEFAULT_DISCOVER_MASK,
    ports: Sequence[str] | None = None,
    timeout: int = 60,
    logger: logging.Logger | None = None,
) -> dict[str, list[int]]:
    """Discover all BMS devices across the available serial ports."""
    logger = logger or logging.getLogger("battery_manager")
    discovered_by_port: dict[str, list[int]] = {}
    serial_ports = list(ports) if ports is not None else enumerate_serial_ports()

    logger.info("Found %d candidate serial port(s): %s", len(serial_ports), serial_ports)

    for port in serial_ports:
        try:
            found = run_discovery_for_port(port, bitmask=bitmask, timeout=timeout, logger=logger)
        except Exception as exc:  # pragma: no cover - runtime hardware-dependent path.
            discovered_by_port[port] = []
            logger.error("%s: discovery error (%s)", port, exc)
            continue
        discovered_by_port[port] = found

    return discovered_by_port


@dataclass
class PortMonitor:
    port: str
    bitmask: int = DEFAULT_DISCOVER_MASK
    timeout: int = 60
    interval: int = 15
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("battery_manager"))
    discovered: list[int] = field(default_factory=list)
    error: str | None = None
    last_scan: float | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    on_update: Callable[[], None] | None = None

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name=f"port-monitor:{self.port}", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=2)

    def restart(self) -> None:
        self.stop()
        self.start()

    def scan_once(self) -> list[int]:
        self.error = None
        try:
            found = run_discovery_for_port(self.port, bitmask=self.bitmask, timeout=self.timeout, logger=self.logger)
            self.discovered = found
            self.last_scan = time.time()
            return found
        except Exception as exc:  # pragma: no cover - runtime hardware dependent path.
            self.error = str(exc)
            self.discovered = []
            self.logger.error("%s: discovery monitor error (%s)", self.port, exc)
            return []

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.scan_once()
            if self.on_update is not None:
                self.on_update()
            self.stop_event.wait(self.interval)

    def status_line(self, port_width: int, status_width: int) -> str:
        if self.discovered:
            bms_text = ", ".join(str(item) for item in self.discovered)
            state = f"BMS: {bms_text}"
        else:
            state = "BMS: none"

        if self.error:
            state = f"ERROR: {self.error}"

        last_text = "never"
        if self.last_scan is not None:
            last_text = time.strftime("%H:%M:%S", time.localtime(self.last_scan))

        return f"{self.port:<{port_width}}  {state:<{status_width}}  {last_text}"

    def status_summary(self) -> str:
        if self.discovered:
            return f"BMS: {', '.join(str(item) for item in self.discovered)}"
        if self.error:
            return f"ERROR: {self.error}"
        return "BMS: none"


class PortRegistry:
    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("battery_manager")
        self.monitors: dict[str, PortMonitor] = {}

    def add_port(self, port: str, bitmask: int = DEFAULT_DISCOVER_MASK, timeout: int = 60, interval: int = 15) -> PortMonitor:
        if port not in self.monitors:
            monitor = PortMonitor(port=port, bitmask=bitmask, timeout=timeout, interval=interval, logger=self.logger)
            self.monitors[port] = monitor
        return self.monitors[port]

    def start_all(self) -> None:
        for monitor in self.monitors.values():
            monitor.start()

    def stop_all(self) -> None:
        for monitor in self.monitors.values():
            monitor.stop()

    def snapshot(self) -> dict[str, list[int]]:
        return {port: monitor.discovered for port, monitor in sorted(self.monitors.items())}

    def render_dashboard(self) -> str:
        if not self.monitors:
            return "\n".join([
                "Daly BMS Manager",
                "=" * 96,
                "No ports configured.",
            ])

        port_width = max(20, max(len(port) for port in self.monitors) + 2)
        status_texts = [monitor.status_summary() for monitor in self.monitors.values()]
        status_width = max(28, max(len(text) for text in status_texts) + 2)

        lines = [
            "Daly BMS Manager",
            "=" * 96,
            f"{'Port':<{port_width}}  {'Status':<{status_width}}  {'Last Scan'}",
            "-" * 96,
        ]

        for port in sorted(self.monitors):
            monitor = self.monitors[port]
            last_text = "never"
            if monitor.last_scan is not None:
                last_text = time.strftime("%H:%M:%S", time.localtime(monitor.last_scan))
            lines.append(f"{monitor.port:<{port_width}}  {monitor.status_summary():<{status_width}}  {last_text}")

        expected_ids = sorted({bms_id for monitor in self.monitors.values() for bms_id in bitmask_to_ids(monitor.bitmask)})
        discovered_ids = sorted({item for monitor in self.monitors.values() for item in monitor.discovered})
        missing_ids = [bms_id for bms_id in expected_ids if bms_id not in discovered_ids]

        lines.append("-" * 96)
        lines.append(f"Missing BMSs: {', '.join(str(item) for item in missing_ids) if missing_ids else 'none'}")
        return "\n".join(lines)


def run_dashboard(registry: PortRegistry, refresh_seconds: float = 1.0) -> None:
    """Display a persistent text dashboard that redraws in place instead of scrolling endlessly."""
    if not registry.monitors:
        print("No ports configured.")
        return

    while True:
        print("\033[H\033[J", end="")
        print(registry.render_dashboard())
        time.sleep(refresh_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover Daly BMS devices on all serial ports.")
    parser.add_argument(
        "--bitmask",
        default=f"0x{DEFAULT_DISCOVER_MASK:08X}",
        help="32-bit bitmask of BMS IDs to scan; defaults to all IDs.",
    )
    parser.add_argument(
        "--port",
        action="append",
        default=[],
        help="Explicit serial port to scan; may be supplied more than once.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout in seconds for each per-port discovery scan.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress logs while each port is being scanned.",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=15,
        help="Seconds between each port's discovery scan while the monitor is running.",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Render a persistent terminal dashboard instead of a one-time JSON dump.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("battery_manager")

    try:
        bitmask = int(args.bitmask, 0)
    except ValueError as exc:
        parser.error(f"Invalid bitmask: {args.bitmask!r} ({exc})")

    ports = args.port or enumerate_serial_ports()
    logger.info("Starting Daly BMS discovery across %d port(s)", len(ports))

    registry = PortRegistry(logger=logger)
    for port in ports:
        registry.add_port(port, bitmask=bitmask, timeout=args.timeout, interval=args.scan_interval)

    if args.dashboard:
        try:
            registry.start_all()
            run_dashboard(registry, refresh_seconds=1.0)
        finally:
            registry.stop_all()
        return 0

    discovered = discover_all_ports(bitmask=bitmask, ports=ports, timeout=args.timeout, logger=logger)
    print(json.dumps(discovered, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
