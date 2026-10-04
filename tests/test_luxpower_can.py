import unittest
import threading

from luxpower_can import (
    CanFrame,
    LUXPOWER_FRAME_IDS,
    LuxPowerCanPublisher,
    build_luxpower_frames,
    build_waveshare_configuration,
    decode_waveshare_frames,
    encode_waveshare_frame,
)
from virtual_battery import VirtualBattery


def virtual_battery(**overrides):
    values = {
        "identifier": "Daly Virtual Battery",
        "member_count": 2,
        "configured_member_count": 2,
        "safe": True,
        "safety_reason": "OK",
        "full_charge_cutoff": False,
        "empty_discharge_cutoff": False,
        "charge_enabled": True,
        "discharge_enabled": True,
        "pack_voltage_v": 48.52,
        "pack_current_a": -12.3,
        "state_of_charge_percent": 76.0,
        "state_of_health_percent": 100.0,
        "rated_capacity_ah": 280.0,
        "remaining_capacity_ah": 212.8,
        "charge_voltage_limit_v": 49.8,
        "charge_current_limit_a": 100.0,
        "discharge_voltage_limit_v": 40.0,
        "discharge_current_limit_a": 100.0,
        "minimum_cell_voltage_v": 3.710,
        "maximum_cell_voltage_v": 3.831,
        "minimum_temperature_c": 19.0,
        "maximum_temperature_c": 25.0,
        "cycle_count": 42,
    }
    values.update(overrides)
    return VirtualBattery(**values)


class LuxPowerCanTests(unittest.TestCase):
    def test_waveshare_configuration_uses_variable_standard_normal_500k_mode(self):
        configuration = build_waveshare_configuration(500_000)

        self.assertEqual(configuration, bytes((
            0xAA, 0x55, 0x12, 0x03, 0x01,
            0, 0, 0, 0, 0, 0, 0, 0,
            0, 0, 0, 0, 0, 0,
            0x16,
        )))

    def test_waveshare_encoder_wraps_standard_frame_in_variable_packet(self):
        self.assertEqual(
            encode_waveshare_frame(CanFrame(0x351, bytes(range(8)))),
            bytes((0xAA, 0xC8, 0x51, 0x03, 0, 1, 2, 3, 4, 5, 6, 7, 0x55)),
        )

    def test_waveshare_decoder_keeps_partial_packets_and_ignores_noise(self):
        buffer = bytearray(b"noise\xAA\xC8\x05\x03\x01\x02")

        self.assertEqual(decode_waveshare_frames(buffer), [])
        self.assertEqual(buffer, bytearray(b"\xAA\xC8\x05\x03\x01\x02"))

        buffer.extend(b"\x03\x04\x05\x06\x07\x08\x55")
        self.assertEqual(decode_waveshare_frames(buffer), [CanFrame(0x305, bytes(range(1, 9)))])
        self.assertEqual(buffer, bytearray())

    def test_build_frames_match_yambms_luxpower_layout(self):
        frames = build_luxpower_frames(virtual_battery())

        self.assertEqual(tuple(frame.arbitration_id for frame in frames), LUXPOWER_FRAME_IDS)
        self.assertEqual([frame.data for frame in frames], [
            b"YamBMS\x00\x00",
            bytes((0xF2, 0x01, 0xE8, 0x03, 0xE8, 0x03, 0x90, 0x01)),
            bytes((76, 0, 100, 0, 0xF7, 0x0E, 0x7E, 0x0E)),
            bytes((0xF4, 0x12, 0x85, 0xFF, 0xFB, 0x00, 0xBE, 0x00)),
            bytes((0, 0, 0, 0, 2, 0x18, 0x01, 0)),
            bytes((0xC0, 0, 42, 0, 0, 0, 0, 0)),
        ])

    def test_incomplete_virtual_battery_emits_no_frames(self):
        self.assertEqual(build_luxpower_frames(virtual_battery(pack_voltage_v=None)), ())

    def test_status_summary_reports_waiting_transmission_and_error_states(self):
        publisher = LuxPowerCanPublisher.__new__(LuxPowerCanPublisher)
        publisher.port = "can0"
        publisher._lock = threading.Lock()
        publisher._frames = ()
        publisher._last_sent = None
        publisher._last_error = None
        publisher._last_received = None
        publisher._last_received_frame_id = None
        publisher._link_state = "awaiting"
        publisher._first_transmission_at = None
        publisher._next_reconnect_at = None
        self.assertEqual(publisher.status_summary(), "CAN: waiting for complete virtual battery data (can0)")

        publisher._frames = build_luxpower_frames(virtual_battery())
        self.assertEqual(publisher.status_summary(), "CAN: ready; awaiting first TX; last RX never (can0)")

        publisher._last_error = "No such device"
        self.assertEqual(publisher.status_summary(), "CAN: ERROR: No such device")
