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
from pathlib import Path
from typing import Iterable, Sequence

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

    discovered = discover_all_ports(bitmask=bitmask, ports=ports, timeout=args.timeout, logger=logger)
    print(json.dumps(discovered, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
