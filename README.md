# battery-manager
Collection of code for managing a group of Daly BMSs in a cluster, with multiple RS-485 busses.

## Virtual battery

The manager combines reports from batteries with a valid integer `id`, `cabinet`,
`row`, and `depth` in [battery-config.json](battery-config.json). The aggregate is
implemented in [virtual_battery.py](virtual_battery.py), displayed on the terminal
dashboard, and published as the Home Assistant MQTT device `Daly Virtual Battery`.

For parallel batteries, voltage, state of charge, and temperature are averaged;
current and reported capacity are summed; the minimum/maximum cell and temperature
readings are preserved; and the highest cycle count is retained. Charge and
discharge are enabled only when every contributing BMS is safe and has the relevant
MOSFET enabled.

The virtual model includes fields required for a future EG4/Luxpower CAN encoder:
charge/discharge voltage and current limits, SoC, SoH, cell and temperature extrema,
pack voltage/current, capacity, flags, cycle count, and identifier. Daly reports do
not currently provide SoH or inverter request limits, so those fields publish as
Home Assistant `unknown` (`None`) until a future control policy/configuration
defines them. CAN transport is intentionally not implemented yet.
