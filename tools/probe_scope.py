#!/usr/bin/env python3
"""
Interrogate a scope and write down what it actually does.

A programming guide says what commands exist. It does not say what the averager
does under RUN, whether anything resets it, whether the hit count reports the
setting or the accumulated depth, or which readout modes will serve a record
stopped one way rather than another. Every one of those was wrong in this
program until it was measured on the MSO-X, and every one of them has to be
measured again on any new instrument before a profile can claim to know it.

So: run this with the scope on the bench and a signal on it, send the report
back, and the profile gets written from measurements instead of from a PDF.

    python tools/probe_scope.py                 find it, probe it, restore it
    python tools/probe_scope.py --addr <VISA>   skip the scan
    python tools/probe_scope.py --channel 2     probe a channel other than 1
    python tools/probe_scope.py --yes           skip the confirmation

WHAT IT CHANGES: the acquisition type, the average count, and the run state,
plus the waveform-readout settings, which are not knobs anyone sets by hand.
Everything it touches is read first and put back at the end - including on
Ctrl-C and on an error. It never writes a timebase, a channel scale, a trigger
setting, or anything the front panel shows as your setup.

WHAT IT NEEDS: a repeating, triggering signal on the probed channel. The
averaging tests are built on watching noise fall as hits accumulate, so a
static or untriggered trace tells it nothing and it will say so rather than
guess.
"""
import argparse
import datetime
import os
import sys
import time

import numpy as np
import pyvisa

REPORT_DIR = os.path.join(os.path.expanduser("~"), "Desktop")


class Report:
    """Everything printed also goes in the file, so the file is the whole
    session rather than a summary someone has to trust."""

    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        print(text, flush=True)
        self.lines.append(text)

    def head(self, title):
        self("")
        self("=" * 72)
        self(title)
        self("=" * 72)

    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.lines) + "\n")


class Probe:
    def __init__(self, inst, say):
        self.inst = inst
        self.say = say

    # -- primitives -------------------------------------------------------

    def ask(self, scpi, timeout_ms=3000):
        """Query, returning None if the scope declines to answer.

        A refused query is not a reply that says so - the scope pushes an error
        and sends nothing, so the read waits out the whole timeout. Hence the
        short one, a device clear to drop a late arrival, and a drain of the
        error queue afterwards."""
        saved = self.inst.timeout
        self.inst.timeout = timeout_ms
        try:
            return self.inst.query(scpi).strip()
        except Exception:
            try:
                self.inst.clear()
            except Exception:
                pass
            self.errors()
            return None
        finally:
            self.inst.timeout = saved

    def send(self, scpi):
        self.inst.write(scpi)

    def errors(self):
        found = []
        for _ in range(10):
            try:
                resp = self.inst.query(":SYSTem:ERRor?").strip()
            except Exception:
                break
            if resp.startswith("0,") or resp.startswith("+0,"):
                break
            found.append(resp)
        return found

    def supported(self, queries):
        """Which of these the scope will answer, and with what."""
        rows = []
        for scpi in queries:
            reply = self.ask(scpi)
            rows.append((scpi, reply))
        return rows

    def show(self, rows):
        width = max((len(s) for s, _ in rows), default=0)
        for scpi, reply in rows:
            if reply is None:
                self.say(f"  {scpi:<{width}}  -- NO ANSWER (unsupported?)")
            else:
                self.say(f"  {scpi:<{width}}  {reply}")

    # -- readout ----------------------------------------------------------

    def preamble(self):
        raw = self.ask(":WAVeform:PREamble?")
        if raw is None:
            return None
        parts = raw.split(",")
        if len(parts) < 10:
            self.say(f"  preamble has {len(parts)} fields, expected 10: {raw}")
            return None
        keys = ("format", "type", "points", "count", "xincrement", "xorigin",
                "xreference", "yincrement", "yorigin", "yreference")
        return dict(zip(keys, [p.strip() for p in parts]))

    def read_trace(self, ch, mode, fmt="BYTE"):
        """(t, v, preamble) for one channel, or (None, None, preamble) if the
        scope would not serve the record in this mode."""
        self.send(f":WAVeform:SOURce CHANnel{ch}")
        self.send(f":WAVeform:MODE {mode}")
        self.send(f":WAVeform:FORMat {fmt}")
        pre = self.preamble()
        if pre is None:
            return None, None, None
        saved = self.inst.timeout
        self.inst.timeout = 20000
        try:
            raw = self.inst.query_binary_values(
                ":WAVeform:DATA?", datatype="B" if fmt == "BYTE" else "H",
                container=np.array)
        except Exception as exc:
            try:
                self.inst.clear()
            except Exception:
                pass
            self.errors()
            self.say(f"  {mode}/{fmt}: no data ({type(exc).__name__})")
            return None, None, pre
        finally:
            self.inst.timeout = saved
        xinc, xorig = float(pre["xincrement"]), float(pre["xorigin"])
        xref = float(pre["xreference"])
        yinc, yorig = float(pre["yincrement"]), float(pre["yorigin"])
        yref = float(pre["yreference"])
        t = (np.arange(len(raw)) - xref) * xinc + xorig
        v = (raw.astype(np.float64) - yref - yorig) * yinc
        return t, v, pre

    @staticmethod
    def roughness(v):
        """A noise figure that does not care what the signal is.

        The standard deviation of the second difference: a smooth waveform of
        any shape contributes almost nothing, while sample-to-sample noise
        contributes all of it. Averaging N sweeps should drop this by sqrt(N),
        which is what makes it a usable ruler for how deep an average really
        is - and it needs no knowledge of the signal on the probe."""
        if v is None or len(v) < 8:
            return float("nan")
        return float(np.std(np.diff(v, n=2)) / np.sqrt(6.0))


def scan(rm, say, want=("USB", "TCPIP")):
    """Every instrument that answers, with its IDN - not just the one we want.
    Knowing what else is on the bus is half of diagnosing a connect failure."""
    found = []
    for res in rm.list_resources():
        if not res.startswith(tuple(want)):
            continue
        dev = None
        try:
            dev = rm.open_resource(res)
            dev.timeout = 3000
            dev.read_termination = "\n"
            dev.write_termination = "\n"
            idn = dev.query("*IDN?").strip()
            found.append((res, idn))
        except Exception as exc:
            found.append((res, f"<no answer: {type(exc).__name__}>"))
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
    return found


# ---------------------------------------------------------------------------
# the phases
# ---------------------------------------------------------------------------

def phase_identity(p, say):
    say.head("1. Identity")
    say("  *IDN?  " + str(p.ask("*IDN?")))
    for scpi in (":SYSTem:OPTion:STATe?", "*OPT?"):
        reply = p.ask(scpi, timeout_ms=2000)
        if reply is not None:
            say(f"  {scpi}  {reply}")
    left = p.errors()
    say(f"  error queue at start: {left if left else 'empty'}")


def phase_settings(p, say, chan):
    say.head("2. Settings the panel wants - which answer, and with what")
    say("Anything marked NO ANSWER cannot go in the profile as written.")
    say("")
    say(" timebase / acquisition")
    p.show(p.supported([
        ":TIMebase:MAIN:SCALe?", ":TIMebase:SCALe?", ":TIMebase:MAIN:OFFSet?",
        ":TIMebase:OFFSet?", ":TIMebase:MODE?", ":TIMebase:REFerence?",
        ":ACQuire:TYPE?", ":ACQuire:AVERages?", ":ACQuire:COUNt?",
        ":ACQuire:SRATe?", ":ACQuire:MDEPth?", ":ACQuire:POINts?",
    ]))
    say("")
    say(" trigger")
    p.show(p.supported([
        ":TRIGger:MODE?", ":TRIGger:SWEep?", ":TRIGger:COUPling?",
        ":TRIGger:HOLDoff?", ":TRIGger:NREJect?", ":TRIGger:STATus?",
        ":TRIGger:POSition?", ":TRIGger:EDGe:SOURce?", ":TRIGger:EDGe:SLOPe?",
        ":TRIGger:EDGe:LEVel?",
        # Keysight spellings, to see whether either scope accepts both
        ":TRIGger:EDGE:SOURce?", ":TRIGger:EDGE:REJect?",
    ]))
    say("")
    say(f" channel {chan}")
    p.show(p.supported([
        f":CHANnel{chan}:SCALe?", f":CHANnel{chan}:OFFSet?",
        f":CHANnel{chan}:COUPling?", f":CHANnel{chan}:PROBe?",
        f":CHANnel{chan}:UNITs?", f":CHANnel{chan}:BWLimit?",
        f":CHANnel{chan}:INVert?", f":CHANnel{chan}:DISPlay?",
        f":CHANnel{chan}:VERNier?",
    ]))
    say("")
    say(" memory depth: which values this scope will take with the current")
    say(" channel count (the guide makes the list depend on channels on)")
    start = p.ask(":ACQuire:MDEPth?")
    for value in ("AUTO", "3000", "6000", "12000", "30000", "120000"):
        p.send(f":ACQuire:MDEPth {value}")
        errs = p.errors()
        got = p.ask(":ACQuire:MDEPth?")
        say(f"  set {value:<8} -> reads {str(got):<10} "
            f"{'rejected: ' + errs[0] if errs else ''}")
    if start is not None:
        p.send(f":ACQuire:MDEPth {start}")
        p.errors()


def phase_readout(p, say, chan):
    say.head("3. Waveform readout - which modes serve a record, and how big")
    say("Run state first, then after a :SINGle, because on the MSO-X those")
    say("two records answer in different modes and that cost a release.")

    for state, setup in (("running", lambda: p.send(":RUN")),
                         ("after :SINGle", None)):
        if setup is not None:
            setup()
            time.sleep(1.0)
        else:
            p.send(":SINGle")
            deadline = time.time() + 10
            while time.time() < deadline:
                if p.ask(":TRIGger:STATus?", 2000) == "STOP":
                    break
                time.sleep(0.2)
            else:
                say("  (no trigger within 10 s - is a signal connected?)")
        say("")
        say(f" {state}: :TRIGger:STATus? = {p.ask(':TRIGger:STATus?')}")
        for mode in ("NORMal", "MAXimum", "RAW"):
            for fmt in ("BYTE", "WORD"):
                t, v, pre = p.read_trace(chan, mode, fmt)
                if pre is None:
                    say(f"  {mode:<8} {fmt:<4}  preamble refused")
                    continue
                n = 0 if v is None else len(v)
                say(f"  {mode:<8} {fmt:<4}  {n:>8} pts   "
                    f"preamble points={pre['points']} count={pre['count']} "
                    f"yinc={pre['yincrement']}")

    say("")
    say(" Does WORD carry more resolution than BYTE, or is it 8 bits padded?")
    say(" The guide says the high byte is always 0; this is that claim tested.")
    t, v8, _ = p.read_trace(chan, "NORMal", "BYTE")
    t, v16, _ = p.read_trace(chan, "NORMal", "WORD")
    for label, v in (("BYTE", v8), ("WORD", v16)):
        if v is None:
            continue
        levels = len(np.unique(v))
        step = np.min(np.diff(np.unique(v))) if levels > 1 else float("nan")
        say(f"  {label}: {levels} distinct levels, smallest step {step:.6g} V")
    say("  (same step in both = WORD buys nothing and BYTE halves the transfer)")


def phase_screenshot(p, say):
    say.head("4. Screenshot")
    for cmd in (":DISPlay:DATA? ON,0,PNG", ":DISPlay:DATA? ON,OFF,PNG",
                ":DISPlay:DATA?"):
        saved = p.inst.timeout
        p.inst.timeout = 20000
        try:
            data = p.inst.query_binary_values(cmd, datatype="B",
                                              container=bytearray)
        except Exception as exc:
            try:
                p.inst.clear()
            except Exception:
                pass
            p.errors()
            say(f"  {cmd:<28}  failed ({type(exc).__name__})")
            continue
        finally:
            p.inst.timeout = saved
        kind = ("PNG" if bytes(data[:4]) == b"\x89PNG" else
                "BMP" if bytes(data[:2]) == b"BM" else
                f"unknown, starts {bytes(data[:4])!r}")
        size = ""
        if kind == "PNG" and len(data) > 24:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            size = f", {w}x{h}"
        say(f"  {cmd:<28}  {len(data)} bytes, {kind}{size}")
        if kind == "PNG":
            say("  ^ this is the one for the profile")
            return
    say("  no working PNG form found - the profile will need BMP and a convert")


def phase_averaging(p, say, chan, depth):
    """The one the guide cannot answer, and the one that matters most."""
    say.head(f"5. Averaging - the {depth}-deep behaviour, measured")
    say("Noise here is the std of the second difference of the trace, which")
    say("ignores signal shape and falls as sqrt(N) with averaging depth. That")
    say("is the ruler; the questions are what the scope does to it.")

    p.send(":ACQuire:TYPE NORMal")
    p.send(":RUN")
    time.sleep(1.5)
    _, v, _ = p.read_trace(chan, "NORMal")
    base = p.roughness(v)
    say("")
    say(f"  unaveraged noise ruler: {base:.6g} V")
    if not np.isfinite(base) or base <= 0:
        say("  !! cannot measure noise - is there a live, triggering signal?")
        say("     Every test below needs one. Stopping this phase.")
        return

    p.send(f":ACQuire:AVERages {depth}")
    errs = p.errors()
    got = p.ask(":ACQuire:AVERages?")
    say(f"  :ACQuire:AVERages {depth} -> reads {got}"
        f"{'  rejected: ' + errs[0] if errs else ''}")
    p.send(":ACQuire:TYPE AVERages")
    say(f"  :ACQuire:TYPE? -> {p.ask(':ACQuire:TYPE?')}")

    # Q1: under RUN, does the average build to depth and stop, or keep running?
    say("")
    say("  Q1  Under RUN, does noise settle at sqrt(N) and the count stop?")
    say("      (MSO-X: exponential running average, count reports the SETTING)")
    p.send(":RUN")
    t0 = time.time()
    for _ in range(10):
        time.sleep(1.0)
        _, v, pre = p.read_trace(chan, "NORMal")
        r = p.roughness(v)
        say(f"      t={time.time() - t0:5.1f}s  noise={r:.6g}  "
            f"ratio={base / r if r else float('nan'):6.2f}  "
            f"preamble count={pre['count'] if pre else '?'}")
    say(f"      expected ratio at a true {depth}-deep average: "
        f"{np.sqrt(depth):.2f}")

    # Q2: does anything reset it?
    say("")
    say("  Q2  Does :CLEar restart the average, or does it survive?")
    say("      (MSO-X: nothing reset it - not :CDISplay, not a stop/run cycle)")
    _, v, _ = p.read_trace(chan, "NORMal")
    before = p.roughness(v)
    p.send(":CLEar")
    time.sleep(0.3)
    _, v, pre = p.read_trace(chan, "NORMal")
    after = p.roughness(v)
    say(f"      before :CLEar  noise={before:.6g}")
    say(f"      after  :CLEar  noise={after:.6g}  count={pre['count'] if pre else '?'}")
    say(f"      -> {'restarted' if after > before * 2 else 'survived (no reset)'}")

    say("")
    say("      and a stop/run cycle?")
    p.send(":STOP")
    time.sleep(0.3)
    p.send(":RUN")
    time.sleep(0.3)
    _, v, _ = p.read_trace(chan, "NORMal")
    cycled = p.roughness(v)
    say(f"      after stop/run noise={cycled:.6g}  "
        f"-> {'restarted' if cycled > before * 2 else 'survived (no reset)'}")

    # Q3: what does :SINGle give in averaging mode?
    say("")
    say("  Q3  In averaging mode, does :SINGle build the full average or take")
    say("      one hit? (MSO-X: one hit, while claiming the full depth)")
    p.send(":STOP")
    time.sleep(0.3)
    p.send(":CLEar")
    p.send(":SINGle")
    deadline = time.time() + 15
    while time.time() < deadline:
        if p.ask(":TRIGger:STATus?", 2000) == "STOP":
            break
        time.sleep(0.2)
    time.sleep(0.5)
    _, v, pre = p.read_trace(chan, "NORMal")
    single = p.roughness(v)
    say(f"      after :SINGle  noise={single:.6g}  "
        f"ratio={base / single if single else float('nan'):.2f}  "
        f"count={pre['count'] if pre else '?'}")
    say(f"      ratio near 1.00 = one hit; near {np.sqrt(depth):.2f} = "
        f"a real {depth}-deep average")

    # Q4: is there any honest counted build?
    say("")
    say("  Q4  Does :DIGitize exist here? (the MSO-X answer to all of the above)")
    p.send(":DIGitize")
    errs = p.errors()
    say(f"      :DIGitize -> {errs[0] if errs else 'accepted, no error'}")
    status = p.ask(":TRIGger:STATus?", 2000)
    say(f"      :TRIGger:STATus? after it: {status}")
    p.send(":STOP")


def main():
    ap = argparse.ArgumentParser(
        description="Measure what a scope actually does, for writing a profile.")
    ap.add_argument("--addr", help="VISA resource string; skips the scan")
    ap.add_argument("--channel", type=int, default=1,
                    help="channel to probe (default 1)")
    ap.add_argument("--depth", type=int, default=64,
                    help="averaging depth to test (default 64)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt")
    args = ap.parse_args()

    say = Report()
    say("scope probe - " + datetime.datetime.now().isoformat(timespec="seconds"))

    rm = pyvisa.ResourceManager()
    if args.addr:
        addr = args.addr
    else:
        say.head("0. What is on the bus")
        found = scan(rm, say)
        for res, idn in found:
            say(f"  {res}\n      {idn}")
        if not found:
            say("  nothing answered. Check the cable and that the scope's")
            say("  USB Device setting is 'Computer' rather than 'PictBridge'.")
            return 1
        rigol = [r for r, i in found if "RIGOL" in i.upper()]
        addr = rigol[0] if rigol else found[0][0]
        say(f"\n  probing {addr}")

    if not args.yes:
        print("\n" + "-" * 72)
        print("This changes the acquisition type, the average count and the run")
        print("state, and puts them all back at the end. It does not touch the")
        print("timebase, the channel scales or the trigger setup.")
        print("It needs a live, repeating, triggering signal on channel "
              f"{args.channel}.")
        print("-" * 72)
        if input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing done")
            return 0

    inst = rm.open_resource(addr)
    inst.timeout = 10000
    inst.read_termination = "\n"
    inst.write_termination = "\n"
    inst.chunk_size = 1024 * 1024
    p = Probe(inst, say)

    # Read back what we are about to disturb, so it can be put back.
    restore = {}
    for scpi in (":ACQuire:TYPE", ":ACQuire:AVERages", ":ACQuire:MDEPth"):
        restore[scpi] = p.ask(scpi + "?")
    was_running = p.ask(":TRIGger:STATus?") != "STOP"
    say(f"\n  saved for restore: {restore}, running={was_running}")

    try:
        phase_identity(p, say)
        phase_settings(p, say, args.channel)
        phase_readout(p, say, args.channel)
        phase_screenshot(p, say)
        phase_averaging(p, say, args.channel, args.depth)
    except KeyboardInterrupt:
        say("\n!! interrupted - restoring settings")
    except Exception as exc:
        say(f"\n!! {type(exc).__name__}: {exc}")
    finally:
        say.head("Restoring")
        for scpi, value in restore.items():
            if value:
                p.send(f"{scpi} {value}")
                say(f"  {scpi} <- {value}")
        p.send(":RUN" if was_running else ":STOP")
        say(f"  {':RUN' if was_running else ':STOP'}")
        left = p.errors()
        say(f"  error queue at end: {left if left else 'empty'}")
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(REPORT_DIR, f"scope_probe_{stamp}.txt")
        try:
            say.save(path)
            print(f"\nreport written to {path}")
            print("Send that file back and the profile gets written from it.")
        except Exception as exc:
            print(f"could not write the report: {exc}")
        try:
            inst.close()
            rm.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
