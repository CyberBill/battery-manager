import unittest

from virtual_battery import UNKNOWN_MQTT_VALUE, aggregate_virtual_battery


def bms_payload(**overrides):
    payload = {
        "id": 1,
        "cabinet": 1,
        "row": 1,
        "depth": 1,
        "safe": True,
        "safety_reason": "OK",
        "soc": {"total_voltage": 51.0, "current": -10.0, "soc_percent": 80.0},
        "cell_voltage_range": {"lowest_voltage": 3.18, "highest_voltage": 3.32},
        "temperature_range": {"lowest_temperature": 21.0, "highest_temperature": 27.0},
        "mosfet_status": {"capacity_ah": 100.0, "charging_mosfet": True, "discharging_mosfet": True},
        "status": {"cycles": 120},
    }
    payload.update(overrides)
    return payload


class VirtualBatteryTests(unittest.TestCase):
    def test_aggregate_combines_parallel_bms_reports(self):
        second = bms_payload(
            id=2,
            depth=2,
            soc={"total_voltage": 51.4, "current": 5.0, "soc_percent": 60.0},
            cell_voltage_range={"lowest_voltage": 3.16, "highest_voltage": 3.35},
            temperature_range={"lowest_temperature": 19.0, "highest_temperature": 29.0},
            mosfet_status={"capacity_ah": 100.0, "charging_mosfet": True, "discharging_mosfet": True},
            status={"cycles": 125},
        )

        virtual = aggregate_virtual_battery([bms_payload(), second])

        self.assertEqual(virtual.member_count, 2)
        self.assertEqual(virtual.configured_member_count, 2)
        self.assertTrue(virtual.safe)
        assert virtual.pack_voltage_v is not None
        assert virtual.pack_current_a is not None
        assert virtual.state_of_charge_percent is not None
        assert virtual.capacity_ah is not None
        self.assertAlmostEqual(virtual.pack_voltage_v, 51.2)
        self.assertAlmostEqual(virtual.pack_current_a, -5.0)
        self.assertAlmostEqual(virtual.state_of_charge_percent, 70.0)
        self.assertAlmostEqual(virtual.capacity_ah, 200.0)
        self.assertEqual(virtual.minimum_cell_voltage_v, 3.16)
        self.assertEqual(virtual.maximum_cell_voltage_v, 3.35)
        self.assertEqual(virtual.minimum_temperature_c, 19.0)
        self.assertEqual(virtual.maximum_temperature_c, 29.0)
        self.assertEqual(virtual.cycle_count, 125)
        self.assertTrue(virtual.charge_enabled)
        self.assertTrue(virtual.discharge_enabled)

    def test_aggregate_excludes_bms_without_complete_rack_metadata(self):
        unconfigured = bms_payload(id="None", cabinet="None", row="None", depth="None")

        virtual = aggregate_virtual_battery([bms_payload(), unconfigured])

        self.assertEqual(virtual.member_count, 1)
        self.assertEqual(virtual.configured_member_count, 1)

    def test_aggregate_propagates_unsafe_member_and_unknown_can_limits(self):
        unsafe = bms_payload(safe=False, safety_reason="Battery temperature is 40 C (limit 35 C)")

        virtual = aggregate_virtual_battery([unsafe])
        mqtt_payload = virtual.mqtt_payload()

        self.assertFalse(virtual.safe)
        self.assertFalse(virtual.charge_enabled)
        self.assertFalse(virtual.discharge_enabled)
        self.assertEqual(virtual.safety_reason, "Battery temperature is 40 C (limit 35 C)")
        self.assertEqual(mqtt_payload["charge_current_limit_a"], UNKNOWN_MQTT_VALUE)
        self.assertEqual(mqtt_payload["state_of_health_percent"], UNKNOWN_MQTT_VALUE)


if __name__ == "__main__":
    unittest.main()