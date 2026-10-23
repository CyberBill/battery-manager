"""Aggregate configured parallel Daly BMS units into one virtual battery model.

This module provides the normalized model and MQTT-facing payload consumed by the
separate LuxPower CAN transport.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from math import floor
from statistics import fmean
from typing import Any, Iterable

UNKNOWN_MQTT_VALUE = "None"


@dataclass(frozen=True)
class DeratingCurve:
    """Per-cell voltage span over which current tapers; start_v is full current, end_v is the floor."""

    start_v: float
    end_v: float
    min_current_a: float = 1.0


@dataclass(frozen=True)
class CellVoltageDeratingSettings:
    """Per-cell current derating near the charge and discharge voltage limits (YamBMS Auto CCL/DCL style)."""

    charge: DeratingCurve
    discharge: DeratingCurve
    curve_end_factor: float = 2.0
    curve_shape: float = 1.8
    recovery_seconds: float = 300.0


@dataclass(frozen=True)
class VirtualBatterySettings:
    """Static inverter-facing settings for the parallel virtual battery."""

    charge_voltage_limit_v: float
    discharge_voltage_limit_v: float
    current_limit_a: float
    charge_current_per_bms_a: float | None = None
    discharge_current_per_bms_a: float | None = None
    current_per_cabinet_a: float | None = None
    state_of_health_percent: float = 100.0
    cell_voltage_derating: CellVoltageDeratingSettings | None = None


@dataclass(frozen=True)
class VirtualBattery:
    """Aggregate state for a configured, physically installed parallel battery bank."""

    identifier: str
    member_count: int
    configured_member_count: int
    safe: bool
    safety_reason: str
    full_charge_cutoff: bool
    empty_discharge_cutoff: bool
    charge_enabled: bool
    discharge_enabled: bool
    pack_voltage_v: float | None
    pack_current_a: float | None
    state_of_charge_percent: float | None
    state_of_health_percent: float | None
    rated_capacity_ah: float | None
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
        rated_capacity = _format_value(self.rated_capacity_ah, "Ah", 1)
        remaining_capacity = _format_value(self.remaining_capacity_ah, "Ah", 1)
        charge_current_limit = _format_value(self.charge_current_limit_a, "A", 1)
        discharge_current_limit = _format_value(self.discharge_current_limit_a, "A", 1)
        cell_range = f"{_format_value(self.minimum_cell_voltage_v, 'V', 3)}–{_format_value(self.maximum_cell_voltage_v, 'V', 3)}"
        temperature_range = f"{_format_value(self.minimum_temperature_c, '°C', 1)}–{_format_value(self.maximum_temperature_c, '°C', 1)}"
        charge_status = "full-voltage cutoff" if self.full_charge_cutoff else str(self.charge_enabled)
        discharge_status = "low-voltage cutoff" if self.empty_discharge_cutoff else str(self.discharge_enabled)
        return [
            "Virtual Battery",
            f"  Members: {self.member_count}/{self.configured_member_count}   Safe: {self.safe} ({self.safety_reason})",
            f"  Pack: {voltage}   {current}   SoC: {soc}   Capacity: {remaining_capacity}/{rated_capacity}",
            f"  Cells: {cell_range}   Temperatures: {temperature_range}",
            f"  Charge enabled: {charge_status} ({charge_current_limit})   Discharge enabled: {discharge_status} ({discharge_current_limit})",
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


def _is_complete(payload: dict[str, Any]) -> bool:
    return _valid_member(payload) and all(
        isinstance(payload.get(section), dict) for section in ("soc", "cell_voltage_range", "temperature_range")
    )


def temperature_delta_violations(payloads: Iterable[dict[str, Any]], limit_c: float | None) -> list[tuple[dict[str, Any], str]]:
    """Return (payload, reason) for each complete BMS whose max temperature exceeds the array minimum by more than limit_c."""
    complete = [payload for payload in payloads if _is_complete(payload)]
    lows = [value for p in complete if (value := _section_number(p, "temperature_range", "lowest_temperature")) is not None]
    if limit_c is None or not lows:
        return []
    array_minimum = min(lows)
    violations = []
    for payload in complete:
        high = _section_number(payload, "temperature_range", "highest_temperature")
        if high is not None and high - array_minimum > limit_c:
            violations.append((
                payload,
                f"BMS {payload.get('id')} is {high - array_minimum:.1f} C above the array minimum "
                f"(max {high:g} C vs min {array_minimum:g} C, limit {limit_c:g} C)",
            ))
    return violations


def derating_fraction(voltage_v: float, curve: DeratingCurve, curve_end_factor: float, curve_shape: float) -> float:
    """Return the allowed fraction (0..1) of current at a cell voltage; works for rising and falling spans."""
    ratio = (voltage_v - curve.start_v) / (curve.end_v - curve.start_v)
    if ratio <= 0:
        return 1.0
    return min(1.0, max(0.0, 2.0 - curve_end_factor ** (min(ratio, 1.0) ** curve_shape)))


class CellVoltageDerater:
    """Derate virtual-battery current limits from the worst cell; drops instantly and recovers linearly."""

    def __init__(self, settings: CellVoltageDeratingSettings) -> None:
        self.settings = settings
        self._fractions: dict[str, float] = {}
        self._updated_at: dict[str, float] = {}

    def apply(self, battery: VirtualBattery, now: float) -> VirtualBattery:
        return replace(
            battery,
            charge_current_limit_a=self._limit(
                "charge", battery.charge_current_limit_a, battery.maximum_cell_voltage_v, self.settings.charge, now,
            ),
            discharge_current_limit_a=self._limit(
                "discharge", battery.discharge_current_limit_a, battery.minimum_cell_voltage_v, self.settings.discharge, now,
            ),
        )

    def _limit(
        self,
        direction: str,
        limit_a: float | None,
        cell_voltage_v: float | None,
        curve: DeratingCurve,
        now: float,
    ) -> float | None:
        if limit_a is None or cell_voltage_v is None:
            return limit_a
        past_end = cell_voltage_v > curve.end_v if curve.end_v > curve.start_v else cell_voltage_v < curve.end_v
        target = 0.0 if past_end else derating_fraction(
            cell_voltage_v, curve, self.settings.curve_end_factor, self.settings.curve_shape,
        )
        previous = self._fractions.get(direction)
        if previous is None or target < previous:
            fraction = target
        else:
            elapsed = max(0.0, now - self._updated_at[direction])
            fraction = min(target, previous + elapsed / self.settings.recovery_seconds)
        self._fractions[direction] = fraction
        self._updated_at[direction] = now
        if limit_a <= 0 or past_end:
            return 0.0
        return min(limit_a, max(curve.min_current_a, limit_a * fraction))


def _effective_current_limit(
    *,
    member_count: int,
    cabinet_count: int,
    array_limit_a: float,
    per_bms_limit_a: float | None,
    per_cabinet_limit_a: float | None,
) -> float:
    limits = [array_limit_a]
    if per_bms_limit_a is not None:
        limits.append(per_bms_limit_a * member_count)
    if per_cabinet_limit_a is not None:
        limits.append(per_cabinet_limit_a * cabinet_count)
    return min(limits)


def aggregate_virtual_battery(
    payloads: Iterable[dict[str, Any]],
    *,
    identifier: str = "Daly Virtual Battery",
    settings: VirtualBatterySettings | None = None,
    max_temperature_delta_c: float | None = None,
) -> VirtualBattery:
    """Combine BMS reports having complete rack-location metadata.

    Voltage and temperatures are averaged across the parallel members. State of
    charge is averaged unless any member reports full, in which case it is
    reported as 100%. Current, rated capacity, and remaining capacity are summed.
    The most restrictive cell readings and enabled flags are retained.
    Charge/discharge voltage/current limits and state of health come from the rack
    configuration.
    """
    configured_payloads = [payload for payload in payloads if _valid_member(payload)]
    complete_payloads = [payload for payload in configured_payloads if _is_complete(payload)]

    pack_voltages = [_section_number(payload, "soc", "total_voltage") for payload in complete_payloads]
    pack_currents = [_section_number(payload, "soc", "current") for payload in complete_payloads]
    state_of_charge = [_section_number(payload, "soc", "soc_percent") for payload in complete_payloads]
    lowest_cell_voltages = [_section_number(payload, "cell_voltage_range", "lowest_voltage") for payload in complete_payloads]
    highest_cell_voltages = [_section_number(payload, "cell_voltage_range", "highest_voltage") for payload in complete_payloads]
    lowest_temperatures = [_section_number(payload, "temperature_range", "lowest_temperature") for payload in complete_payloads]
    highest_temperatures = [_section_number(payload, "temperature_range", "highest_temperature") for payload in complete_payloads]
    rated_capacities = [_section_number(payload, "rated_parameters", "rated_capacity_ah") for payload in complete_payloads]
    remaining_capacities = [_section_number(payload, "mosfet_status", "remaining_capacity_ah") for payload in complete_payloads]
    cycle_counts = [_section_number(payload, "status", "cycles") for payload in complete_payloads]

    def present(values: Iterable[float | None]) -> list[float]:
        return [value for value in values if value is not None]

    delta_reasons = [reason for _, reason in temperature_delta_violations(complete_payloads, max_temperature_delta_c)]
    complete_safe = (
        bool(complete_payloads)
        and all(payload.get("safe") is True for payload in complete_payloads)
        and not delta_reasons
    )
    unsafe_reasons = [str(payload.get("safety_reason")) for payload in complete_payloads if payload.get("safe") is not True]
    unsafe_reasons.extend(delta_reasons)
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
    charge_enabled = complete_safe and bool(charging_mosfets) and all(value is True for value in charging_mosfets)
    discharge_enabled = complete_safe and bool(discharging_mosfets) and all(value is True for value in discharging_mosfets)
    cabinet_count = len({payload.get("cabinet") for payload in complete_payloads if isinstance(payload.get("cabinet"), int)})
    pack_voltage_v = _mean(present(pack_voltages))
    full_charge_cutoff = (
        settings is not None
        and pack_voltage_v is not None
        and pack_voltage_v >= settings.charge_voltage_limit_v
    )
    empty_discharge_cutoff = (
        settings is not None
        and pack_voltage_v is not None
        and pack_voltage_v <= settings.discharge_voltage_limit_v
    )
    member_reports_full = any(value >= 100.0 for value in present(state_of_charge))
    charge_current_limit_a = None
    discharge_current_limit_a = None
    if settings is not None:
        charge_current_limit_a = _effective_current_limit(
            member_count=len(complete_payloads),
            cabinet_count=cabinet_count,
            array_limit_a=settings.current_limit_a,
            per_bms_limit_a=settings.charge_current_per_bms_a,
            per_cabinet_limit_a=settings.current_per_cabinet_a,
        )
        discharge_current_limit_a = _effective_current_limit(
            member_count=len(complete_payloads),
            cabinet_count=cabinet_count,
            array_limit_a=settings.current_limit_a,
            per_bms_limit_a=settings.discharge_current_per_bms_a,
            per_cabinet_limit_a=settings.current_per_cabinet_a,
        )

    return VirtualBattery(
        identifier=identifier,
        member_count=len(complete_payloads),
        configured_member_count=len(configured_payloads),
        safe=complete_safe,
        safety_reason=safety_reason,
        full_charge_cutoff=full_charge_cutoff,
        empty_discharge_cutoff=empty_discharge_cutoff,
        charge_enabled=charge_enabled and not full_charge_cutoff,
        discharge_enabled=discharge_enabled and not empty_discharge_cutoff,
        pack_voltage_v=pack_voltage_v,
        pack_current_a=_sum(present(pack_currents)),
        state_of_charge_percent=(
            100.0 if full_charge_cutoff or member_reports_full
            else 0.0 if empty_discharge_cutoff
            else _mean(present(state_of_charge))
        ),
        state_of_health_percent=settings.state_of_health_percent if settings is not None else None,
        rated_capacity_ah=_sum(present(rated_capacities)),
        remaining_capacity_ah=_sum(present(remaining_capacities)),
        charge_voltage_limit_v=settings.charge_voltage_limit_v if settings is not None else None,
        charge_current_limit_a=(
            0.0 if not charge_enabled or full_charge_cutoff
            else charge_current_limit_a
        ),
        discharge_voltage_limit_v=settings.discharge_voltage_limit_v if settings is not None else None,
        discharge_current_limit_a=(
            0.0 if not discharge_enabled or empty_discharge_cutoff
            else discharge_current_limit_a
        ),
        minimum_cell_voltage_v=min(present(lowest_cell_voltages), default=None),
        maximum_cell_voltage_v=max(present(highest_cell_voltages), default=None),
        minimum_temperature_c=min(present(lowest_temperatures), default=None),
        maximum_temperature_c=max(present(highest_temperatures), default=None),
        cycle_count=floor(fmean(present(cycle_counts)) + 0.5) if present(cycle_counts) else None,
    )
