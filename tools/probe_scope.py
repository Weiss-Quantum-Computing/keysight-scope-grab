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
    python tools/probe_scope.py --fix-trigger   let it arrange its own trigger
    python tools/probe_scope.py --yes           skip the confirmation

WHAT IT CHANGES: the acquisition type, the average count, and the run state,
plus the waveform-readout settings, which are not knobs anyone sets by hand.
Everything it touches is read first and put back at the end - including on
Ctrl-C and on an error. It never writes a timebase or a channel scale.

With --fix-trigger it also points the trigger at the channel being probed and
puts the sweep in AUTO, which is the one case where it writes a setting the
front panel shows as yours. Those three are read first and restored too, and
the report says so on both sides.

WHAT IT NEEDS: a repeating signal on the probed channel, and triggers actually
arriving. The averaging tests watch noise fall as hits accumulate, so an
untriggered scope tells them nothing. This is checked up front now rather than
discovered two phases in: if nothing is triggering, the probe says exactly what
the trigger is set to and what to do about it, instead of filling a report with
zeroes.

The easiest signal on a DS1054Z is its own front-panel probe-compensation
terminal - a 1 kHz square wave that always triggers.
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
        """A noise figure that ignores the signal, edges included.

        The second difference of a smooth waveform is near zero while
        sample-to-sample noise contributes all of it, so it makes a ruler for
        how deep an average really is: averaging N sweeps should drop it by
        sqrt(N), and it needs no knowledge of what is on the probe.

        The estimator has to be a robust one, though, and this used a standard
        deviation at first. On a square wave - which is what the probe-comp
        terminal produces, and what anyone will reach for when told to put a
        signal on the channel - the two edges produce enormous second
        differences and a standard deviation is dominated by the handful of
        samples in them. Averaging does not reduce an edge, so the ratio would
        have sat near 1 at every depth and the report would have concluded the
        scope does not average at all.

        So the edges have to be trimmed off rather than averaged in. A median
        was the first attempt and was worse: once the scope averages, the noise
        drops below one code, most second differences are then exactly zero,
        and the median of them is exactly zero too. It reported a perfect 0.000
        at every depth and measured nothing at all.

        A trimmed RMS keeps both properties. Dropping the largest 2% by
        magnitude removes the handful of edge samples - a square wave has two
        transitions in 1200 points, well under that - and what is left is an
        honest RMS of the noise, which still resolves a sub-code trace instead
        of collapsing to zero."""
        if v is None or len(v) < 8:
            return float("nan")
        d2 = np.diff(np.asarray(v, dtype=float), n=2)
        if not len(d2):
            return float("nan")
        keep = np.abs(d2) <= np.percentile(np.abs(d2), 98)
        if not keep.any():
            return float("nan")
        return float(np.sqrt(np.mean(d2[keep] ** 2)) / np.sqrt(6.0))

    @staticmethod
    def codes(v):
        """How many distinct sample values the transferred trace uses.

        The other half of the resolution question. The scope hands back 8-bit
        codes whatever the format, so an averaged record is re-quantised on
        transfer: if averaging is smoothing the trace below one code, this
        number collapses even though the screen looks better."""
        if v is None or not len(v):
            return 0
        return int(len(np.unique(np.asarray(v))))


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


def single_shot(p, say, wait_s=10.0):
    """Arm one acquisition and wait, leaving the sweep mode as it was found.

    MEASURED on a DS1054Z, 00.04.04.SP1: :SINGle here is not a one-shot arm the
    way it is on an MSO-X. The guide says it plainly once you look for it - it
    is "equivalent to ... sending the :TRIGger:SWEep SINGle command" - so it
    changes a setting the front panel shows and leaves it changed. An earlier
    version of this script sent it in two different phases and put it back in
    neither, which parked the scope in single-sweep mode and made every phase
    after it wait forever for a trigger that a stopped scope was never going to
    produce.

    Any profile built for this scope has to do the same save and restore around
    its single(), or a capture will quietly rewrite the trigger setup.

    Returns True if it triggered.
    """
    sweep = p.ask(":TRIGger:SWEep?")
    p.send(":SINGle")
    deadline = time.time() + wait_s
    triggered = False
    while time.time() < deadline:
        if p.ask(":TRIGger:STATus?", 2000) == "STOP":
            triggered = True
            break
        time.sleep(0.2)
    if sweep:
        p.send(f":TRIGger:SWEep {sweep}")
        p.errors()
    if not triggered:
        say(f"  (no trigger within {wait_s:g} s; sweep put back to {sweep})")
    return triggered


def phase_trigger(p, say, chan, fix):
    """Is anything actually triggering? Returns True if the rest can proceed.

    This exists because the first real run of this script produced two phases
    of zeroes and a noise ruler of nan, and the reason - the scope was set to
    trigger on a different channel than the one being read, in NORMal sweep,
    with nothing crossing the level - was sitting in the phase 2 dump the whole
    time. A probe that cannot measure should say so in three seconds, not fill
    a report with nothing and let someone read it to find out.
    """
    say.head("2.5 Is anything triggering?")

    sweep = p.ask(":TRIGger:SWEep?")
    source = p.ask(":TRIGger:EDGe:SOURce?")
    level = p.ask(":TRIGger:EDGe:LEVel?")
    say(f"  sweep={sweep}  source={source}  level={level}  "
        f"probing CHANnel{chan}")
    say("")
    say("  channels the scope is showing:")
    for ch in (1, 2, 3, 4):
        on = p.ask(f":CHANnel{ch}:DISPlay?")
        if on in ("1", "ON"):
            say(f"    CH{ch}  on   {p.ask(f':CHANnel{ch}:SCALe?')} V/div, "
                f"offset {p.ask(f':CHANnel{ch}:OFFSet?')}")
        else:
            say(f"    CH{ch}  off")

    if fix:
        say("")
        say("  --fix-trigger: pointing the trigger at the probed channel and")
        say("  putting the sweep in AUTO, so a sweep arrives whatever the")
        say("  signal does. Both are restored at the end.")
        p.send(":TRIGger:MODE EDGE")
        p.send(f":TRIGger:EDGe:SOURce CHANnel{chan}")
        p.send(":TRIGger:SWEep AUTO")
        p.errors()
        time.sleep(0.5)

    p.send(":RUN")
    say("")
    say("  watching :TRIGger:STATus? for 6 s:")
    seen, t0 = [], time.time()
    while time.time() - t0 < 6.0:
        status = p.ask(":TRIGger:STATus?", 2000)
        if status and (not seen or seen[-1] != status):
            seen.append(status)
        time.sleep(0.2)
    say(f"    {' -> '.join(seen) if seen else '(no answer)'}")

    # A record with points in it is the real test: TD and RUN come and go too
    # fast to catch reliably, but a served trace cannot be faked.
    _, v, pre = p.read_trace(chan, "NORMal")
    points = 0 if v is None else len(v)
    say(f"    a NORMal read returns {points} points"
        f"{', preamble points=' + pre['points'] if pre else ''}")

    if points > 0:
        rough = p.roughness(v)
        say(f"    noise ruler on that trace: {rough:.6g} V")
        if np.isfinite(rough) and rough > 0:
            if fix:
                # A level the signal actually crosses, or :SINGle never fires
                # and phase 3 learns nothing about a stopped record. Halfway
                # between the 5th and 95th percentiles: for a square wave that
                # is the 50% crossing, which is exactly where you want it, and
                # the percentiles rather than min/max keep one spike from
                # dragging it off. A plain median would be wrong here - on a
                # square wave it lands on whichever level has more samples.
                lo, hi = np.percentile(v, [5, 95])
                mid = float((lo + hi) / 2.0)
                p.send(f":TRIGger:EDGe:LEVel {mid:.6e}")
                p.errors()
                say(f"    trigger level set to the trace midpoint, {mid:.4g} V,"
                    f" so a single shot can actually fire")
            say("  -> triggering, and the trace has noise to measure. Good.")
            return True
        say("  !! a trace, but it is perfectly flat - nothing to measure.")
        say("     The channel is probably railed or unconnected. Put a signal")
        say("     on it (the front-panel probe-comp terminal will do).")
        return False

    say("")
    say("  !! nothing is triggering, so phases 3 and 5 would measure nothing.")
    if source and f"CHAN{chan}" not in source.upper():
        say(f"     The trigger is on {source} but this is probing CH{chan}.")
        say(f"     Either run with --channel {source.upper().replace('CHAN', '')}"
            f" or add --fix-trigger.")
    if sweep and sweep.upper().startswith("NORM"):
        say("     Sweep is NORMal, so with no trigger there is never a sweep.")
        say("     --fix-trigger puts it in AUTO for the duration.")
    say("     Or connect the probed channel to the scope's own probe-comp")
    say("     terminal, which is a 1 kHz square wave that always triggers.")
    return False


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
            single_shot(p, say)
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


def ratio_str(base, r):
    """How much the noise fell, or a word when it fell out of sight.

    A ruler of exactly zero is not a failed measurement here: it means the
    averaged trace has no sample-to-sample variation left at all once it has
    been re-quantised to 8 bits for transfer. That is a real result and worth
    saying in words, rather than printing nan and looking like the earlier runs
    that genuinely measured nothing."""
    if not np.isfinite(r):
        return "   n/a"
    if r <= 0:
        return "sub-LSB"
    return f"{base / r:6.2f}"


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
    base_codes = p.codes(v)
    say("")
    say(f"  unaveraged noise ruler: {base:.6g} V over {base_codes} distinct "
        f"codes")
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

    # Q0: does the count report the setting, or the hits so far? This is the
    # one question the noise ruler cannot reach - readback quantisation puts a
    # floor under it - and it is answerable on its own by asking for a deep
    # average and watching the number from the first moment it exists.
    say("")
    say("  Q0  Does the preamble count report the SETTING or the hits so far?")
    say("      Asking for a deep average and reading the count straight away:")
    say("      a number that jumps to the setting reports the setting; one")
    say("      that climbs is counting real hits. (MSO-X: reports the setting")
    say("      the moment RUN is involved, which made a poll declare a")
    say("      contaminated average complete immediately.)")
    deep = 1024
    p.send(":ACQuire:TYPE NORMal")
    p.send(":RUN")
    time.sleep(0.8)
    p.send(f":ACQuire:AVERages {deep}")
    p.send(":ACQuire:TYPE AVERages")
    t0 = time.time()
    counts = []
    for _ in range(14):
        _, v, pre = p.read_trace(chan, "NORMal")
        c = pre["count"] if pre else "?"
        counts.append(c)
        say(f"      t={time.time() - t0:5.1f}s  count={c:>6}  "
            f"noise={p.roughness(v):.6g}  codes={p.codes(v)}")
        time.sleep(0.7)
    uniq = [c for i, c in enumerate(counts) if i == 0 or c != counts[i - 1]]
    say(f"      count went: {' -> '.join(uniq)}")
    if len(uniq) == 1 and uniq[0] == str(deep):
        say(f"      -> pinned at the setting from the first read: it reports")
        say(f"         the SETTING, exactly as the MSO-X does. A poll on this")
        say(f"         number cannot tell you an average is finished.")
    elif len(uniq) > 1:
        say("      -> it climbed, so it is counting real hits. This is better")
        say("         than the MSO-X and a build can be polled to completion.")
    p.send(f":ACQuire:AVERages {depth}")
    p.errors()

    # Q1: under RUN, does the average build to depth and stop, or keep running?
    say("")
    say(f"  Q1  Under RUN, does noise settle at sqrt({depth}) and stay there?")
    say("      (MSO-X: exponential running average - it never settles, and it")
    say("      carries a memory of whatever played before)")
    say(f"      unaveraged: noise={base:.6g}, codes={base_codes}")
    p.send(":RUN")
    t0 = time.time()
    for _ in range(10):
        time.sleep(1.0)
        _, v, pre = p.read_trace(chan, "NORMal")
        r = p.roughness(v)
        say(f"      t={time.time() - t0:5.1f}s  noise={r:.6g}  "
            f"ratio={ratio_str(base, r)}  "
            f"codes={p.codes(v):>4}  "
            f"preamble count={pre['count'] if pre else '?'}")
    say(f"      expected ratio at a true {depth}-deep average: "
        f"{np.sqrt(depth):.2f}")
    say("      codes falling as the ratio rises = averaging is smoothing the")
    say("      trace below what an 8-bit transfer can carry.")

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
    single_shot(p, say, wait_s=15.0)
    # Stop before reading. single_shot puts the sweep back the way it found it,
    # which here means AUTO, so the scope starts free-running again the instant
    # the single completes and the record moves under the read. The first go at
    # this returned an empty trace for exactly that reason.
    p.send(":STOP")
    time.sleep(0.5)
    _, v, pre = p.read_trace(chan, "NORMal")
    single = p.roughness(v)
    say(f"      after :SINGle  noise={single:.6g}  "
        f"ratio={ratio_str(base, single)}  codes={p.codes(v)}  "
        f"count={pre['count'] if pre else '?'}")
    say(f"      ratio near 1.00 = one hit; near {np.sqrt(depth):.2f} = "
        f"a real {depth}-deep average")

    # Q5: if the scope cannot be trusted to count an average, can we just do
    # it ourselves? This is the one that decides the design, so it is measured
    # rather than assumed.
    say("")
    say("  Q5  Software averaging: N plain traces averaged here, against the")
    say("      scope's own N-deep average.")
    say("      The scope hands back 8-bit codes whatever the format, so its")
    say("      average is re-quantised on transfer and arrives with the")
    say("      resolution thrown away. Averaging raw traces in float here")
    say("      should keep it. If so, that is how the profile should do it.")
    p.send(":ACQuire:TYPE NORMal")
    p.send(":RUN")
    time.sleep(0.5)
    stack, seen_first = [], None
    t0 = time.time()
    for _ in range(depth):
        _, v, _ = p.read_trace(chan, "NORMal")
        if v is None or not len(v):
            continue
        if seen_first is None:
            seen_first = v
        stack.append(v)
    took = time.time() - t0
    if len(stack) < 4:
        say(f"      only got {len(stack)} traces - cannot compare")
    else:
        arr = np.vstack(stack)
        # Consecutive reads can land on the same sweep. Count how many are
        # actually distinct, or the depth claimed here is as dishonest as the
        # scope's own count.
        distinct = len(np.unique(arr, axis=0))
        mean = arr.mean(axis=0)
        say(f"      {len(stack)} traces in {took:.1f} s "
            f"({took / max(1, len(stack)) * 1000:.0f} ms each), "
            f"{distinct} of them distinct sweeps")
        say(f"      one raw trace     noise={p.roughness(seen_first):.6g}  "
            f"codes={p.codes(seen_first)}")
        say(f"      averaged here     noise={p.roughness(mean):.6g}  "
            f"ratio={ratio_str(base, p.roughness(mean))}  "
            f"codes={p.codes(mean)}")
        say(f"      (the scope's own {depth}-deep average above, for contrast)")
        say(f"      expected ratio for {distinct} honest hits: "
            f"{np.sqrt(max(1, distinct)):.2f}")

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
    ap.add_argument("--fix-trigger", action="store_true",
                    help="point the trigger at the probed channel and put the "
                         "sweep in AUTO, restoring both afterwards")
    ap.add_argument("--force", action="store_true",
                    help="run the measuring phases even if nothing is "
                         "triggering (they will report nothing useful)")
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
        print("timebase or the channel scales.")
        if args.fix_trigger:
            print("")
            print("--fix-trigger: it will ALSO point the trigger at channel "
                  f"{args.channel}")
            print("and put the sweep in AUTO. Both are read first and restored.")
        print("")
        print(f"It needs a repeating signal on channel {args.channel}, and")
        print("triggers arriving. It checks that up front and stops early if")
        print("not, rather than writing a report full of zeroes.")
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

    # Read back what we are about to disturb, so it can be put back. The
    # trigger settings are only in here when --fix-trigger will write them,
    # so a plain run cannot restore something it never touched.
    # :TRIGger:SWEep is always in here, --fix-trigger or not: on a Rigol
    # :SINGle *is* a sweep-mode write, so any run that arms a single shot has
    # already changed it whether it meant to or not. single_shot() puts it back
    # each time; this is the belt to that braces.
    wanted = [":ACQuire:TYPE", ":ACQuire:AVERages", ":ACQuire:MDEPth",
              ":TRIGger:SWEep"]
    if args.fix_trigger:
        wanted += [":TRIGger:MODE", ":TRIGger:EDGe:SOURce",
                   ":TRIGger:EDGe:LEVel"]
    restore = {}
    for scpi in wanted:
        restore[scpi] = p.ask(scpi + "?")
    was_running = p.ask(":TRIGger:STATus?") != "STOP"
    say(f"\n  saved for restore: {restore}, running={was_running}")

    try:
        phase_identity(p, say)
        phase_settings(p, say, args.channel)
        live = phase_trigger(p, say, args.channel, args.fix_trigger)
        if not live and not args.force:
            say("")
            say("  Skipping phases 3 and 5 - they need triggers and there are")
            say("  none. Phase 4 still runs; it only needs a screen. Re-run")
            say("  with --fix-trigger, or --force to see them fail in detail.")
            phase_screenshot(p, say)
        else:
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
