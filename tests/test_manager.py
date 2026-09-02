import unittest

from manager import filter_serial_ports, parse_discover_output, bitmask_to_ids, PortRegistry


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
        self.assertEqual(bitmask_to_ids(0x00000380), [7, 8, 9])
        self.assertEqual(bitmask_to_ids(0x80000000), [32])
        self.assertEqual(bitmask_to_ids(0xFFFFFFFF), list(range(1, 33)))

    def test_render_dashboard_includes_missing_bms_row(self):
        registry = PortRegistry()
        registry.add_port("/dev/ttyACM0")
        registry.monitors["/dev/ttyACM0"].discovered = [7, 8]

        dashboard = registry.render_dashboard()

        self.assertIn("Missing BMSs:", dashboard)
        self.assertIn("1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15, 16", dashboard)


if __name__ == "__main__":
    unittest.main()
