"""LuxPower/EG4 CAN transport for the aggregated virtual battery.

Frame encoding follows YamBMS' LuxPower mode: standard 11-bit frames 0x35E,
0x351, 0x355, 0x356, 0x359, and 0x35C, transmitted in that order at 100 ms
intervals. Multi-byte values are little-endian. Serial devices use the Waveshare
USB-CAN-A binary protocol; named interfaces use Linux SocketCAN.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from virtual_battery import VirtualBattery

CAN_UPDATE_INTERVAL_SECONDS = 0.1
CAN_HEARTBEAT_FRAME_ID = 0x305
CAN_HEARTBEAT_TIMEOUT_SECONDS = 5.0
CAN_RECONNECT_DELAY_SECONDS = 30.0
LUXPOWER_FRAME_IDS = (0x35E, 0x351, 0x355, 0x356, 0x359, 0x35C)
WAVESHARE_USB_SERIAL_BAUDRATE = 2_000_000
WAVESHARE_CAN_BITRATE_CODES = {
    1_000_000: 0x01,
    800_000: 0x02,
    500_000: 0x03,
    400_000: 0x04,
    250_000: 0x05,
    200_000: 0x06,
    125_000: 0x07,
    100_000: 0x08,
    50_000: 0x09,
    20_000: 0x0A,
    10_000: 0x0B,
    5_000: 0x0C,
}


@dataclass(frozen=True)
class CanFrame:
    """One standard CAN data frame ready for transmission."""

    arbitration_id: int
    data: bytes


def build_waveshare_configuration(bitrate: int) -> bytes:
    """Configure a USB-CAN-A for variable frames, 11-bit CAN, and normal mode."""
    try:
        bitrate_code = WAVESHARE_CAN_BITRATE_CODES[bitrate]
    except KeyError as exc:
        supported = ", ".join(str(value) for value in WAVESHARE_CAN_BITRATE_CODES)
        raise ValueError(f"Waveshare USB-CAN-A does not support CAN bitrate {bitrate}; use one of: {supported}.") from exc

    # AA 55, type 12 (variable protocol), bitrate, standard frame type,
    # receive-all filter/block IDs, normal mode, automatic retransmission.
    packet = bytearray((0xAA, 0x55, 0x12, bitrate_code, 0x01))
    packet.extend(bytes(14))
    packet.append(sum(packet[2:]) & 0xFF)
    return bytes(packet)


def encode_waveshare_frame(frame: CanFrame) -> bytes:
    """Wrap an 11-bit CAN data frame in the USB-CAN-A variable-length protocol."""
    if not 0 <= frame.arbitration_id <= 0x7FF:
        raise ValueError(f"Waveshare USB-CAN-A requires an 11-bit arbitration ID, got {frame.arbitration_id:#x}.")
    if not 0 <= len(frame.data) <= 8:
        raise ValueError(f"CAN frame payload must contain 0 to 8 bytes, got {len(frame.data)}.")
    return bytes((0xAA, 0xC0 | len(frame.data))) + frame.arbitration_id.to_bytes(2, "little") + frame.data + bytes((0x55,))


def decode_waveshare_frames(buffer: bytearray) -> list[CanFrame]:
    """Extract complete standard CAN frames from a mutable USB-CAN-A byte buffer."""
    frames: list[CanFrame] = []
    while True:
        try:
            start = buffer.index(0xAA)
        except ValueError:
            buffer.clear()
            return frames
        if start:
            del buffer[:start]
        if len(buffer) < 2:
            return frames
        header = buffer[1]
        if header & 0xF0 != 0xC0:
            del buffer[0]
            continue
        data_length = header & 0x0F
        packet_length = 5 + data_length
        if len(buffer) < packet_length:
            return frames
        if buffer[packet_length - 1] != 0x55:
            del buffer[0]
            continue
        arbitration_id = int.from_bytes(buffer[2:4], "little")
        data = bytes(buffer[4:4 + data_length])
        del buffer[:packet_length]
        if arbitration_id <= 0x7FF:
            frames.append(CanFrame(arbitration_id, data))


class WaveshareUsbCanABus:
    """CAN reader/writer for Waveshare USB-CAN-A adapters exposed as /dev/ttyUSB*."""

    def __init__(self, port: str, bitrate: int) -> None:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on deployed environment.
            raise RuntimeError("Waveshare USB-CAN-A output requires the pyserial package.") from exc

        self._serial = serial.Serial(
            port=port,
            baudrate=WAVESHARE_USB_SERIAL_BAUDRATE,
            timeout=0.1,
            write_timeout=0.1,
        )
        try:
            self._write(build_waveshare_configuration(bitrate))
        except Exception:
            self._serial.close()
            raise
        self._receive_buffer = bytearray()

    def _write(self, packet: bytes) -> None:
        written = self._serial.write(packet)
        if written != len(packet):
            raise OSError(f"Wrote {written} of {len(packet)} bytes to Waveshare USB-CAN-A.")
        self._serial.flush()

    def send(self, frame: CanFrame) -> None:
        self._write(encode_waveshare_frame(frame))

    def recv(self, timeout: float = 0.1) -> CanFrame | None:
        """Return the next received CAN frame, or None when no complete frame arrives."""
        frames = decode_waveshare_frames(self._receive_buffer)
        if frames:
            return frames[0]
        original_timeout = self._serial.timeout
        self._serial.timeout = timeout
        try:
            first_byte = self._serial.read(1)
            if not first_byte:
                return None
            self._receive_buffer.extend(first_byte)
            waiting = self._serial.in_waiting
            if waiting:
                self._receive_buffer.extend(self._serial.read(waiting))
        finally:
            self._serial.timeout = original_timeout
        frames = decode_waveshare_frames(self._receive_buffer)
        return frames[0] if frames else None

    def shutdown(self) -> None:
        self._serial.close()


def _unsigned_16(value: float) -> bytes:
    integer = round(value)
    if not 0 <= integer <= 0xFFFF:
        raise ValueError(f"Unsigned 16-bit CAN value is out of range: {value!r}")
    return integer.to_bytes(2, byteorder="little", signed=False)


def _signed_16(value: float) -> bytes:
    integer = round(value)
    if not -0x8000 <= integer <= 0x7FFF:
        raise ValueError(f"Signed 16-bit CAN value is out of range: {value!r}")
    return integer.to_bytes(2, byteorder="little", signed=True)


def build_luxpower_frames(battery: VirtualBattery) -> tuple[CanFrame, ...]:
    """Encode an eligible virtual battery as YamBMS-compatible LuxPower frames.

    Frame 0x351 uses the virtual battery's effective voltage and current limits.
    Those limits are dynamically reduced to zero when the associated BMS safety
    or MOSFET permission is unavailable, and charge is cut off at the configured
    charge-voltage limit.
    """
    required_values = (
        battery.pack_voltage_v,
        battery.pack_current_a,
        battery.state_of_charge_percent,
        battery.state_of_health_percent,
        battery.rated_capacity_ah,
        battery.charge_voltage_limit_v,
        battery.charge_current_limit_a,
        battery.discharge_voltage_limit_v,
        battery.discharge_current_limit_a,
        battery.minimum_cell_voltage_v,
        battery.maximum_cell_voltage_v,
        battery.minimum_temperature_c,
        battery.maximum_temperature_c,
        battery.cycle_count,
    )
    if battery.member_count == 0 or any(value is None for value in required_values):
        return ()

    assert battery.pack_voltage_v is not None
    assert battery.pack_current_a is not None
    assert battery.state_of_charge_percent is not None
    assert battery.state_of_health_percent is not None
    assert battery.rated_capacity_ah is not None
    assert battery.charge_voltage_limit_v is not None
    assert battery.charge_current_limit_a is not None
    assert battery.discharge_voltage_limit_v is not None
    assert battery.discharge_current_limit_a is not None
    assert battery.minimum_cell_voltage_v is not None
    assert battery.maximum_cell_voltage_v is not None
    assert battery.minimum_temperature_c is not None
    assert battery.maximum_temperature_c is not None
    assert battery.cycle_count is not None

    limits = (
        _unsigned_16(battery.charge_voltage_limit_v * 10)
        + _unsigned_16(battery.charge_current_limit_a * 10)
        + _unsigned_16(battery.discharge_current_limit_a * 10)
        + _unsigned_16(battery.discharge_voltage_limit_v * 10)
    )
    soc_and_cells = (
        _unsigned_16(battery.state_of_charge_percent)
        + _unsigned_16(battery.state_of_health_percent)
        + _unsigned_16(battery.maximum_cell_voltage_v * 1000)
        + _unsigned_16(battery.minimum_cell_voltage_v * 1000)
    )
    state = (
        _unsigned_16(battery.pack_voltage_v * 100)
        + _signed_16(battery.pack_current_a * 10)
        + _signed_16(battery.maximum_temperature_c * 10 + 1)
        + _signed_16(battery.minimum_temperature_c * 10)
    )
    protection = bytes((0, 0, 0, 0, battery.member_count & 0xFF)) + _unsigned_16(battery.rated_capacity_ah) + bytes((0,))
    enable_flags = (0x40 if battery.discharge_enabled else 0) | (0x80 if battery.charge_enabled else 0)
    flags = bytes((enable_flags, 0)) + _unsigned_16(battery.cycle_count) + bytes(4)

    return (
        CanFrame(0x35E, b"YamBMS\x00\x00"),
        CanFrame(0x351, limits),
        CanFrame(0x355, soc_and_cells),
        CanFrame(0x356, state),
        CanFrame(0x359, protection),
        CanFrame(0x35C, flags),
    )


class LuxPowerCanPublisher:
    """Continuously publish the newest virtual-battery snapshot to a CAN port."""

    def __init__(self, port: str, bitrate: int = 500_000, logger: logging.Logger | None = None) -> None:
        self.port = port
        self.bitrate = bitrate
        self.logger = logger or logging.getLogger("battery_manager")
        self._lock = threading.Lock()
        self._frames: tuple[CanFrame, ...] = ()
        self._last_sent: float | None = None
        self._first_transmission_at: float | None = None
        self._last_received: float | None = None
        self._last_heartbeat: float | None = None
        self._last_received_frame_id: int | None = None
        self._link_state = "awaiting"
        self._next_reconnect_at: float | None = None
        self._last_reconnect_at: float | None = None
        self._bus_lock = threading.Lock()
        self._last_error: str | None = None
        self._stop_event = threading.Event()
        self._frame_index = 0
        self._bus = self._open_bus()
        self._thread = threading.Thread(target=self._run, name=f"luxpower-can:{port}", daemon=True)
        self._reader_thread = threading.Thread(target=self._read_loop, name=f"luxpower-can-rx:{port}", daemon=True)
        self._thread.start()
        self._reader_thread.start()

    def _open_bus(self):
        if self.port.startswith("/dev/"):
            try:
                return WaveshareUsbCanABus(self.port, self.bitrate)
            except (OSError, ValueError) as exc:  # pragma: no cover - hardware-dependent path.
                raise RuntimeError(f"Unable to initialize Waveshare USB-CAN-A on {self.port!r}: {exc}") from exc

        try:
            import can
        except ImportError as exc:  # pragma: no cover - depends on deployed environment.
            raise RuntimeError("SocketCAN output requires the python-can package. Install the project's dependencies first.") from exc
        try:
            return can.Bus(interface="socketcan", channel=self.port)
        except (can.CanError, OSError) as exc:  # pragma: no cover - hardware-dependent path.
            raise RuntimeError(f"Unable to open CAN port {self.port!r}: {exc}") from exc

    def update(self, battery: VirtualBattery) -> None:
        """Replace the transmitted snapshot. Incomplete data stops all frames."""
        frames = build_luxpower_frames(battery)
        with self._lock:
            self._frames = frames
            self._frame_index = 0

    def status_summary(self) -> str:
        """Return the CAN output state for the manager dashboard."""
        with self._lock:
            if self._last_error is not None:
                return f"CAN: ERROR: {self._last_error}"
            received_at = (
                time.strftime("%H:%M:%S", time.localtime(self._last_received))
                if self._last_received is not None else "never"
            )
            received_detail = f"last RX {received_at}"
            if self._last_received_frame_id is not None:
                received_detail += f" (0x{self._last_received_frame_id:03X})"
            if self._link_state == "failed":
                retry_at = self._next_reconnect_at
                retry_text = time.strftime("%H:%M:%S", time.localtime(retry_at)) if retry_at is not None else "unknown"
                return f"CAN: link failed; retrying at {retry_text}; {received_detail} ({self.port})"
            if self._link_state == "retrying":
                return f"CAN: retrying; awaiting 0x{CAN_HEARTBEAT_FRAME_ID:03X}; {received_detail} ({self.port})"
            if self._link_state == "awaiting" and self._first_transmission_at is not None:
                return f"CAN: awaiting 0x{CAN_HEARTBEAT_FRAME_ID:03X}; {received_detail} ({self.port})"
            if self._last_sent is not None:
                sent_at = time.strftime("%H:%M:%S", time.localtime(self._last_sent))
                return f"CAN: last TX {sent_at}; {received_detail} ({self.port})"
            if self._frames:
                return f"CAN: ready; awaiting first TX; {received_detail} ({self.port})"
            return f"CAN: waiting for complete virtual battery data ({self.port})"

    def _record_received_frame(self, frame: CanFrame) -> None:
        now = time.time()
        with self._lock:
            self._last_received = now
            self._last_received_frame_id = frame.arbitration_id
            if frame.arbitration_id == CAN_HEARTBEAT_FRAME_ID:
                self._last_heartbeat = now
                self._link_state = "healthy"
                self._next_reconnect_at = None

    def _read_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._bus_lock:
                    bus = self._bus
                    frame = bus.recv(timeout=CAN_UPDATE_INTERVAL_SECONDS)
                if frame is not None:
                    self._record_received_frame(CanFrame(frame.arbitration_id, bytes(frame.data)))
            except Exception as exc:  # pragma: no cover - hardware-dependent path.
                if not self._stop_event.is_set():
                    with self._lock:
                        self._last_error = f"CAN receive failed: {exc}"
                    self.logger.error("Failed to receive CAN frame on %s: %s", self.port, exc)

    def _fail_link_if_heartbeat_missing(self, now: float) -> None:
        with self._lock:
            if self._link_state not in {"awaiting", "retrying", "healthy"} or self._first_transmission_at is None:
                return
            reference = self._last_heartbeat or self._first_transmission_at
            if now - reference < CAN_HEARTBEAT_TIMEOUT_SECONDS:
                return
            self._link_state = "failed"
            self._next_reconnect_at = now + CAN_RECONNECT_DELAY_SECONDS
        self.logger.error(
            "CAN inverter heartbeat 0x%03X missing for %.0f seconds on %s; pausing output until retry.",
            CAN_HEARTBEAT_FRAME_ID,
            CAN_HEARTBEAT_TIMEOUT_SECONDS,
            self.port,
        )

    def _reconnect_bus(self) -> None:
        try:
            with self._bus_lock:
                self._bus.shutdown()
                self._bus = self._open_bus()
            with self._lock:
                self._last_error = None
                self._last_heartbeat = None
                self._first_transmission_at = None
                self._link_state = "retrying"
                self._next_reconnect_at = None
                self._last_reconnect_at = time.time()
            self.logger.warning("Reinitialized CAN transport on %s; awaiting inverter heartbeat.", self.port)
        except Exception as exc:  # pragma: no cover - hardware-dependent path.
            with self._lock:
                self._last_error = f"CAN reconnect failed: {exc}"
                self._next_reconnect_at = time.time() + CAN_RECONNECT_DELAY_SECONDS
            self.logger.error("Unable to reinitialize CAN transport on %s: %s", self.port, exc)

    def _run(self) -> None:
        while not self._stop_event.wait(CAN_UPDATE_INTERVAL_SECONDS):
            now = time.time()
            self._fail_link_if_heartbeat_missing(now)
            with self._lock:
                if self._link_state == "failed":
                    reconnect = self._next_reconnect_at is not None and now >= self._next_reconnect_at
                    if not reconnect:
                        continue
                else:
                    reconnect = False
                if reconnect:
                    frame = None
                elif not self._frames:
                    continue
                else:
                    frame = self._frames[self._frame_index]
                    self._frame_index = (self._frame_index + 1) % len(self._frames)
            if reconnect:
                self._reconnect_bus()
                continue
            assert frame is not None
            try:
                with self._bus_lock:
                    if isinstance(self._bus, WaveshareUsbCanABus):
                        self._bus.send(frame)
                    else:
                        import can

                        self._bus.send(
                            can.Message(arbitration_id=frame.arbitration_id, data=frame.data, is_extended_id=False),
                            timeout=CAN_UPDATE_INTERVAL_SECONDS,
                        )
                with self._lock:
                    self._last_sent = time.time()
                    self._first_transmission_at = self._first_transmission_at or self._last_sent
                    self._last_error = None
            except Exception as exc:  # pragma: no cover - hardware-dependent path.
                with self._lock:
                    self._last_error = str(exc)
                self.logger.error("Failed to send LuxPower CAN frame on %s: %s", self.port, exc)

    def close(self) -> None:
        """Stop output promptly and release the CAN interface."""
        self._stop_event.set()
        self._thread.join(timeout=1)
        self._reader_thread.join(timeout=1)
        with self._bus_lock:
            self._bus.shutdown()
