"""SigenStor Modbus register map (read-only subset).

Source: Sigenergy Modbus Protocol (V2.7-V2.9) as implemented by the
TypQxQ/Sigenergy-Local-Modbus Home Assistant integration.

All registers here are *input registers* read with function code 0x04.
The PDU address is the full register number (e.g. 30014) - do not subtract 30001.

Sign conventions (plant):
  grid_power   > 0  = importing from grid,  < 0 = exporting
  battery_power> 0  = charging,             < 0 = discharging
"""

PLANT_UNIT_ID = 247  # "plant" address: whole-system values
MAX_PV_STRINGS = 36  # the protocol defines PV1..PV36; register 31025 says how many exist

# name, address, data type, gain (raw / gain = value), unit
# Data types: u16, s16, u32, s32, u64, str (count given separately for str)
PLANT = [
    ("system_time",            30000, "u32", 1,    "s"),
    ("timezone_offset",        30002, "s16", 1,    "min"),
    ("ems_work_mode",          30003, "u16", 1,    ""),
    ("grid_sensor_status",     30004, "u16", 1,    ""),
    ("grid_power",             30005, "s32", 1,    "W"),
    ("on_off_grid",            30009, "u16", 1,    ""),
    ("max_active_power",       30010, "u32", 1,    "W"),
    ("battery_soc",            30014, "u16", 10,   "%"),
    ("alarm1",                 30027, "u16", 1,    ""),
    ("alarm2",                 30028, "u16", 1,    ""),
    ("alarm3",                 30029, "u16", 1,    ""),
    ("alarm4",                 30030, "u16", 1,    ""),
    ("plant_active_power",     30031, "s32", 1,    "W"),
    ("pv_power",               30035, "s32", 1,    "W"),
    ("battery_power",          30037, "s32", 1,    "W"),
    ("battery_max_charge_power",    30047, "u32", 1, "W"),
    ("battery_max_discharge_power", 30049, "u32", 1, "W"),
    ("running_state",          30051, "u16", 1,    ""),
    ("battery_chargeable_kwh",    30064, "u32", 100, "kWh"),
    ("battery_dischargeable_kwh", 30066, "u32", 100, "kWh"),
    ("alarm5",                 30072, "u16", 1,    ""),
    ("battery_rated_kwh",      30083, "u32", 100,  "kWh"),
    ("charge_cutoff_soc",      30085, "u16", 10,   "%"),
    ("discharge_cutoff_soc",   30086, "u16", 10,   "%"),
    ("battery_soh",            30087, "u16", 10,   "%"),
    ("total_pv_kwh",           30088, "u64", 100,  "kWh"),
    ("daily_load_kwh",         30092, "u32", 100,  "kWh"),
    ("total_load_kwh",         30094, "u64", 100,  "kWh"),
    ("total_charge_kwh",       30200, "u64", 100,  "kWh"),
    ("total_discharge_kwh",    30204, "u64", 100,  "kWh"),
    ("total_import_kwh",       30216, "u64", 100,  "kWh"),
    ("total_export_kwh",       30220, "u64", 100,  "kWh"),
    ("daily_pv_kwh",           30272, "u32", 100,  "kWh"),
    ("yesterday_pv_kwh",       30274, "u32", 100,  "kWh"),
    ("alarm6",                 30280, "u16", 1,    ""),
    ("alarm7",                 30281, "u16", 1,    ""),
    ("general_load_power",     30282, "s32", 1,    "W"),
    ("load_power",             30284, "s32", 1,    "W"),
    ("battery_avg_cell_temp",  30286, "s16", 10,   "°C"),
]

# Per-inverter registers (unit id = the inverter's Modbus device id, usually 1)
INVERTER = [
    ("model",                  30500, "str15", 1,  ""),
    ("serial",                 30515, "str10", 1,  ""),
    ("firmware",               30525, "str15", 1,  ""),
    ("rated_power",            30540, "u32", 1,    "W"),
    ("daily_charge_kwh",       30566, "u32", 100,  "kWh"),
    ("daily_discharge_kwh",    30572, "u32", 100,  "kWh"),
    ("running_state",          30578, "u16", 1,    ""),
    ("active_power",           30587, "s32", 1,    "W"),
    ("battery_power",          30599, "s32", 1,    "W"),
    ("battery_soc",            30601, "u16", 10,   "%"),
    ("battery_soh",            30602, "u16", 10,   "%"),
    ("battery_avg_cell_temp",  30603, "s16", 10,   "°C"),
    ("battery_avg_cell_voltage", 30604, "u16", 1000, "V"),
    ("alarm1",                 30605, "u16", 1,    ""),
    ("alarm2",                 30606, "u16", 1,    ""),
    ("alarm3",                 30607, "u16", 1,    ""),
    ("alarm4",                 30608, "u16", 1,    ""),
    ("alarm5",                 30609, "u16", 1,    ""),
    ("battery_max_temp",       30620, "s16", 10,   "°C"),
    ("battery_min_temp",       30621, "s16", 10,   "°C"),
    ("battery_max_cell_voltage", 30622, "u16", 1000, "V"),
    ("battery_min_cell_voltage", 30623, "u16", 1000, "V"),
    ("grid_frequency",         31002, "u16", 100,  "Hz"),
    ("pcs_temp",               31003, "s16", 10,   "°C"),
    ("phase_a_voltage",        31011, "u32", 100,  "V"),
    ("phase_a_current",        31017, "s32", 100,  "A"),
    ("pack_count",             31024, "u16", 1,    ""),
    ("pv_string_count",        31025, "u16", 1,    ""),
    ("mppt_count",             31026, "u16", 1,    ""),
    ("pv1_voltage",            31027, "s16", 10,   "V"),
    ("pv1_current",            31028, "s16", 100,  "A"),
    ("pv2_voltage",            31029, "s16", 10,   "V"),
    ("pv2_current",            31030, "s16", 100,  "A"),
    ("pv3_voltage",            31031, "s16", 10,   "V"),
    ("pv3_current",            31032, "s16", 100,  "A"),
    ("pv4_voltage",            31033, "s16", 10,   "V"),
    ("pv4_current",            31034, "s16", 100,  "A"),
    ("pv_power",               31035, "s32", 1,    "W"),
    *[(f"pv{n}_{k}", 31042 + (n - 5) * 2 + i, "s16", g, u)
      for n in range(5, 37) for i, (k, g, u) in enumerate((("voltage", 10, "V"), ("current", 100, "A")))],
    ("insulation_resistance",  31037, "u16", 1000, "MΩ"),
    ("daily_pv_kwh",           31509, "u32", 100,  "kWh"),
]


def reg_count(dtype):
    if dtype.startswith("str"):
        return int(dtype[3:])
    return {"u16": 1, "s16": 1, "u32": 2, "s32": 2, "u64": 4}[dtype]


RUNNING_STATE = {0: "Standby", 1: "Running", 2: "Fault", 3: "Shutdown", 7: "Environmental abnormality"}
EMS_WORK_MODE = {
    0: "Maximum self-consumption", 1: "AI mode", 2: "Time of use",
    5: "Full feed-in to grid", 6: "VPP scheduling", 7: "Remote EMS", 9: "Custom",
}
ON_OFF_GRID = {0: "On grid", 1: "Off grid (backup, automatic)", 2: "Off grid (manual)"}

# Bit -> description, per alarm word (plant registers alarm1..alarm7)
ALARM_BITS = {
    "alarm1": ("Inverter", {
        0: "Software version mismatch", 1: "Low insulation resistance", 2: "Over-temperature",
        3: "Equipment fault", 4: "System grounding fault", 5: "PV string over-voltage",
        6: "PV string reversely connected", 7: "PV string back-filling", 8: "AFCI fault",
        9: "Grid power outage", 10: "Grid over-voltage", 11: "Grid under-voltage",
        12: "Grid over-frequency", 13: "Grid under-frequency", 14: "Grid voltage imbalance",
        15: "DC component of output current out of limit"}),
    "alarm2": ("Inverter", {
        0: "Leak current out of limit", 1: "Communication abnormal", 2: "System internal protection",
        3: "AFCI self-check circuit fault", 4: "Off-grid protection", 5: "Manual operation protection",
        7: "Abnormal phase sequence", 8: "Short circuit to PE", 9: "Soft start failure"}),
    "alarm3": ("Battery", {
        0: "Software version mismatch", 1: "Low insulation resistance to ground",
        2: "Temperature too high", 3: "Equipment fault", 4: "Under-temperature",
        5: "Internal protection", 6: "Thermal runaway"}),
    "alarm4": ("Gateway", {
        0: "Software version mismatch", 1: "Temperature too high", 2: "Equipment fault",
        3: "Excessive leakage current in off-grid output", 4: "N line grounding fault",
        5: "Abnormal phase sequence of grid wiring", 6: "Abnormal phase sequence of inverter wiring",
        7: "Grid phase loss"}),
    "alarm5": ("DC charger", {
        0: "Software version mismatch", 1: "Low insulation resistance to ground",
        2: "Over-temperature", 3: "Equipment fault", 4: "Charging fault", 5: "Equipment protection"}),
    "alarm6": ("System", {
        0: "Gateway communication abnormal", 1: "Meter communication abnormal",
        2: "AC power sensor communication abnormal",
        6: "Hard protection against grid-feed power limit exceedance",
        8: "Generator failure to start", 10: "CLS fault"}),
    "alarm7": ("System", {0: "OVGR fault", 1: "RPR fault"}),
}


def decode_alarms(values):
    """Decode the alarm bit-fields.

    Returns (alarms, ignored): alarms is a list of {source, message, register, bit};
    ignored maps register -> raw value for words that were skipped because they have
    bits set that the protocol doesn't define. Firmware that doesn't implement an alarm
    register returns a filler value (often 0xFFFF); taking it at face value would show a
    wall of bogus faults, so such a word is treated as "not supported" instead.
    """
    out, ignored = [], {}
    for key, (source, bits) in ALARM_BITS.items():
        try:
            word = int(values.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if not word:
            continue
        defined = sum(1 << b for b in bits)
        if word & ~defined & 0xFFFF:
            ignored[key] = word
            continue
        for bit in range(16):
            if word & (1 << bit):
                out.append({"source": source, "message": bits[bit], "register": key, "bit": bit})
    return out, ignored
