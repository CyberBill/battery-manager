"""Aggregate configured parallel Daly BMS units into one virtual battery model.

This module deliberately does not implement CAN transport.  It provides the normalized
model and MQTT-facing payload needed by a future EG4/Luxpower CAN encoder.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Any, Iterable

UNKNOWN_MQTT_VALUE = "None"


@dataclass(frozen=True)
class VirtualBattery:
    """Aggregate state for a configured, physically installed parallel battery bank."""

    identifier: str
    member_count: int
    configured_member_count: int
    safe: bool
    safety_reason: str
    charge_enabled: bool
    discharge_enabled: bool
    pack_voltage_v: float | None
    pack_current_a: float | None
    state_of_charge_percent: float | None
    state_of_health_percent: float | None
    capacity_ah: float | None
    remaining_capacity_ah: float | None
    charge_voltage_limit_v: float | None
    charge_current_limit_a: float | None
    discharge_voltage_limit_v: float | None
    discharge_current_limit_a: float | None
    minimum_cell_voltage_v: float | None
    maximum_cell_voltage_v: float | None
    minimum_temperature_c: float | None
    maximum_temperature_c: float | None
    cycle_count: int | None

    def mqtt_payload(self) -> dict[str, object]:
        """Return a flat payload compatible with the Daly MQTT discovery adapter."""
        payload = asdict(self)
        return {
            key: UNKNOWN_MQTT_VALUE if value is None else value
            for key, value in payload.items()
        }

    def dashboard_lines(self) -> list[str]:
        """Return concise human-readable status lines for the manager dashboard."""
        voltage = _format_value(self.pack_voltage_v, "V", 2)
        current = _format_value(self.pack_current_a, "A", 1)
        soc = _format_value(self.state_of_charge_percent, "%", 1)
        capacity = _format_value(self.capacity_ah, "Ah", 1)
        cell_range = f"{_format_value(self.minimum_cell_voltage_v, 'V', 3)}–{_format_value(self.maximum_cell_voltage_v, 'V', 3)}"
        temperature_range = f"{_format_value(self.minimum_temperature_c, '°C', 1)}–{_format_value(self.maximum_temperature_c, '°C', 1)}"
        return [
            "Virtual Battery",
            f"  Members: {self.member_count}/{self.configured_member_count}   Safe: {self.safe} ({self.safety_reason})",
            f"  Pack: {voltage}   {current}   SoC: {soc}   Capacity: {capacity}",
            f"  Cells: {cell_range}   Temperatures: {temperature_range}",
            f"  Charge enabled: {self.charge_enabled}   Discharge enabled: {self.discharge_enabled}",
        ]


def _format_value(value: float | None, unit: str, precision: int) -> str:
    if value is None:
        return UNKNOWN_MQTT_VALUE
    return f"{value:.{precision}f} {unit}"


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _section_number(payload: dict[str, Any], section: str, field: str) -> float | None:
    section_value = payload.get(section)
    if not isinstance(section_value, dict):
        return None
    return _number(section_value.get(field))


def _valid_member(payload: dict[str, Any]) -> bool:
    return all(isinstance(payload.get(field), int) for field in ("id", "cabinet", "row", "depth"))


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return fmean(values) if values else None


def _sum(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) if values else None


def aggregate_virtual_battery(
    payloads: Iterable[dict[str, Any]],
    *,
    identifier: str = "Daly Virtual Battery",
) -> VirtualBattery:
    """Combine BMS reports having complete rack-location metadata.

    Voltage, state of charge, and temperatures are averaged across the parallel
    members. Current and reported capacity are summed. The most restrictive cell
    readings and enabled flags are retained. Daly reports do not expose CAN charge
    or discharge limits, nor state of health, so those fields remain unknown until
    a future policy/configuration supplies them.
    """
    configured_payloads = [payload for payload in payloads if _valid_member(payload)]
    complete_payloads = [
        payload for payload in configured_payloads
        if isinstance(payload.get("soc"), dict)
        and isinstance(payload.get("cell_voltage_range"), dict)
        and isinstance(payload.get("temperature_range"), dict)
    ]

    pack_voltages = [_section_number(payload, "soc", "total_voltage") for payload in complete_payloads]
    pack_currents = [_section_number(payload, "soc", "current") for payload in complete_payloads]
    state_of_charge = [_section_number(payload, "soc", "soc_percent") for payload in complete_payloads]
    lowest_cell_voltages = [_section_number(payload, "cell_voltage_range", "lowest_voltage") for payload in complete_payloads]
    highest_cell_voltages = [_section_number(payload, "cell_voltage_range", "highest_voltage") for payload in complete_payloads]
    lowest_temperatures = [_section_number(payload, "temperature_range", "lowest_temperature") for payload in complete_payloads]
    highest_temperatures = [_section_number(payload, "temperature_range", "highest_temperature") for payload in complete_payloads]
    capacities = [_section_number(payload, "mosfet_status", "capacity_ah") for payload in complete_payloads]
    cycle_counts = [_section_number(payload, "status", "cycles") for payload in complete_payloads]

    def present(values: Iterable[float | None]) -> list[float]:
        return [value for value in values if value is not None]

    complete_safe = bool(complete_payloads) and all(payload.get("safe") is True for payload in complete_payloads)
    unsafe_reasons = [str(payload.get("safety_reason")) for payload in complete_payloads if payload.get("safe") is not True]
    safety_reason = "OK" if complete_safe else (unsafe_reasons[0] if unsafe_reasons else "No complete configured BMS reports")

    charging_mosfets = [
        payload.get("mosfet_status", {}).get("charging_mosfet")
        for payload in complete_payloads
        if isinstance(payload.get("mosfet_status"), dict)
    ]
    discharging_mosfets = [
        payload.get("mosfet_status", {}).get("discharging_mosfet")
        for payload in complete_payloads
        if isinstance(payload.get("mosfet_status"), dict)
    ]

    return VirtualBattery(
        identifier=identifier,
        member_count=len(complete_payloads),
        configured_member_count=len(configured_payloads),
        safe=complete_safe,
        safety_reason=safety_reason,
        charge_enabled=complete_safe and bool(charging_mosfets) and all(value is True for value in charging_mosfets),
        discharge_enabled=complete_safe and bool(discharging_mosfets) and all(value is True for value in discharging_mosfets),
        pack_voltage_v=_mean(present(pack_voltages)),
        pack_current_a=_sum(present(pack_currents)),
        state_of_charge_percent=_mean(present(state_of_charge)),
        state_of_health_percent=None,
        capacity_ah=_sum(present(capacities)),
        remaining_capacity_ah=_sum(present(capacities)),
        charge_voltage_limit_v=None,
        charge_current_limit_a=None,
        discharge_voltage_limit_v=None,
        discharge_current_limit_a=None,
        minimum_cell_voltage_v=min(present(lowest_cell_voltages), default=None),
        maximum_cell_voltage_v=max(present(highest_cell_voltages), default=None),
        minimum_temperature_c=min(present(lowest_temperatures), default=None),
        maximum_temperature_c=max(present(highest_temperatures), default=None),
        cycle_count=int(max(present(cycle_counts), default=0)) if present(cycle_counts) else None,
    )
