import unittest

from manager import filter_serial_ports, parse_discover_output


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


if __name__ == "__main__":
    unittest.main()
