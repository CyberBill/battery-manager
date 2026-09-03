#!/usr/bin/env python3
"""High-level manager for discovering and monitoring Daly BMS units across multiple serial buses."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

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


def parse_discover_output(output: str) -> list[int]:
    """Backward-compatible helper retained for tests and older callers; direct library discovery no longer parses CLI text."""
    ids: list[int] = []
    for line in output.splitlines():
        match = re.search(r"^\s*\[(\d+)\]\s*$", line)
        if match:
            ids.append(int(match.group(1)))
            continue

        match = re.search(r"^\s*ID\s+(\d+)\s*$", line)
        if match:
            ids.append(int(match.group(1)))

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


def sanitize_identifier(value: object) -> str:
    """Convert a serial number or label into a safe MQTT and Home Assistant identifier."""
    return re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_")


def build_mqtt_device_identity(
    serial_number: str | None,
    *,
    mqtt_topic_root: str | None = None,
) -> tuple[str, str, str]:
    """Return the canonical MQTT and Home Assistant identity for a BMS.

    This keeps the identity stable across restarts and matches the upstream Daly CLI naming.
    When no serial number is available, a safe fallback keeps the legacy generic name.
    """
    serial_value = str(serial_number).strip() if serial_number is not None else ""
    sanitized_serial = sanitize_identifier(serial_value)

    if sanitized_serial:
        device_id = f"daly_{sanitized_serial}"
        device_name = f"Daly BMS {serial_value}"
        topic_root = mqtt_topic_root.rstrip("/") if mqtt_topic_root else f"battery/daly/{serial_value}"
        return device_id, device_name, topic_root

    device_id = "daly_bms"
    device_name = "Daly BMS"
    topic_root = mqtt_topic_root.rstrip("/") if mqtt_topic_root else "daly_bms"
    return device_id, device_name, topic_root


def build_mqtt_hass_config_discovery(base: str, *, device_id: str, device_name: str, topic_root: str, serial_number: str | None) -> tuple[str, str]:
    """Build a Home Assistant discovery payload using the library's MQTT adapter."""
    from dalybms import DalyBMSMQTT

    mqtt_adapter = DalyBMSMQTT(
        device_id=device_id,
        device_name=device_name,
        topic_root=topic_root,
        serial_number=serial_number,
        logger=logging.getLogger("battery_manager"),
    )
    message = mqtt_adapter.build_hass_config_discovery(base)
    return message.topic, message.payload


def mqtt_single_out(mqtt_client: Any, logger: logging.Logger, topic: str, data: object, retain: bool = False) -> None:
    """Publish one MQTT payload using the Paho client."""
    publish_result = mqtt_client.publish(topic, data, qos=1, retain=retain)
    publish_result.wait_for_publish()
    if publish_result.rc != 0:
        raise RuntimeError(f"MQTT publish failed for topic {topic}; result code: {publish_result.rc}")
    logger.debug("Published %s -> %s", topic, data)


def mqtt_iterator(result: dict[str, Any], *, mqtt_client: Any, logger: logging.Logger, topic_root: str, device_id: str, device_name: str, serial_number: str | None, mqtt_hass: bool = True, base: str = "") -> None:
    """Publish Daly data via the library's MQTT adapter so naming stays consistent with the CLI."""
    from dalybms import DalyBMSMQTT

    mqtt_adapter = DalyBMSMQTT(
        device_id=device_id,
        device_name=device_name,
        topic_root=topic_root,
        serial_number=serial_number,
        logger=logger,
    )
    mqtt_adapter.publish(
        mqtt_client,
        result,
        include_hass_discovery=mqtt_hass,
        add_last_active_utc=False,
    )


def publish_bms_report_via_library(
    port: str,
    bms_id: int,
    *,
    mqtt_broker: str,
    mqtt_user: str,
    mqtt_password: str,
    mqtt_port: int = 1883,
    logger: logging.Logger | None = None,
) -> dict[str, object]:
    """Connect to the serial port, read the Daly data directly, and publish it to MQTT via the library API."""
    logger = logger or logging.getLogger("battery_manager")
    try:
        from dalybms import DalyBMS
        import paho.mqtt.client as paho
    except ImportError as exc:  # pragma: no cover - environment-specific dependency.
        raise RuntimeError("The dalybms and paho-mqtt Python packages are required for MQTT publishing.") from exc

    bms = DalyBMS(request_retries=3, address=4, bms_id=bms_id, logger=logger)
    try:
        bms.connect(device=port, timeout=0.5)
        serial_number = bms.get_serial_number()
        if not serial_number:
            raise RuntimeError(f"Unable to read serial number for BMS {bms_id} on {port}; MQTT identity cannot be created.")

        device_id, device_name, topic_root = build_mqtt_device_identity(serial_number)

        mqtt_client = paho.Client()
        mqtt_client.username_pw_set(mqtt_user, mqtt_password)
        mqtt_client.connect(mqtt_broker, port=mqtt_port)
        mqtt_client.loop_start()
        try:
            result = bms.get_all()
            if not isinstance(result, dict):
                raise RuntimeError(f"Unexpected Daly payload for BMS {bms_id}: {result!r}")

            from dalybms import DalyBMSMQTT

            mqtt_adapter = DalyBMSMQTT(
                device_id=device_id,
                device_name=device_name,
                topic_root=topic_root,
                serial_number=serial_number,
                logger=logger,
            )
            mqtt_adapter.publish(
                mqtt_client,
                result,
                include_hass_discovery=True,
                add_last_active_utc=True,
            )
            return {"bms_id": bms_id, "status": "ok", "serial_number": serial_number, "topic_root": topic_root}
        finally:
            mqtt_client.disconnect()
            mqtt_client.loop_stop()
    finally:
        if getattr(bms, "serial", None) is not None and bms.serial.is_open:
            bms.disconnect()


def discover_port_via_library(
    port: str,
    bitmask: int = DEFAULT_DISCOVER_MASK,
    timeout: int = 60,
    logger: logging.Logger | None = None,
) -> list[int]:
    """Discover Daly BMS units on one serial port using the DalyBMS library directly."""
    logger = logger or logging.getLogger("battery_manager")
    probe_logger = logging.getLogger("battery_manager.discovery_probe")
    probe_logger.setLevel(logging.CRITICAL)
    probe_logger.propagate = False
    logger.info("Scanning port %s with bitmask 0x%08X via DalyBMS library", port, bitmask)

    try:
        from dalybms import DalyBMS
    except ImportError as exc:  # pragma: no cover - dependency-specific path.
        raise RuntimeError("The dalybms package is required for direct discovery and publishing.") from exc

    discovered: list[int] = []
    scan_timeout = 0.05
    for bms_id in bitmask_to_ids(bitmask):
        bms = DalyBMS(request_retries=1, address=4, bms_id=bms_id, logger=probe_logger)
        try:
            bms.connect(device=port, timeout=scan_timeout)
            serial_obj = getattr(bms, "serial", None)
            if serial_obj is not None:
                serial_obj.timeout = scan_timeout
                serial_obj.writeTimeout = scan_timeout
            try:
                board_info = bms.get_board_info()
            except Exception:
                board_info = False
            if board_info:
                discovered.append(bms_id)
                logger.info("Port %s discovered BMS ID %s", port, bms_id)
        except Exception as exc:  # pragma: no cover - hardware-dependent path.
            logger.debug("No response from BMS ID %s on %s: %s", bms_id, port, exc)
        finally:
            serial_obj = getattr(bms, "serial", None)
            if serial_obj is not None and getattr(serial_obj, "is_open", False):
                bms.disconnect()

    return discovered


def run_discovery_for_port(
    port: str,
    bitmask: int = DEFAULT_DISCOVER_MASK,
    timeout: int = 60,
    logger: logging.Logger | None = None,
) -> list[int]:
    """Backward-compatible alias for direct library discovery."""
    return discover_port_via_library(port, bitmask=bitmask, timeout=timeout, logger=logger)


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

    def publish_report(self, mqtt_config: dict[str, object] | None = None) -> list[dict[str, object]]:
        """Run the Daly library report for each discovered BMS on this port and return the results."""
        if not self.discovered:
            return []

        mqtt_enabled = bool(mqtt_config and mqtt_config.get("enabled", True))
        results: list[dict[str, object]] = []
        for bms_id in self.discovered:
            try:
                if not mqtt_enabled:
                    self.logger.warning("MQTT publishing disabled for %s; skipping report for BMS %s", self.port, bms_id)
                    results.append({"bms_id": bms_id, "status": "mqtt_disabled"})
                    continue

                mqtt_broker = mqtt_config.get("broker") if mqtt_config else None
                mqtt_user = mqtt_config.get("user") if mqtt_config else None
                mqtt_password = mqtt_config.get("password") if mqtt_config else None
                if not mqtt_broker or not mqtt_user or not mqtt_password:
                    raise ValueError(
                        "MQTT publishing is enabled but broker, user, and password are required. "
                        "Set --mqtt-broker, --mqtt-user, and --mqtt-password."
                    )

                self.logger.info("Publishing report for port %s BMS %s via direct Daly library", self.port, bms_id)
                payload = publish_bms_report_via_library(
                    self.port,
                    bms_id,
                    mqtt_broker=str(mqtt_broker),
                    mqtt_user=str(mqtt_user),
                    mqtt_password=str(mqtt_password),
                    mqtt_port=int(mqtt_config.get("port", 1883)) if mqtt_config else 1883,
                    logger=self.logger,
                )
                results.append({"bms_id": bms_id, "status": "ok", "payload": payload})
            except Exception as exc:  # pragma: no cover - hardware-dependent path.
                self.logger.error("Failed to publish BMS %s on %s: %s", bms_id, self.port, exc)
                results.append({"bms_id": bms_id, "status": "error", "error": str(exc)})

        return results

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

    def render_dashboard(self, mqtt_status: str | None = None) -> str:
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
        ]
        if mqtt_status is not None:
            lines.append(mqtt_status)
        lines.extend([
            f"{'Port':<{port_width}}  {'Status':<{status_width}}  {'Last Scan'}",
            "-" * 96,
        ])

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


def bind_dashboard_monitor_updates(registry: PortRegistry, mqtt_config: dict[str, object] | None = None) -> None:
    """Attach each monitor update callback to MQTT publication when MQTT credentials are available."""
    if not registry.monitors:
        return

    for monitor in registry.monitors.values():
        def on_update(monitor_ref: PortMonitor = monitor, mqtt_cfg: dict[str, object] | None = mqtt_config):
            if not mqtt_cfg:
                return
            mqtt_ready = bool(mqtt_cfg.get("enabled", True))
            if not mqtt_ready:
                return
            if not monitor_ref.discovered:
                return
            mqtt_cfg_for_call = {
                "enabled": True,
                "broker": mqtt_cfg.get("broker"),
                "user": mqtt_cfg.get("user"),
                "password": mqtt_cfg.get("password"),
                "port": mqtt_cfg.get("port", 1883),
            }
            monitor_ref.publish_report(mqtt_cfg_for_call)

        monitor.on_update = on_update


def get_missing_mqtt_fields(mqtt_broker: str | None, mqtt_user: str | None, mqtt_password: str | None) -> list[str]:
    """Return the required MQTT CLI flags that are missing so the user knows why publishing will be disabled."""
    return [
        name for name, value in {
            "--mqtt-broker": mqtt_broker,
            "--mqtt-user": mqtt_user,
            "--mqtt-password": mqtt_password,
        }.items() if not value
    ]


def should_quit_dashboard_key(key: str | None) -> bool:
    """Return True when a quit key or Ctrl+C interrupt is detected in dashboard mode."""
    if key is None:
        return False
    return key.lower() in {"q", "\x1b", "\x03"}


def _read_dashboard_key(timeout_seconds: float) -> str | None:
    """Read a single keypress while the dashboard is refreshing, or return None on timeout."""
    if not sys.stdin or not hasattr(sys.stdin, "fileno"):
        return None

    try:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
            if not ready:
                return None
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except (AttributeError, OSError, termios.error):
        return None


def run_dashboard(
    registry: PortRegistry,
    refresh_seconds: float = 1.0,
    mqtt_status: str | None = None,
) -> None:
    """Display a persistent text dashboard that redraws in place and exits cleanly on q/Escape/Ctrl+C."""
    if not registry.monitors:
        print("No ports configured.")
        return

    while True:
        print("\033[H\033[J", end="")
        print(registry.render_dashboard(mqtt_status=mqtt_status))
        key = _read_dashboard_key(refresh_seconds)
        if should_quit_dashboard_key(key):
            return


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
    parser.add_argument(
        "--mqtt-broker",
        type=str,
        default=None,
        help="MQTT broker hostname or IP for Daly BMS publishing.",
    )
    parser.add_argument(
        "--mqtt-user",
        type=str,
        default=None,
        help="MQTT username to authenticate Daly BMS publishing.",
    )
    parser.add_argument(
        "--mqtt-password",
        type=str,
        default=None,
        help="MQTT password to authenticate Daly BMS publishing.",
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

    missing_mqtt_fields = get_missing_mqtt_fields(args.mqtt_broker, args.mqtt_user, args.mqtt_password)
    mqtt_status = (
        "MQTT: enabled"
        if not missing_mqtt_fields
        else f"MQTT: disabled (missing: {', '.join(missing_mqtt_fields)})"
    )
    if missing_mqtt_fields:
        logger.warning(
            "MQTT publishing is disabled because these required values are missing: %s. "
            "Set all of them to enable MQTT publishing.",
            ", ".join(missing_mqtt_fields),
        )

    if args.dashboard:
        try:
            mqtt_config = None
            if not missing_mqtt_fields:
                mqtt_config = {
                    "enabled": True,
                    "broker": args.mqtt_broker,
                    "user": args.mqtt_user,
                    "password": args.mqtt_password,
                    "port": 1883,
                }
            bind_dashboard_monitor_updates(registry, mqtt_config)
            registry.start_all()
            run_dashboard(registry, refresh_seconds=1.0, mqtt_status=mqtt_status)
        except KeyboardInterrupt:
            logger.info("Dashboard shutdown requested; stopping all monitors.")
        finally:
            registry.stop_all()
        return 0

    discovered = discover_all_ports(bitmask=bitmask, ports=ports, timeout=args.timeout, logger=logger)

    if not missing_mqtt_fields:
        logger.info("Publishing to MQTT broker %s for each discovered BMS", args.mqtt_broker)
        for port, found in discovered.items():
            monitor = registry.monitors.get(port)
            if monitor is None:
                monitor = PortMonitor(port=port, bitmask=bitmask, timeout=args.timeout, interval=args.scan_interval, logger=logger)
                monitor.discovered = found
                registry.monitors[port] = monitor
            mqtt_config = {
                "enabled": True,
                "broker": args.mqtt_broker,
                "user": args.mqtt_user,
                "password": args.mqtt_password,
            }
            monitor.publish_report(mqtt_config)

    print(json.dumps(discovered, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
