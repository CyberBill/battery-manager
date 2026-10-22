# battery-manager
Collection of code for managing a group of Daly BMSs in a cluster, with multiple RS-485 busses.

## Virtual battery

The manager combines reports from batteries with a valid integer `id`, `cabinet`,
`row`, and `depth` in [battery-config.json](battery-config.json). The aggregate is
implemented in [virtual_battery.py](virtual_battery.py), displayed on the terminal
dashboard, and published as the Home Assistant MQTT device `Daly Virtual Battery`.

The `virtual_battery` configuration section defines charge/discharge voltage
limits, state of health, and three configurable current caps:

- `charge_current_per_bms_a` and `discharge_current_per_bms_a`: per-member
	current contribution (for example $20\,A$ each direction).
- `current_per_cabinet_a`: shared per-cabinet maximum for both charge and
	discharge (for example $100\,A$).
- `current_limit_a`: shared whole-array maximum for both charge and discharge
	(for example $250\,A$).

The effective current limit is the minimum of those applicable caps:

$$
I_\text{effective,dir}=\min\left(I_\text{array},\;N_\text{members}\cdot I_\text{per BMS,dir},\;N_\text{cabinets}\cdot I_\text{per cabinet}\right)
$$

If per-BMS and per-cabinet fields are omitted, `current_limit_a` behaves as a
fixed current limit in both directions.

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
`0x351` advertises the configured voltage limits and the effective current limits.
