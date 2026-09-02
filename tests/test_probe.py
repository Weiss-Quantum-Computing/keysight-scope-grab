"""Run tools/probe_scope.py against a fake instrument.

The probe only earns its keep if it survives contact with a scope, and the
place not to discover a typo in it is standing at the bench with an experiment
paused. So: a mock that answers like a DS1000Z, and a second one that refuses
almost everything, driven through every phase.

This does not check that the probe's conclusions are right - only a real scope
can say that. It checks that every phase runs, handles a refusal, and restores
what it touched.

    python tests/test_probe.py
"""
import importlib.util
import io
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location(
    "probe_scope", os.path.join(REPO, "tools", "probe_scope.py"))
probe_scope = importlib.util.module_from_spec(spec)
sys.modules["probe_scope"] = probe_scope
spec.loader.exec_module(probe_scope)

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILS.append(label)


class FakeScope:
    """Answers like a DS1000Z, well enough to drive every phase.

    `unsupported` is a list of substrings; a query matching one raises, which
    is how a scope declines - it pushes an error and sends nothing rather than
    replying "no".
    """

    PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 4 + b"IHDR"
           + (800).to_bytes(4, "big") + (480).to_bytes(4, "big")
           + b"\x00" * 200)

    def __init__(self, idn="RIGOL TECHNOLOGIES,DS1054Z,DS1ZA1,00.04.04.SP4",
                 unsupported=()):
        self.idn = idn
        self.unsupported = unsupported
        self.timeout = 5000
        self.chunk_size = 0
        self.read_termination = self.write_termination = None
        self.written = []
        self.cleared = 0
        self.state = {
            ":ACQuire:TYPE": "NORM", ":ACQuire:AVERages": "2",
            ":ACQuire:MDEPth": "AUTO", ":ACQuire:SRATe": "1.000000e+09",
            ":TIMebase:MAIN:SCALe": "1.000000e-03",
            ":TIMebase:MAIN:OFFSet": "0.000000e+00",
            ":TIMebase:MODE": "MAIN", ":TRIGger:MODE": "EDGE",
            ":TRIGger:SWEep": "AUTO", ":TRIGger:COUPling": "DC",
            ":TRIGger:HOLDoff": "1.6e-08", ":TRIGger:NREJect": "0",
            ":TRIGger:STATus": "RUN", ":TRIGger:EDGe:SOURce": "CHAN1",
            ":TRIGger:EDGe:SLOPe": "POS", ":TRIGger:EDGe:LEVel": "1.2e+00",
        }
        for ch in (1, 2, 3, 4):
            self.state.update({
                f":CHANnel{ch}:SCALe": "1.000000e+00",
                f":CHANnel{ch}:OFFSet": "0.000000e+00",
                f":CHANnel{ch}:COUPling": "DC", f":CHANnel{ch}:PROBe": "1",
                f":CHANnel{ch}:UNITs": "VOLT", f":CHANnel{ch}:BWLimit": "OFF",
                f":CHANnel{ch}:INVert": "0", f":CHANnel{ch}:DISPlay": "1",
            })

    # -- pyvisa surface ---------------------------------------------------

    def _refuses(self, scpi):
        return any(u in scpi for u in self.unsupported)

    def query(self, scpi):
        scpi = scpi.strip()
        if self._refuses(scpi):
            raise IOError("timeout")
        if scpi == "*IDN?":
            return self.idn + "\n"
        if scpi == ":SYSTem:ERRor?":
            return '0,"No error"\n'
        if scpi == ":WAVeform:PREamble?":
            count = (self.state[":ACQuire:AVERages"]
                     if self.state[":ACQuire:TYPE"].startswith("AVER") else "1")
            return f"0,0,1200,{count},1.0e-09,-6.0e-04,0,4.0e-03,0,127\n"
        if scpi.endswith("?"):
            root = scpi[:-1]
            if root in self.state:
                return self.state[root] + "\n"
            raise IOError("timeout")
        raise IOError("not a query: " + scpi)

    def write(self, scpi):
        self.written.append(scpi)
        if self._refuses(scpi):
            return
        parts = scpi.split(" ", 1)
        if len(parts) == 2 and parts[0] in self.state:
            self.state[parts[0]] = parts[1]
        if scpi == ":RUN":
            self.state[":TRIGger:STATus"] = "RUN"
        elif scpi in (":STOP", ":SINGle"):
            self.state[":TRIGger:STATus"] = "STOP"

    def query_binary_values(self, scpi, datatype="B", container=None):
        if self._refuses(scpi):
            raise IOError("timeout")
        if scpi.startswith(":DISPlay:DATA"):
            return bytearray(self.PNG)
        # A noisy sine whose noise falls with the average count, so the probe's
        # roughness ruler has something real to measure.
        n = 1200
        depth = (int(self.state[":ACQuire:AVERages"])
                 if self.state[":ACQuire:TYPE"].startswith("AVER") else 1)
        rng = np.random.default_rng(0)
        sig = 40 * np.sin(np.linspace(0, 8 * np.pi, n))
        noise = rng.normal(0, 12 / np.sqrt(depth), n)
        return np.clip(127 + sig + noise, 0, 255).astype(
            np.uint16 if datatype == "H" else np.uint8)

    def clear(self):
        self.cleared += 1

    def close(self):
        pass


def run_phases(scope, label):
    """Every phase against one mock, with output captured."""
    say = probe_scope.Report()
    buf = io.StringIO()
    real_stdout, sys.stdout = sys.stdout, buf
    try:
        p = probe_scope.Probe(scope, say)
        # Real sleeps would make this take minutes; the phases only sleep to
        # let the scope catch up, which a mock never needs.
        real_sleep, probe_scope.time.sleep = probe_scope.time.sleep, lambda s: None
        try:
            probe_scope.phase_identity(p, say)
            probe_scope.phase_settings(p, say, 1)
            probe_scope.phase_readout(p, say, 1)
            probe_scope.phase_screenshot(p, say)
            probe_scope.phase_averaging(p, say, 1, 64)
        finally:
            probe_scope.time.sleep = real_sleep
    finally:
        sys.stdout = real_stdout
    return "\n".join(say.lines)


def main():
    print("probe against a DS1054Z-like mock")
    scope = FakeScope()
    try:
        out = run_phases(scope, "rigol")
        check("all five phases ran", True)
    except Exception as exc:
        check("all five phases ran", False, f"{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    for phase in ("1. Identity", "2. Settings", "3. Waveform readout",
                  "4. Screenshot", "5. Averaging"):
        check(f"report has {phase!r}", phase in out)
    check("identified the scope", "DS1054Z" in out)
    check("found the PNG and its size", "800x480" in out)
    check("noticed unsupported commands",
          "NO ANSWER" in out, "(:ACQuire:COUNt etc. are not Rigol)")
    check("measured a noise ruler",
          re.search(r"unaveraged noise ruler: [\d.]+e?-?\d*", out) is not None)
    check("read a trace in every mode it tried", "1200 pts" in out)
    check("probed :DIGitize", ":DIGitize" in out)
    check("restored nothing itself (main() does that)",
          ":ACQuire:TYPE NORMal" in scope.written)

    # The averaging ruler has to actually respond to depth, or the phase is
    # measuring nothing and would report a real scope as flat.
    ratios = [float(m) for m in re.findall(r"ratio=\s*([\d.]+)", out)]
    check("noise ruler responds to averaging depth",
          any(r > 3 for r in ratios), f"max ratio {max(ratios) if ratios else 0:.2f}")

    print("\nprobe against a scope that refuses almost everything")
    stubborn = FakeScope(idn="SOME OTHER SCOPE,X,1,1.0",
                         unsupported=(":WAVeform", ":DISPlay:DATA", ":ACQuire",
                                      ":TRIGger:STATus"))
    try:
        out2 = run_phases(stubborn, "stubborn")
        check("survives a scope that answers almost nothing", True)
    except Exception as exc:
        check("survives a scope that answers almost nothing", False,
              f"{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    check("says so rather than crashing", "NO ANSWER" in out2)
    check("bails out of the averaging phase cleanly",
          "cannot measure noise" in out2 or "no trigger" in out2)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("Probe survives both mocks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
