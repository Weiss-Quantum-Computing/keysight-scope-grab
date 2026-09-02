"""The fixtures the output tests are built from.

Kept in one place so the golden files and the test that checks them cannot
drift: both the checker and the regenerator read the cases from here.

Nothing in this module imports scope_grab, so it describes what a scope would
have said without needing one - or the program - to be present.
"""

# A settings snapshot as read_all_settings() returns it: the instrument's own
# reply strings, unrounded, exactly as they arrive over VISA. The odd-looking
# formats are deliberate - "+2.00000000E+09" and a bare "0" for a boolean are
# what an MSO-X actually sends, and metadata() writes them through verbatim.
MSOX_SNAPSHOT = {
    ":ACQuire:SRATe": "+2.00000000E+09",
    ":ACQuire:POINts": "+7680",
    ":ACQuire:TYPE": "AVER",
    ":ACQuire:COUNt": "+256",
    ":WAVeform:COUNt": "+256",
    ":TIMebase:SCALe": "+1.00000000E-03",
    ":TIMebase:POSition": "+0.0E+00",
    ":TIMebase:REFerence": "CENT",
    ":TIMebase:MODE": "MAIN",
    ":TRIGger:MODE": "EDGE",
    ":TRIGger:SWEep": "NORM",
    ":TRIGger:EDGE:SOURce": "CHAN1",
    ":TRIGger:EDGE:LEVel": "+1.20000E+00",
    ":TRIGger:EDGE:SLOPe": "POS",
    ":TRIGger:EDGE:REJect": "OFF",
    ":TRIGger:NREJect": "0",
    ":TRIGger:HOLDoff": "+6.0E-08",
}
for _ch in (1, 2, 3, 4):
    MSOX_SNAPSHOT.update({
        f":CHANnel{_ch}:SCALe": "+4.00000E-02",
        f":CHANnel{_ch}:OFFSet": "-1.0E-01",
        f":CHANnel{_ch}:COUPling": "DC",
        f":CHANnel{_ch}:PROBe": "+1.0E+00",
        f":CHANnel{_ch}:UNITs": "VOLT",
        f":CHANnel{_ch}:BWLimit": "0",
        f":CHANnel{_ch}:INVert": "0",
        f":CHANnel{_ch}:DISPlay": "1",
    })

_PLAIN = dict(MSOX_SNAPSHOT, **{":ACQuire:TYPE": "NORM"})
del _PLAIN[":WAVeform:COUNt"]

_NO_COUNT = {k: v for k, v in MSOX_SNAPSHOT.items() if k != ":WAVeform:COUNt"}

# (golden file stem, kwargs for Scope.metadata). Between them these cover every
# branch in the layout: the averaging note on and off, the hit-count line
# present and absent, the sequence label, the existing-trace line, named and
# unnamed channels, and a snapshot with nothing in it at all.
METADATA_CASES = [
    ("averaged_named_sequence", dict(
        channels=[1, 2], settings=MSOX_SNAPSHOT,
        names={1: "PD monitor", 2: "ramp"}, label="007", existing=False)),
    ("plain_single_channel", dict(
        channels=[1], settings=_PLAIN, names=None, label=None, existing=False)),
    ("existing_trace_four_channels", dict(
        channels=[1, 2, 3, 4], settings=MSOX_SNAPSHOT,
        names={1: "PD", 3: "trig"}, label=None, existing=True)),
    ("averaging_without_hit_count", dict(
        channels=[1], settings=_NO_COUNT, names=None, label=None,
        existing=False)),
    ("empty_snapshot", dict(
        channels=[1], settings={}, names=None, label=None, existing=False)),
]

# Stamped into every metadata golden in place of the real clock and the real
# instrument, so a rerun produces the same bytes.
FROZEN_TIME = (2026, 9, 2, 14, 30, 15, 123456)
IDN = "KEYSIGHT TECHNOLOGIES,MSO-X 2014A,MY12345678,02.65.2021012345"
ADDR = "USB0::0x2A8D::0x1797::MY12345678::INSTR"
