import unittest
from unittest.mock import MagicMock, patch

from manager import (
    DALY_BMS_MAX_ID,
    PortRegistry,
    bind_dashboard_monitor_updates,
    bitmask_to_ids,
    build_mqtt_device_identity,
    build_mqtt_hass_config_discovery,
    discover_port_via_library,
    filter_serial_ports,
    get_missing_mqtt_fields,
    mqtt_iterator,
    parse_discover_output,
    should_quit_dashboard_key,
)


class ManagerTests(unittest.TestCase):
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
        registry = PortRegistry()
        registry.add_port("/dev/ttyACM0")
        registry.monitors["/dev/ttyACM0"].discovered = [7, 8]

        dashboard = registry.render_dashboard()

        self.assertNotIn("Missing BMSs:", dashboard)
        self.assertNotIn("1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15, 16", dashboard)

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
        registry = PortRegistry()
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
