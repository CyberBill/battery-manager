import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from manager import (
    DALY_BMS_MAX_ID,
    PortMonitor,
    PortRegistry,
    BatteryConfiguration,
    SafetyConfiguration,
    bind_dashboard_monitor_updates,
    bitmask_to_ids,
    build_bms_management_payload,
    build_mqtt_device_identity,
    build_mqtt_hass_config_discovery,
    discover_port_via_library,
    enumerate_serial_ports,
    filter_serial_ports,
    evaluate_bms_safety,
    get_missing_mqtt_fields,
    is_excluded_port,
    load_battery_configuration,
    load_safety_configuration,
    mqtt_iterator,
    parse_discover_output,
    should_quit_dashboard_key,
)


class ManagerTests(unittest.TestCase):
    @staticmethod
    def battery_config() -> BatteryConfiguration:
        return BatteryConfiguration(batteries_by_serial={})

    @staticmethod
    def safety_config() -> SafetyConfiguration:
        return SafetyConfiguration(35, 0.5, 39, 51, 3.1, 4.18)

    def test_filter_serial_ports_keeps_raspberry_serial_candidates(self):
        raw_ports = [
            "/dev/ttyS0",
            "/dev/ttyUSB0",
            "/dev/ttyUSB1",
            "/dev/ttyACM0",
            "/dev/ttyAMA0",
            "/dev/pts/3",
            "/dev/input/event0",
        ]

        ports = filter_serial_ports(raw_ports)

        self.assertEqual(
            ports,
            [
                "/dev/ttyS0",
                "/dev/ttyUSB0",
                "/dev/ttyUSB1",
                "/dev/ttyACM0",
                "/dev/ttyAMA0",
            ],
        )

    def test_is_excluded_port_handles_multiple_exclusions(self):
        self.assertTrue(
            is_excluded_port(
                "/dev/ttyUSB0",
                excluded_ports=["/dev/ttyUSB0", "/dev/ttyUSB1"],
                excluded_port_ids=["usb-VictronEnergy_BV_VE_Direct_cable_ABC123-if00-port0"],
            )
        )
        self.assertTrue(
            is_excluded_port(
                "/dev/ttyAMA10",
                excluded_ports=["/dev/ttyUSB0"],
                excluded_port_ids=["usb-VictronEnergy_BV_VE_Direct_cable_ABC123-if00-port0"],
            )
        )
        self.assertFalse(
            is_excluded_port(
                "/dev/ttyUSB3",
                excluded_ports=["/dev/ttyUSB0", "/dev/ttyUSB1"],
                excluded_port_ids=["usb-VictronEnergy_BV_VE_Direct_cable_ABC123-if00-port0"],
            )
        )

    def test_is_excluded_port_matches_by_id_symlink_name(self):
        with patch("manager.Path.iterdir") as mock_iterdir:
            mock_iterdir.return_value = [
                Path("/dev/serial/by-id/usb-VictronEnergy_BV_VE_Direct_cable_VE9IUP7O-if00-port0"),
            ]
            with patch("manager.os.path.realpath", return_value="/dev/ttyUSB0"):
                self.assertTrue(
                    is_excluded_port(
                        "/dev/ttyUSB0",
                        excluded_port_ids=["usb-VictronEnergy_BV_VE_Direct_cable_VE9IUP7O-if00-port0"],
                    )
                )

    def test_enumerate_serial_ports_ignores_internal_uart_defaults(self):
        ports = [
            "/dev/ttyAMA10",
            "/dev/ttyS0",
            "/dev/ttyUSB0",
            "/dev/ttyACM0",
            "/dev/pts/3",
        ]

        filtered = enumerate_serial_ports(
            excluded_ports=[],
            excluded_port_ids=[],
            excluded_patterns=["/dev/ttyAMA*", "/dev/ttyS*"],
        )
        self.assertNotIn("/dev/ttyAMA10", filtered)
        self.assertNotIn("/dev/ttyS0", filtered)

    def test_parse_discover_output_extracts_bms_ids(self):
        sample = """
Scanning Daly BMS IDs from mask 0xFFFFFFFF on /dev/ttyUSB0...
[2]
ID 2
  Serial number: 123456

[5]
ID 5
  Serial number: ABCDEF

Found 2 BMS devices.
"""

        self.assertEqual(parse_discover_output(sample), [2, 5])

    def test_bitmask_to_ids_returns_ids_in_bit_order(self):
        self.assertEqual(bitmask_to_ids(0x0000001F), [1, 2, 3, 4, 5])
        self.assertEqual(bitmask_to_ids(0x00000380), [8, 9, 10])
        self.assertEqual(bitmask_to_ids(0x00008000), [16])
        self.assertEqual(bitmask_to_ids(0xFFFFFFFF), list(range(1, DALY_BMS_MAX_ID + 1)))
        self.assertEqual(bitmask_to_ids(0xFFFFFFFF, max_ids=16), list(range(1, 17)))

    def test_render_dashboard_omits_missing_bms_row(self):
        registry = PortRegistry(battery_config=self.battery_config())
        registry.add_port("/dev/ttyACM0")
        registry.monitors["/dev/ttyACM0"].discovered = [7, 8]

        dashboard = registry.render_dashboard()

        self.assertNotIn("Missing BMSs:", dashboard)
        self.assertNotIn("1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15, 16", dashboard)

    def test_render_dashboard_shows_cumulative_cell_voltage_summary(self):
        registry = PortRegistry(battery_config=self.battery_config())
        registry.add_port("/dev/ttyACM0")
        registry.add_port("/dev/ttyUSB0")
        registry.record_bms_payload(
            "/dev/ttyACM0:1",
            {
                "cell_voltage_range": {
                    "lowest_voltage": 3.21,
                    "highest_voltage": 3.42,
                },
                "pack_voltage": 51.2,
            },
        )
        registry.record_bms_payload(
            "/dev/ttyUSB0:2",
            {
                "cell_voltage_range": {
                    "lowest_voltage": 3.19,
                    "highest_voltage": 3.51,
                },
                "temperature": 26.4,
            },
        )

        dashboard = registry.render_dashboard()

        self.assertIn("Cumulative Battery Data", dashboard)
        self.assertIn("Sources: 2", dashboard)
        self.assertIn("Voltage summaries: 2", dashboard)
        self.assertIn("Min cell voltage: 3.190 V", dashboard)
        self.assertIn("Max cell voltage: 3.510 V", dashboard)

    def test_port_registry_refresh_ports_tracks_usb_hotplug_events(self):
        registry = PortRegistry(battery_config=self.battery_config())
        registry.add_port("/dev/ttyUSB0")

        with patch("manager.enumerate_serial_ports", return_value=["/dev/ttyUSB0", "/dev/ttyUSB1"]):
            registry.refresh_ports()
        self.assertIn("/dev/ttyUSB1", registry.monitors)

        with patch("manager.enumerate_serial_ports", return_value=["/dev/ttyUSB1"]):
            registry.refresh_ports()
        self.assertNotIn("/dev/ttyUSB0", registry.monitors)
        self.assertIn("/dev/ttyUSB1", registry.monitors)

    def test_monitor_status_summary_uses_error_label(self):
        monitor = PortMonitor(port="/dev/ttyUSB0")
        monitor.error = "device reset"

        self.assertEqual(monitor.status_summary(), "ERROR")

    def test_port_registry_refresh_ports_starts_new_monitors(self):
        registry = PortRegistry(battery_config=self.battery_config())
        mqtt_config = {
            "enabled": True,
            "broker": "homeassistant",
            "user": "daly",
            "password": "secret",
        }

        bind_dashboard_monitor_updates(registry, mqtt_config)
        with patch("manager.enumerate_serial_ports", return_value=["/dev/ttyUSB0", "/dev/ttyUSB1"]):
            registry.refresh_ports()

        self.assertIn("/dev/ttyUSB0", registry.monitors)
        self.assertIn("/dev/ttyUSB1", registry.monitors)
        self.assertIsNotNone(registry.monitors["/dev/ttyUSB0"].on_update)
        self.assertIsNotNone(registry.monitors["/dev/ttyUSB1"].on_update)
        self.assertTrue(registry.monitors["/dev/ttyUSB0"].thread is not None and registry.monitors["/dev/ttyUSB0"].thread.is_alive())
        self.assertTrue(registry.monitors["/dev/ttyUSB1"].thread is not None and registry.monitors["/dev/ttyUSB1"].thread.is_alive())

    def test_discover_port_via_library_uses_dalybms_for_each_candidate(self):
        class FakeSerial:
            def __init__(self):
                self.timeout = 0.05
                self.writeTimeout = 0.05
                self.is_open = True

        class FakeDalyBMS:
            def __init__(self, request_retries, address, bms_id, logger):
                self.bms_id = bms_id
                self.logger = logger
                self.serial = None

            def connect(self, device, timeout=None):
                self.serial = FakeSerial()

            def get_board_info(self):
                if self.bms_id in {2, 5}:
                    return {"board_number": 1, "slave_number": 0}
                return False

            def disconnect(self):
                self.serial = None

        with patch("dalybms.DalyBMS", FakeDalyBMS):
            discovered = discover_port_via_library("/dev/ttyUSB0", bitmask=0xFFFFFFFF, logger=None)

        self.assertEqual(discovered, [2, 5])

    def test_discover_port_via_library_raises_on_unresponsive_serial_port(self):
        class FakeDalyBMS:
            def __init__(self, request_retries, address, bms_id, logger):
                self.bms_id = bms_id
                self.logger = logger
                self.serial = None

            def connect(self, device, timeout=None):
                raise OSError("device reset")

            def disconnect(self):
                self.serial = None

        with patch("dalybms.DalyBMS", FakeDalyBMS):
            with self.assertRaisesRegex(RuntimeError, "not responding to Daly discovery"):
                discover_port_via_library("/dev/ttyUSB0", bitmask=0x00000001, logger=None)

    def test_discover_port_via_library_uses_quiet_probe_logger(self):
        created = []

        class FakeSerial:
            def __init__(self):
                self.timeout = 0.05
                self.writeTimeout = 0.05
                self.is_open = True

        class FakeDalyBMS:
            def __init__(self, request_retries, address, bms_id, logger):
                created.append(logger)
                self.logger = logger
                self.serial = FakeSerial()

            def connect(self, device, timeout=None):
                return None

            def get_board_info(self):
                return False

            def disconnect(self):
                self.serial = None

        with patch("dalybms.DalyBMS", FakeDalyBMS):
            discover_port_via_library("/dev/ttyUSB0", bitmask=0x00000001, logger=None)

        self.assertTrue(created)
        self.assertEqual(created[0].name, "battery_manager.discovery_probe")

    def test_dashboard_monitor_updates_publish_when_mqtt_enabled(self):
        registry = PortRegistry(battery_config=self.battery_config())
        registry.add_port("/dev/ttyACM0")
        monitor = registry.monitors["/dev/ttyACM0"]
        monitor.discovered = [7]
        calls = []

        def fake_publish(config):
            calls.append(config)

        monitor.publish_report = fake_publish
        mqtt_config = {
            "enabled": True,
            "broker": "homeassistant",
            "user": "daly",
            "password": "secret",
        }

        bind_dashboard_monitor_updates(registry, mqtt_config)
        monitor.on_update()

        self.assertEqual(calls, [{**mqtt_config, "port": 1883}])

    def test_dashboard_monitor_updates_record_nested_payload_for_summary(self):
        registry = PortRegistry(battery_config=self.battery_config())
        registry.add_port("/dev/ttyACM0")
        monitor = registry.monitors["/dev/ttyACM0"]
        monitor.discovered = [9]

        def fake_publish(_config):
            return [
                {
                    "bms_id": 9,
                    "status": "ok",
                    "payload": {
                        "bms_id": 9,
                        "status": "ok",
                        "serial_number": "221KL280200079",
                        "topic_root": "battery/daly/221KL280200079",
                        "payload": {
                            "cell_voltage_range": {
                                "lowest_voltage": 3.779,
                                "highest_voltage": 3.831,
                            }
                        },
                    },
                }
            ]

        monitor.publish_report = fake_publish
        mqtt_config = {
            "enabled": True,
            "broker": "homeassistant",
            "user": "daly",
            "password": "secret",
        }

        bind_dashboard_monitor_updates(registry, mqtt_config)
        monitor.on_update()

        summary = registry.cumulative_battery_summary()
        self.assertEqual(summary.get("sources"), 1)
        self.assertEqual(summary.get("voltage_summaries"), 1)
        self.assertEqual(summary.get("min_cell_voltage"), 3.779)
        self.assertEqual(summary.get("max_cell_voltage"), 3.831)

    def test_build_mqtt_device_identity_matches_cli_naming(self):
        device_id, device_name, topic_root = build_mqtt_device_identity("ABC-123")

        self.assertEqual(device_id, "daly_abc_123")
        self.assertEqual(device_name, "Daly BMS ABC-123")
        self.assertEqual(topic_root, "battery/daly/ABC-123")

    def test_build_mqtt_device_identity_falls_back_to_legacy_single_bms_name(self):
        device_id, device_name, topic_root = build_mqtt_device_identity(None)

        self.assertEqual(device_id, "daly_bms")
        self.assertEqual(device_name, "Daly BMS")
        self.assertEqual(topic_root, "daly_bms")

    def test_load_battery_configuration_reads_serial_metadata(self):
        battery_config = load_battery_configuration(Path(__file__).parent.parent / "battery-config.json")

        battery = battery_config.find_battery("221KL280200318")

        self.assertIsNotNone(battery)
        assert battery is not None
        self.assertEqual(battery.id, 1)
        self.assertEqual(battery.cabinet, 1)
        self.assertEqual(battery.row, 2)
        self.assertEqual(battery.depth, 2)

    def test_management_payload_uses_configured_metadata_and_temporary_safety_values(self):
        battery_config = load_battery_configuration(Path(__file__).parent.parent / "battery-config.json")

        self.assertEqual(
            build_bms_management_payload(battery_config, "221KL280200318", (True, "OK")),
            {
                "id": 1,
                "cabinet": 1,
                "row": 2,
                "depth": 2,
                "safe": True,
                "safety_reason": "OK",
            },
        )

    def test_management_payload_uses_home_assistant_unknown_for_unknown_or_unset_location_data(self):
        battery_config = load_battery_configuration(Path(__file__).parent.parent / "battery-config.json")

        self.assertEqual(
            build_bms_management_payload(battery_config, "unknown-serial", (True, "OK")),
            {
                "id": "None",
                "cabinet": "None",
                "row": "None",
                "depth": "None",
                "safe": True,
                "safety_reason": "OK",
            },
        )
        self.assertEqual(
            build_bms_management_payload(battery_config, "221KL280200089", (True, "OK")),
            {
                "id": 7,
                "cabinet": "None",
                "row": "None",
                "depth": "None",
                "safe": True,
                "safety_reason": "OK",
            },
        )

    def test_load_safety_configuration_and_evaluate_bms_safety(self):
        safety_config = load_safety_configuration(Path(__file__).parent.parent / "safety-config.json")
        payload = {
            "temperature_range": {"highest_temperature": 36},
            "cell_voltage_range": {"highest_voltage": 3.9, "lowest_voltage": 3.6},
            "soc": {"total_voltage": 48},
        }

        self.assertEqual(safety_config.max_temperature_c, 35)
        self.assertEqual(
            evaluate_bms_safety(payload, safety_config),
            (False, "Battery temperature is 36 C (limit 35 C)"),
        )

    def test_evaluate_bms_safety_checks_highest_cell_voltage(self):
        payload = {
            "temperature_range": {"highest_temperature": 30},
            "cell_voltage_range": {"highest_voltage": 4.19, "lowest_voltage": 3.8},
            "soc": {"total_voltage": 48},
        }

        self.assertEqual(
            evaluate_bms_safety(payload, self.safety_config()),
            (False, "Cell voltage exceeds safety limit: highest cell is 4.190 V"),
        )

    def test_evaluate_bms_safety_checks_lowest_cell_voltage(self):
        payload = {
            "temperature_range": {"highest_temperature": 30},
            "cell_voltage_range": {"highest_voltage": 3.5, "lowest_voltage": 3.09},
            "soc": {"total_voltage": 48},
        }

        self.assertEqual(
            evaluate_bms_safety(payload, self.safety_config()),
            (False, "Cell voltage is below safety limit: lowest cell is 3.090 V (minimum 3.100 V)"),
        )

    def test_mqtt_iterator_uses_library_adapter_humanized_hass_paths(self):
        mqtt_client = MagicMock()
        mqtt_client.publish.return_value.rc = 0
        mqtt_client.publish.return_value.wait_for_publish.return_value = None
        logger = MagicMock()
        result = {"cell_voltages": {"12": 3.409}}

        mqtt_iterator(
            result,
            mqtt_client=mqtt_client,
            logger=logger,
            topic_root="battery/daly/ABC123",
            device_id="daly_abc_123",
            device_name="Daly BMS ABC123",
            serial_number="ABC123",
            mqtt_hass=True,
        )

        discovery_topic = None
        payload = None
        for call in mqtt_client.publish.call_args_list:
            args = call.args
            if args[0].startswith("homeassistant/sensor/"):
                discovery_topic = args[0]
                payload = args[1]
                break

        self.assertEqual(discovery_topic, "homeassistant/sensor/daly_abc_123/cell_voltages_12/config")
        self.assertIn('"name": "Cell 12 Voltage"', payload)
        self.assertIn('"state_topic": "battery/daly/ABC123/cell_voltages/12"', payload)

    def test_mqtt_iterator_publishes_management_entities(self):
        mqtt_client = MagicMock()
        mqtt_client.publish.return_value.rc = 0
        mqtt_client.publish.return_value.wait_for_publish.return_value = None
        logger = MagicMock()

        mqtt_iterator(
            {
                "id": 1,
                "cabinet": 2,
                "row": 3,
                "depth": 4,
                "safe": True,
                "safety_reason": "OK",
            },
            mqtt_client=mqtt_client,
            logger=logger,
            topic_root="battery/daly/ABC123",
            device_id="daly_abc_123",
            device_name="Daly BMS ABC123",
            serial_number="ABC123",
            mqtt_hass=True,
        )

        messages = {call.args[0]: call.args[1] for call in mqtt_client.publish.call_args_list}
        id_config = json.loads(messages["homeassistant/sensor/daly_abc_123/id/config"])
        self.assertEqual(id_config["unique_id"], "daly_abc_123_id")
        self.assertEqual(id_config["state_topic"], "battery/daly/ABC123/id")
        self.assertEqual(messages["battery/daly/ABC123/id"], 1)
        self.assertEqual(messages["battery/daly/ABC123/cabinet"], 2)
        self.assertEqual(messages["battery/daly/ABC123/row"], 3)
        self.assertEqual(messages["battery/daly/ABC123/depth"], 4)
        self.assertIs(messages["battery/daly/ABC123/safe"], True)
        self.assertEqual(messages["battery/daly/ABC123/safety_reason"], "OK")

    def test_get_missing_mqtt_fields_lists_missing_values(self):
        self.assertEqual(
            get_missing_mqtt_fields("homeassistant", None, "secret"),
            ["--mqtt-user"],
        )

    def test_should_quit_dashboard_key_matches_quit_keys(self):
        self.assertTrue(should_quit_dashboard_key("q"))
        self.assertTrue(should_quit_dashboard_key("Q"))
        self.assertTrue(should_quit_dashboard_key("\x1b"))
        self.assertTrue(should_quit_dashboard_key("\x03"))
        self.assertFalse(should_quit_dashboard_key("a"))
        self.assertFalse(should_quit_dashboard_key(None))



if __name__ == "__main__":
    unittest.main()
