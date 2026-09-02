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
                 unsupported=(), dead_trigger=False):
        self.idn = idn
        self.unsupported = unsupported
        # Reproduces the first real run: sweep NORMal, trigger pointed at a
        # channel with nothing on it, so the status sits at WAIT and every
        # read comes back empty rather than erroring.
        self.dead_trigger = dead_trigger
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

    def _dead(self):
        """A scope waiting for a trigger it will never get has no record to
        serve, and answers with an empty one rather than an error."""
        return self.dead_trigger and self.state[":TRIGger:STATus"] != "AUTO"

    def query(self, scpi):
        scpi = scpi.strip()
        if self._refuses(scpi):
            raise IOError("timeout")
        if scpi == "*IDN?":
            return self.idn + "\n"
        if scpi == ":SYSTem:ERRor?":
            return '0,"No error"\n'
        if scpi == ":WAVeform:PREamble?":
            if self._dead():
                return "0,0,0,1,0.0,0.0,0,0.0,0,0\n"
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
        if self.dead_trigger and scpi == ":RUN":
            # NORMal sweep with nothing crossing the level: waits forever.
            self.state[":TRIGger:STATus"] = (
                "AUTO" if self.state[":TRIGger:SWEep"].startswith("AUTO")
                else "WAIT")

    def query_binary_values(self, scpi, datatype="B", container=None):
        if self._refuses(scpi):
            raise IOError("timeout")
        if scpi.startswith(":DISPlay:DATA"):
            return bytearray(self.PNG)
        if self._dead():
            return np.array([], dtype=np.uint8)
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


def run_phases(scope, label, fix=False):
    """Every phase against one mock, with output captured.

    Phases 3 and 5 only run when the trigger preflight says there is something
    to measure, which is the same gate main() applies."""
    live = False
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
            live = probe_scope.phase_trigger(p, say, 1, fix)
            probe_scope.phase_screenshot(p, say)
            if live:
                probe_scope.phase_readout(p, say, 1)
                probe_scope.phase_averaging(p, say, 1, 64)
        finally:
            probe_scope.time.sleep = real_sleep
    finally:
        sys.stdout = real_stdout
    return "\n".join(say.lines), live


def main():
    print("probe against a DS1054Z-like mock")
    scope = FakeScope()
    try:
        out, live = run_phases(scope, "rigol")
        check("all phases ran", True)
        check("preflight said the scope is measurable", live)
    except Exception as exc:
        check("all phases ran", False, f"{type(exc).__name__}: {exc}")
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

    print("\nprobe against a scope that is not triggering")
    print("(the failure the first real run hit: NORMal sweep, trigger on a")
    print(" channel with nothing on it, every read comes back empty)")
    dead = FakeScope(dead_trigger=True)
    dead.state[":TRIGger:SWEep"] = "NORM"
    dead.state[":TRIGger:EDGe:SOURce"] = "CHAN2"
    out3, live3 = run_phases(dead, "dead")
    check("preflight catches it", not live3)
    check("says nothing is triggering", "nothing is triggering" in out3)
    check("names the channel mismatch", "trigger is on CHAN2" in out3)
    check("names the NORMal sweep", "Sweep is NORMal" in out3)
    check("suggests the probe-comp terminal", "probe-comp" in out3)
    check("screenshot still ran, since it needs no trigger", "800x480" in out3)
    check("did not waste time in the phases that need one",
          "5. Averaging" not in out3)

    print("\nsame scope, with --fix-trigger")
    fixed = FakeScope(dead_trigger=True)
    fixed.state[":TRIGger:SWEep"] = "NORM"
    fixed.state[":TRIGger:EDGe:SOURce"] = "CHAN2"
    out4, live4 = run_phases(fixed, "fixed", fix=True)
    check("AUTO sweep rescues the run", live4)
    check("it pointed the trigger at the probed channel",
          ":TRIGger:EDGe:SOURce CHANnel1" in fixed.written)
    check("and put the sweep in AUTO", ":TRIGger:SWEep AUTO" in fixed.written)
    check("so the averaging phase ran after all", "5. Averaging" in out4)

    print("\nprobe against a scope that refuses almost everything")
    stubborn = FakeScope(idn="SOME OTHER SCOPE,X,1,1.0",
                         unsupported=(":WAVeform", ":DISPlay:DATA", ":ACQuire",
                                      ":TRIGger:STATus"))
    try:
        out2, _ = run_phases(stubborn, "stubborn")
        check("survives a scope that answers almost nothing", True)
    except Exception as exc:
        check("survives a scope that answers almost nothing", False,
              f"{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    check("says so rather than crashing", "NO ANSWER" in out2)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("Probe survives every mock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
