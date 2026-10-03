#!/usr/bin/env python3
"""High-level manager for discovering and monitoring Daly BMS units across multiple serial buses."""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

try:
    import serial.tools.list_ports as list_ports
except ImportError:  # pragma: no cover - pyserial is required for runtime use.
    list_ports = None


DALY_BMS_MAX_ID = 16
DEFAULT_DISCOVER_MASK = (1 << DALY_BMS_MAX_ID) - 1
DEFAULT_EXCLUDED_PORT_PATTERNS = ("/dev/ttyAMA*", "/dev/ttyS*")
DEFAULT_BATTERY_CONFIG_PATH = Path(__file__).with_name("battery-config.json")
DEFAULT_SAFETY_CONFIG_PATH = Path(__file__).with_name("safety-config.json")


@dataclass(frozen=True)
class BatteryMetadata:
    """Physical location and identifier assigned to one BMS serial number."""

    serial: str
    id: int
    cabinet: int | None = None
    row: int | None = None
    depth: int | None = None

    def mqtt_payload(self) -> dict[str, int]:
        payload = {"id": self.id}
        for name in ("cabinet", "row", "depth"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True)
class BatteryConfiguration:
    """Battery metadata indexed by the serial number reported by the BMS."""

    batteries_by_serial: dict[str, BatteryMetadata]

    def find_battery(self, serial_number: object) -> BatteryMetadata | None:
        return self.batteries_by_serial.get(str(serial_number).strip())


@dataclass(frozen=True)
class SafetyConfiguration:
    """Per-BMS limits used to decide whether a report is safe."""

    max_temperature_c: float
    max_cell_voltage_spread_v: float
    min_pack_voltage_v: float
    max_pack_voltage_v: float
    min_cell_voltage_v: float
    max_cell_voltage_v: float


def _require_config_integer(entry: dict[str, object], field_name: str, position: int, *, required: bool) -> int | None:
    value = entry.get(field_name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        requirement = "an integer" if required else "an integer when provided"
        raise ValueError(f"batteries[{position}].{field_name} must be {requirement}.")
    return value


def load_battery_configuration(path: str | Path = DEFAULT_BATTERY_CONFIG_PATH) -> BatteryConfiguration:
    """Load BMS metadata from the JSON configuration file used for MQTT publishing."""
    config_path = Path(path)
    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Battery configuration {config_path} contains invalid JSON: {exc.msg}.") from exc

    if not isinstance(raw_config, dict) or not isinstance(raw_config.get("batteries"), list):
        raise ValueError(f"Battery configuration {config_path} must contain a 'batteries' list.")

    batteries_by_serial: dict[str, BatteryMetadata] = {}
    for position, raw_battery in enumerate(raw_config["batteries"]):
        if not isinstance(raw_battery, dict):
            raise ValueError(f"batteries[{position}] must be an object.")

        serial = raw_battery.get("serial")
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError(f"batteries[{position}].serial must be a non-empty string.")
        serial = serial.strip()
        if serial in batteries_by_serial:
            raise ValueError(f"Battery configuration contains duplicate serial number {serial!r}.")

        battery_id = _require_config_integer(raw_battery, "id", position, required=True)
        assert battery_id is not None
        batteries_by_serial[serial] = BatteryMetadata(
            serial=serial,
            id=battery_id,
            cabinet=_require_config_integer(raw_battery, "cabinet", position, required=False),
            row=_require_config_integer(raw_battery, "row", position, required=False),
            depth=_require_config_integer(raw_battery, "depth", position, required=False),
        )

    return BatteryConfiguration(batteries_by_serial=batteries_by_serial)


def _require_config_number(entry: dict[str, object], field_name: str) -> float:
    value = entry.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Safety configuration field {field_name!r} must be a number.")
    return float(value)


def load_safety_configuration(path: str | Path = DEFAULT_SAFETY_CONFIG_PATH) -> SafetyConfiguration:
    """Load BMS safety limits from JSON."""
    config_path = Path(path)
    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Safety configuration {config_path} contains invalid JSON: {exc.msg}.") from exc

    if not isinstance(raw_config, dict):
        raise ValueError(f"Safety configuration {config_path} must be a JSON object.")

    return SafetyConfiguration(
        max_temperature_c=_require_config_number(raw_config, "max_temperature_c"),
        max_cell_voltage_spread_v=_require_config_number(raw_config, "max_cell_voltage_spread_v"),
        min_pack_voltage_v=_require_config_number(raw_config, "min_pack_voltage_v"),
        max_pack_voltage_v=_require_config_number(raw_config, "max_pack_voltage_v"),
        min_cell_voltage_v=_require_config_number(raw_config, "min_cell_voltage_v"),
        max_cell_voltage_v=_require_config_number(raw_config, "max_cell_voltage_v"),
    )


def normalize_discover_mask(bitmask: int, max_ids: int = DALY_BMS_MAX_ID) -> int:
    """Clip a bitmask to Daly's supported BMS ID range (1..16)."""
    if max_ids <= 0:
        return 0
    return bitmask & ((1 << max_ids) - 1)


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


def port_identity_candidates(port: str) -> set[str]:
    """Return all relevant path and stable-ID aliases for a serial port."""
    candidates: set[str] = set()
    if not port:
        return candidates

    path = str(port).strip()
    for value in (path, Path(path).name, Path(path).stem):
        if value:
            candidates.add(value)

    try:
        resolved = str(Path(path).resolve(strict=False))
    except Exception:
        resolved = path
    for value in (resolved, Path(resolved).name, Path(resolved).stem):
        if value:
            candidates.add(value)

    for base in ("/dev/serial/by-id", "/dev/serial/by-path"):
        base_path = Path(base)
        if not base_path.exists():
            continue
        for entry in sorted(base_path.iterdir()):
            try:
                target = os.path.realpath(str(entry))
            except OSError:
                continue
            if target == path or target == resolved:
                candidates.add(entry.name)
                candidates.add(str(entry))
                candidates.add(entry.stem)

    return candidates


def is_excluded_port(
    port: str,
    *,
    excluded_ports: Sequence[str] | None = None,
    excluded_port_ids: Sequence[str] | None = None,
    excluded_patterns: Sequence[str] | None = None,
) -> bool:
    """Return True when a port path or stable serial ID is explicitly excluded."""
    if not port:
        return False

    excluded_ports = {str(item).strip() for item in (excluded_ports or ()) if str(item).strip()}
    excluded_port_ids = {str(item).strip() for item in (excluded_port_ids or ()) if str(item).strip()}
    excluded_patterns = tuple(excluded_patterns or DEFAULT_EXCLUDED_PORT_PATTERNS)

    path = str(port).strip()
    if path in excluded_ports:
        return True

    if any(path.startswith(pattern.rstrip("*")) for pattern in excluded_patterns if pattern.endswith("*")):
        return True

    if any(fnmatch.fnmatch(path, pattern) for pattern in excluded_patterns):
        return True

    candidate_ids = port_identity_candidates(path)
    if any(item in excluded_port_ids for item in candidate_ids):
        return True

    return False


def enumerate_serial_ports(
    *,
    excluded_ports: Sequence[str] | None = None,
    excluded_port_ids: Sequence[str] | None = None,
    excluded_patterns: Sequence[str] | None = None,
) -> list[str]:
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

    filtered = filter_serial_ports(ports)
    return [
        port for port in filtered
        if not is_excluded_port(
            port,
            excluded_ports=excluded_ports,
            excluded_port_ids=excluded_port_ids,
            excluded_patterns=excluded_patterns,
        )
    ]


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


def bitmask_to_ids(bitmask: int, max_ids: int = DALY_BMS_MAX_ID) -> list[int]:
    """Convert a Daly-compatible bitmask to the list of BMS IDs represented by the set bits."""
    bitmask = normalize_discover_mask(bitmask, max_ids=max_ids)
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


def build_bms_management_payload(
    battery_config: BatteryConfiguration,
    serial_number: object,
    safety_result: tuple[bool, str],
) -> dict[str, object]:
    """Return configured metadata, safety state, and Home Assistant unknown values."""
    battery = battery_config.find_battery(serial_number)
    payload: dict[str, object] = {
        "id": "None",
        "cabinet": "None",
        "row": "None",
        "depth": "None",
    }
    if battery is not None:
        payload.update(battery.mqtt_payload())
    payload.update({
        "safe": safety_result[0],
        "safety_reason": safety_result[1],
    })
    return payload


def _payload_number(payload: dict[str, Any], section: str, field_name: str) -> float | None:
    values = payload.get(section)
    if not isinstance(values, dict):
        return None
    return _coerce_number(values.get(field_name))


def evaluate_bms_safety(payload: dict[str, Any], safety_config: SafetyConfiguration) -> tuple[bool, str]:
    """Evaluate a complete Daly payload against the configured per-BMS safety limits."""
    temperature = _payload_number(payload, "temperature_range", "highest_temperature")
    if temperature is None:
        return False, "Missing highest battery temperature"
    if temperature > safety_config.max_temperature_c:
        return False, f"Battery temperature is {temperature:g} C (limit {safety_config.max_temperature_c:g} C)"

    highest_voltage = _payload_number(payload, "cell_voltage_range", "highest_voltage")
    lowest_voltage = _payload_number(payload, "cell_voltage_range", "lowest_voltage")
    if highest_voltage is None or lowest_voltage is None:
        return False, "Missing cell voltage range"
    voltage_spread = highest_voltage - lowest_voltage
    if voltage_spread > safety_config.max_cell_voltage_spread_v:
        return False, f"Cell voltage spread is {voltage_spread:.3f} V (limit {safety_config.max_cell_voltage_spread_v:.3f} V)"

    pack_voltage = _payload_number(payload, "soc", "total_voltage")
    if pack_voltage is None:
        return False, "Missing pack voltage"
    if pack_voltage < safety_config.min_pack_voltage_v:
        return False, f"Pack voltage is {pack_voltage:g} V (minimum {safety_config.min_pack_voltage_v:g} V)"
    if pack_voltage > safety_config.max_pack_voltage_v:
        return False, f"Pack voltage is {pack_voltage:g} V (maximum {safety_config.max_pack_voltage_v:g} V)"

    if lowest_voltage < safety_config.min_cell_voltage_v:
        return False, f"Cell voltage is below safety limit: lowest cell is {lowest_voltage:.3f} V (minimum {safety_config.min_cell_voltage_v:.3f} V)"
    if highest_voltage > safety_config.max_cell_voltage_v:
        return False, f"Cell voltage exceeds safety limit: highest cell is {highest_voltage:.3f} V"

    return True, "OK"


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


def _coerce_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(Decimal(text))
        except (InvalidOperation, ValueError):
            return None
    return None


def summarize_cumulative_battery_data(payloads_by_source: dict[str, dict[str, Any]]) -> dict[str, object]:
    lowest_voltage_readings: list[float] = []
    highest_voltage_readings: list[float] = []
    voltage_summary_count = 0

    for payload in payloads_by_source.values():
        voltage_range = payload.get("cell_voltage_range")
        if not isinstance(voltage_range, dict):
            continue

        lowest_voltage = _coerce_number(voltage_range.get("lowest_voltage"))
        highest_voltage = _coerce_number(voltage_range.get("highest_voltage"))
        if lowest_voltage is not None:
            lowest_voltage_readings.append(lowest_voltage)
        if highest_voltage is not None:
            highest_voltage_readings.append(highest_voltage)
        if lowest_voltage is not None and highest_voltage is not None:
            voltage_summary_count += 1

    summary: dict[str, object] = {
        "sources": len(payloads_by_source),
        "voltage_summaries": voltage_summary_count,
    }
    if lowest_voltage_readings:
        summary["min_cell_voltage"] = min(lowest_voltage_readings)
    if highest_voltage_readings:
        summary["max_cell_voltage"] = max(highest_voltage_readings)
    return summary


def publish_bms_report_via_library(
    port: str,
    bms_id: int,
    *,
    mqtt_broker: str,
    mqtt_user: str,
    mqtt_password: str,
    mqtt_port: int = 1883,
    battery_config: BatteryConfiguration,
    safety_config: SafetyConfiguration,
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
            safety_result = evaluate_bms_safety(result, safety_config)
            management_payload = build_bms_management_payload(battery_config, serial_number, safety_result)
            if battery_config.find_battery(serial_number) is None:
                logger.warning("No battery configuration entry found for BMS serial number %s.", serial_number)
            payload = {**result, **management_payload}
            mqtt_adapter.publish(
                mqtt_client,
                payload,
                include_hass_discovery=True,
                add_last_active_utc=True,
            )
            return {
                "bms_id": bms_id,
                "status": "ok",
                "serial_number": serial_number,
                "topic_root": topic_root,
                "payload": payload,
            }
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
    seen_ids: set[int] = set()
    saw_io_failure = False
    scan_timeout = 0.05
    for bms_id in bitmask_to_ids(bitmask):
        if bms_id in seen_ids:
            logger.debug("Skipping BMS ID %s on %s because it was already discovered on this bus.", bms_id, port)
            continue

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
                seen_ids.add(bms_id)
                discovered.append(bms_id)
                logger.info("Port %s discovered BMS ID %s", port, bms_id)
        except Exception as exc:  # pragma: no cover - hardware-dependent path.
            saw_io_failure = True
            logger.debug("No response from BMS ID %s on %s: %s", bms_id, port, exc)
        finally:
            serial_obj = getattr(bms, "serial", None)
            if serial_obj is not None and getattr(serial_obj, "is_open", False):
                bms.disconnect()

    if not discovered and saw_io_failure:
        raise RuntimeError(f"Port {port} is not responding to Daly discovery; serial I/O failed for every candidate ID.")

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
    battery_config: BatteryConfiguration = field(default_factory=lambda: BatteryConfiguration({}))
    safety_config: SafetyConfiguration | None = None
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
            self.logger.warning("%s: discovery monitor error (%s)", self.port, exc)
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
                    battery_config=self.battery_config,
                    safety_config=self.safety_config or load_safety_configuration(),
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
            return "ERROR"
        return "BMS: none"


class PortRegistry:
    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        battery_config: BatteryConfiguration | None = None,
        safety_config: SafetyConfiguration | None = None,
        excluded_ports: Sequence[str] | None = None,
        excluded_port_ids: Sequence[str] | None = None,
        excluded_patterns: Sequence[str] | None = None,
    ):
        self.logger = logger or logging.getLogger("battery_manager")
        self.battery_config = battery_config or BatteryConfiguration({})
        self.safety_config = safety_config
        self.monitors: dict[str, PortMonitor] = {}
        self.bms_payloads: dict[str, dict[str, Any]] = {}
        self._mqtt_config: dict[str, object] | None = None
        self.excluded_ports = tuple(excluded_ports or ())
        self.excluded_port_ids = tuple(excluded_port_ids or ())
        self.excluded_patterns = tuple(excluded_patterns or DEFAULT_EXCLUDED_PORT_PATTERNS)

    def record_bms_payload(self, source: str, payload: dict[str, Any]) -> None:
        self.bms_payloads[source] = payload

    def cumulative_battery_summary(self) -> dict[str, object]:
        return summarize_cumulative_battery_data(self.bms_payloads)

    def add_port(self, port: str, bitmask: int = DEFAULT_DISCOVER_MASK, timeout: int = 60, interval: int = 15) -> PortMonitor:
        if is_excluded_port(
            port,
            excluded_ports=self.excluded_ports,
            excluded_port_ids=self.excluded_port_ids,
            excluded_patterns=self.excluded_patterns,
        ):
            self.logger.debug("Skipping excluded serial port %s", port)
            raise ValueError(f"Port {port} is excluded from scanning.")
        if port not in self.monitors:
            monitor = PortMonitor(
                port=port,
                battery_config=self.battery_config,
                safety_config=self.safety_config,
                bitmask=bitmask,
                timeout=timeout,
                interval=interval,
                logger=self.logger,
            )
            self.monitors[port] = monitor
            monitor.start()
            if self._mqtt_config is not None:
                monitor.on_update = _make_mqtt_update_callback(monitor, self, self._mqtt_config)
        return self.monitors[port]

    def refresh_ports(
        self,
        bitmask: int = DEFAULT_DISCOVER_MASK,
        timeout: int = 60,
        interval: int = 15,
    ) -> set[str]:
        """Re-enumerate serial ports so USB hot-plug events add or remove monitors automatically."""
        current_ports = set(
            enumerate_serial_ports(
                excluded_ports=self.excluded_ports,
                excluded_port_ids=self.excluded_port_ids,
                excluded_patterns=self.excluded_patterns,
            )
        )
        present_ports = set(self.monitors)

        for port in sorted(current_ports - present_ports):
            self.logger.info("Detected new serial port %s; adding monitor.", port)
            self.add_port(port, bitmask=bitmask, timeout=timeout, interval=interval)

        for port in sorted(present_ports - current_ports):
            self.logger.warning("Serial port %s disappeared; removing monitor.", port)
            monitor = self.monitors.pop(port)
            monitor.stop()

        if self._mqtt_config is not None:
            for monitor in self.monitors.values():
                monitor.on_update = _make_mqtt_update_callback(monitor, self, self._mqtt_config)

        return set(self.monitors)

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
        summary = self.cumulative_battery_summary()
        lines.append("Cumulative Battery Data")
        lines.append(f"  Sources: {summary.get('sources', 0)}   Voltage summaries: {summary.get('voltage_summaries', 0)}")
        if "min_cell_voltage" in summary and "max_cell_voltage" in summary:
            lines.append(
                f"  Min cell voltage: {summary['min_cell_voltage']:.3f} V   Max cell voltage: {summary['max_cell_voltage']:.3f} V"
            )
        else:
            lines.append("  Min cell voltage: n/a   Max cell voltage: n/a")
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

        return "\n".join(lines)


def _make_mqtt_update_callback(
    monitor: PortMonitor,
    registry: PortRegistry,
    mqtt_config: dict[str, object] | None,
) -> Callable[[], None]:
    def on_update() -> None:
        if not mqtt_config:
            return
        mqtt_ready = bool(mqtt_config.get("enabled", True))
        if not mqtt_ready:
            return
        if not monitor.discovered:
            return
        mqtt_cfg_for_call = {
            "enabled": True,
            "broker": mqtt_config.get("broker"),
            "user": mqtt_config.get("user"),
            "password": mqtt_config.get("password"),
            "port": mqtt_config.get("port", 1883),
        }
        publish_results = monitor.publish_report(mqtt_cfg_for_call) or []
        for result in publish_results:
            payload = result.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
                payload = payload["payload"]
            bms_id = result.get("bms_id")
            if isinstance(payload, dict) and bms_id is not None:
                registry.record_bms_payload(f"{monitor.port}:{bms_id}", payload)

    return on_update


def bind_dashboard_monitor_updates(registry: PortRegistry, mqtt_config: dict[str, object] | None = None) -> None:
    """Attach each monitor update callback to MQTT publication when MQTT credentials are available."""
    registry._mqtt_config = mqtt_config
    if not registry.monitors:
        return

    for monitor in registry.monitors.values():
        monitor.on_update = _make_mqtt_update_callback(monitor, registry, mqtt_config)


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
        registry.refresh_ports()
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
    parser.add_argument(
        "--battery-config",
        type=Path,
        default=DEFAULT_BATTERY_CONFIG_PATH,
        help="Path to the JSON file containing BMS serial, ID, and location metadata.",
    )
    parser.add_argument(
        "--safety-config",
        type=Path,
        default=DEFAULT_SAFETY_CONFIG_PATH,
        help="Path to the JSON file containing per-BMS safety limits.",
    )
    parser.add_argument(
        "--exclude-port",
        action="append",
        default=[],
        help="Serial path to ignore. May be provided more than once.",
    )
    parser.add_argument(
        "--exclude-port-id",
        action="append",
        default=[],
        help="Stable serial ID to ignore. May be provided more than once.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("battery_manager")

    try:
        battery_config = load_battery_configuration(args.battery_config)
        safety_config = load_safety_configuration(args.safety_config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    logger.info("Loaded metadata for %d battery serial number(s) from %s", len(battery_config.batteries_by_serial), args.battery_config)

    try:
        bitmask = normalize_discover_mask(int(args.bitmask, 0))
    except ValueError as exc:
        parser.error(f"Invalid bitmask: {args.bitmask!r} ({exc})")

    excluded_ports = list(dict.fromkeys(args.exclude_port or []))
    excluded_port_ids = list(dict.fromkeys(args.exclude_port_id or []))
    ports = args.port or enumerate_serial_ports(
        excluded_ports=excluded_ports,
        excluded_port_ids=excluded_port_ids,
    )
    if args.port:
        ports = [
            port for port in args.port
            if not is_excluded_port(
                port,
                excluded_ports=excluded_ports,
                excluded_port_ids=excluded_port_ids,
            )
        ]
    logger.info("Starting Daly BMS discovery across %d port(s)", len(ports))

    registry = PortRegistry(
        logger=logger,
        battery_config=battery_config,
        safety_config=safety_config,
        excluded_ports=excluded_ports,
        excluded_port_ids=excluded_port_ids,
    )
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
                monitor = PortMonitor(
                    port=port,
                    battery_config=battery_config,
                    safety_config=safety_config,
                    bitmask=bitmask,
                    timeout=args.timeout,
                    interval=args.scan_interval,
                    logger=logger,
                )
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
