import unittest

from virtual_battery import UNKNOWN_MQTT_VALUE, VirtualBatterySettings, aggregate_virtual_battery


VIRTUAL_BATTERY_SETTINGS = VirtualBatterySettings(
    charge_voltage_limit_v=49.8,
    discharge_voltage_limit_v=40.0,
    charge_current_limit_a=100.0,
    discharge_current_limit_a=100.0,
)


def bms_payload(**overrides):
    payload = {
        "id": 1,
        "cabinet": 1,
        "row": 1,
        "depth": 1,
        "safe": True,
        "safety_reason": "OK",
        "soc": {"total_voltage": 48.0, "current": -10.0, "soc_percent": 80.0},
        "cell_voltage_range": {"lowest_voltage": 3.18, "highest_voltage": 3.32},
        "temperature_range": {"lowest_temperature": 21.0, "highest_temperature": 27.0},
        "rated_parameters": {"rated_capacity_ah": 100.0},
        "mosfet_status": {"remaining_capacity_ah": 80.0, "charging_mosfet": True, "discharging_mosfet": True},
        "status": {"cycles": 120},
    }
    payload.update(overrides)
    return payload


class VirtualBatteryTests(unittest.TestCase):
    def test_aggregate_combines_parallel_bms_reports(self):
        second = bms_payload(
            id=2,
            depth=2,
            soc={"total_voltage": 48.4, "current": 5.0, "soc_percent": 60.0},
            cell_voltage_range={"lowest_voltage": 3.16, "highest_voltage": 3.35},
            temperature_range={"lowest_temperature": 19.0, "highest_temperature": 29.0},
            rated_parameters={"rated_capacity_ah": 100.0},
            mosfet_status={"remaining_capacity_ah": 60.0, "charging_mosfet": True, "discharging_mosfet": True},
            status={"cycles": 125},
        )

        virtual = aggregate_virtual_battery([bms_payload(), second], settings=VIRTUAL_BATTERY_SETTINGS)

        self.assertEqual(virtual.member_count, 2)
        self.assertEqual(virtual.configured_member_count, 2)
        self.assertTrue(virtual.safe)
        assert virtual.pack_voltage_v is not None
        assert virtual.pack_current_a is not None
        assert virtual.state_of_charge_percent is not None
        assert virtual.rated_capacity_ah is not None
        assert virtual.remaining_capacity_ah is not None
        self.assertAlmostEqual(virtual.pack_voltage_v, 48.2)
        self.assertAlmostEqual(virtual.pack_current_a, -5.0)
        self.assertAlmostEqual(virtual.state_of_charge_percent, 70.0)
        self.assertAlmostEqual(virtual.rated_capacity_ah, 200.0)
        self.assertAlmostEqual(virtual.remaining_capacity_ah, 140.0)
        self.assertEqual(virtual.charge_voltage_limit_v, 49.8)
        self.assertEqual(virtual.discharge_voltage_limit_v, 40.0)
        self.assertEqual(virtual.state_of_health_percent, 100.0)
        self.assertEqual(virtual.minimum_cell_voltage_v, 3.16)
        self.assertEqual(virtual.maximum_cell_voltage_v, 3.35)
        self.assertEqual(virtual.minimum_temperature_c, 19.0)
        self.assertEqual(virtual.maximum_temperature_c, 29.0)
        self.assertEqual(virtual.cycle_count, 123)
        self.assertFalse(virtual.full_charge_cutoff)
        self.assertFalse(virtual.empty_discharge_cutoff)
        self.assertTrue(virtual.charge_enabled)
        self.assertTrue(virtual.discharge_enabled)
        self.assertEqual(
            virtual.dashboard_lines()[-1],
            "  Charge enabled: True (100.0 A)   Discharge enabled: True (100.0 A)",
        )

    def test_aggregate_stops_charging_and_reports_full_when_voltage_reaches_limit(self):
        full = bms_payload(soc={"total_voltage": 49.8, "current": 2.0, "soc_percent": 94.0})

        virtual = aggregate_virtual_battery([full], settings=VIRTUAL_BATTERY_SETTINGS)

        self.assertTrue(virtual.full_charge_cutoff)
        self.assertFalse(virtual.charge_enabled)
        self.assertEqual(virtual.charge_current_limit_a, 0.0)
        self.assertEqual(virtual.state_of_charge_percent, 100.0)
        self.assertEqual(virtual.discharge_current_limit_a, 100.0)
        self.assertEqual(
            virtual.dashboard_lines()[-1],
            "  Charge enabled: full-voltage cutoff (0.0 A)   Discharge enabled: True (100.0 A)",
        )

    def test_aggregate_stops_discharging_and_reports_empty_when_voltage_reaches_limit(self):
        empty = bms_payload(soc={"total_voltage": 40.0, "current": -2.0, "soc_percent": 11.0})

        virtual = aggregate_virtual_battery([empty], settings=VIRTUAL_BATTERY_SETTINGS)

        self.assertTrue(virtual.empty_discharge_cutoff)
        self.assertFalse(virtual.discharge_enabled)
        self.assertEqual(virtual.discharge_current_limit_a, 0.0)
        self.assertEqual(virtual.state_of_charge_percent, 0.0)
        self.assertEqual(virtual.charge_current_limit_a, 100.0)
        self.assertEqual(
            virtual.dashboard_lines()[-1],
            "  Charge enabled: True (100.0 A)   Discharge enabled: low-voltage cutoff (0.0 A)",
        )

    def test_aggregate_excludes_bms_without_complete_rack_metadata(self):
        unconfigured = bms_payload(id="None", cabinet="None", row="None", depth="None")

        virtual = aggregate_virtual_battery([bms_payload(), unconfigured])

        self.assertEqual(virtual.member_count, 1)
        self.assertEqual(virtual.configured_member_count, 1)

    def test_aggregate_propagates_unsafe_member_and_configured_can_limits(self):
        unsafe = bms_payload(safe=False, safety_reason="Battery temperature is 40 C (limit 35 C)")

        virtual = aggregate_virtual_battery([unsafe], settings=VIRTUAL_BATTERY_SETTINGS)
        mqtt_payload = virtual.mqtt_payload()

        self.assertFalse(virtual.safe)
        self.assertFalse(virtual.charge_enabled)
        self.assertFalse(virtual.discharge_enabled)
        self.assertEqual(virtual.safety_reason, "Battery temperature is 40 C (limit 35 C)")
        self.assertEqual(mqtt_payload["charge_current_limit_a"], 0.0)
        self.assertEqual(mqtt_payload["discharge_current_limit_a"], 0.0)
        self.assertEqual(mqtt_payload["state_of_health_percent"], 100.0)


if __name__ == "__main__":
    unittest.main()