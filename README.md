# battery-manager
Collection of code for managing a group of Daly BMSs in a cluster, with multiple RS-485 busses.

## Virtual battery

The manager combines reports from batteries with a valid integer `id`, `cabinet`,
`row`, and `depth` in [battery-config.json](battery-config.json). The aggregate is
implemented in [virtual_battery.py](virtual_battery.py), displayed on the terminal
dashboard, and published as the Home Assistant MQTT device `Daly Virtual Battery`.

The `virtual_battery` configuration section defines the $49.8\,V$ charge-voltage
limit, $40\,V$ discharge-voltage limit, $100\,A$ charge/discharge current limits,
and $100\%$ state of health sent to the inverter-facing model.

Only batteries excluded from the aggregate need a flag: set
`"virtual_battery_disabled": true` to exclude that BMS's data from the virtual
battery. This is useful for an installed but electrically disconnected battery
during testing. When omitted, the flag defaults to `false`; individual BMS
monitoring and Home Assistant publishing continue unchanged.

For parallel batteries, voltage, state of charge, and temperature are averaged;
current, rated capacity, and remaining capacity are summed; the minimum/maximum
cell and temperature readings are preserved; and the cycle count is a rounded
mean. Charge and
discharge are enabled only when every contributing BMS is safe and has the relevant
MOSFET enabled.

## LuxPower / EG4 CAN output

Optional CAN output follows YamBMS' LuxPower mode at 500 kbit/s, rotating the
standard 11-bit frames `0x35E`, `0x351`, `0x355`, `0x356`, `0x359`, and `0x35C`
every 100 ms. Enable it by passing `--can-port`:

- A SocketCAN interface such as `can0` uses the operating system's configured CAN
	bitrate.
- A Waveshare USB-CAN-A serial adapter such as `/dev/ttyUSB1` is initialized
	using Waveshare's binary serial protocol at its required 2 Mbit/s USB baud
	rate. `--can-bitrate` configures its CAN bus rate and defaults to 500000.

The configured CAN port is automatically excluded from Daly serial-port scans.
CAN output is entirely disabled unless `--can-port` is supplied.

The CAN publisher monitors received inverter traffic. It expects a standard
`0x305` heartbeat frame; after five seconds without one it pauses transmission,
then closes and reinitializes the CAN transport after thirty seconds. The dashboard
shows the last received-frame timestamp and link retry state. This detects
protocol-level inverter responsiveness, rather than merely confirming that bytes
were written to the USB adapter.

The installed dependency is listed in [requirements.txt](requirements.txt). Frame
`0x351` advertises the configured static charge/discharge voltage and current
limits.
