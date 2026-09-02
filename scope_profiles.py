#!/usr/bin/env python3
"""
Instrument profiles for Scope Grab.

One profile per scope family. A profile carries everything the panel and the
capture path need to know about a particular instrument:

  * the settings tables the GUI lays itself out from,
  * the SCPI roots the capture logic has to ask for by name rather than find in
    a table (the acquisition type, the average count, the channel display flag),
  * the layout of the metadata file,
  * and the handful of operations that differ because the instruments *do* them
    differently rather than merely spell them differently.

Adding a scope means adding a subclass here and registering it in PROFILES.
Nothing in scope_grab.py should have to know which one it is talking to.

Everything a profile records about an instrument was established against that
instrument, so the notes stay with the code they describe: a behaviour measured
on an MSO-X is not a fact about oscilloscopes. That is also why ScopeProfile
below holds almost no defaults. With one profile written, anything put in the
base class would be a guess about what a second scope shares; what actually
turns out to be common can move up when there is a second one to compare
against.
"""

import time

import numpy as np


class ScopeProfile:
    """The contract a profile has to satisfy. See KeysightInfiniiVision for a
    worked example of every field."""

    # -- identity ---------------------------------------------------------
    key = ""              # short name, stored in the config and in setup files
    name = ""             # what the title bar and the log call it
    idn_keys = ()         # substrings that identify the maker in *IDN?
    resource_hints = ()   # VISA resource prefixes worth scanning ("USB", "TCPIP")
    visa_dll = None       # a VISA implementation to prefer, or None for default
    channels = ()         # analog channel numbers, in panel order
    preview_size = (0, 0)  # screenshot box, sized to the PNG the scope sends

    # -- settings tables --------------------------------------------------
    # (label, SCPI root, kind, choices); the root is queried as "<root>?" and
    # written as "<root> <value>", and doubles as the dict key.
    #   num    - free-form number
    #   choice - fixed list, scope answers with the same mnemonics
    #   bool   - fixed list, but the scope answers 1/0
    timebase = ()
    trigger = ()
    channel = ()          # per-channel, with {ch} in the root
    info = ()             # (label, SCPI root) read-only results
    actions = ()          # (button, SCPI, log note, confirm text or None,
    #                        whether it rewrites the panel)
    write_first = ()      # roots that must land before the rest of an Apply
    depends_on = {}       # {field: (deciding field, mnemonics that make it live)}

    # -- roots the app asks for by name -----------------------------------
    acq_type = ""         # acquisition type
    acq_count = ""        # averaging depth setting
    avg_prefix = ""       # what acq_type starts with when averaging
    avg_name = ""         # the mnemonic, spelled out for messages
    wave_count = ""       # hits in the record being read out
    ch_display = ""       # per-channel display flag, with {ch}

    # -- one-shot commands ------------------------------------------------
    cmd_run = ""
    cmd_stop = ""
    cmd_single = ""

    # -- metadata file layout ---------------------------------------------
    # (label, SCPI root) in file order. meta_head runs from the top of the file
    # down to the acquisition type; the averaging lines are written between the
    # two; meta_tail carries the rest. meta_channel is per-channel, with {ch}.
    meta_head = ()
    meta_tail = ()
    meta_channel = ()

    # -- behaviour --------------------------------------------------------

    def open(self, dev):
        """Configure a freshly opened VISA session that answered our *IDN?."""
        raise NotImplementedError

    def running(self, scope):
        """True while an acquisition is in progress. Raises if the scope will
        not answer - a poll that failed is not a trigger that arrived, and the
        callers depend on being able to tell those apart."""
        raise NotImplementedError

    def accumulate(self, scope, count, wait_s, cancelled, progress, source):
        """Build a `count`-deep average and report how deep it actually got.
        Scope.accumulate documents the contract this has to meet."""
        raise NotImplementedError

    def read_waveform(self, scope, channel, points_mode, points):
        """Return (t, v) for one channel."""
        raise NotImplementedError

    def screenshot(self, scope):
        """Return the scope screen as PNG bytes."""
        raise NotImplementedError

    def transfer_plan(self, averaged, points):
        """(points mode, point count) for reading out a record. Which modes will
        serve a record depends on how it was stopped, which is per-instrument."""
        raise NotImplementedError

    # -- shared -----------------------------------------------------------

    def matches(self, idn):
        up = idn.upper()
        return any(k in up for k in self.idn_keys)

    def setting_groups(self):
        """(group title, [(label, scpi root)]) in panel order. What a saved
        setup .txt companion is laid out from."""
        groups = [("Timebase / acquisition",
                   [(lbl, scpi) for lbl, scpi, _, _ in self.timebase]),
                  ("Trigger",
                   [(lbl, scpi) for lbl, scpi, _, _ in self.trigger])]
        for ch in self.channels:
            groups.append((f"CH{ch}", [(lbl, tmpl.format(ch=ch))
                                       for lbl, tmpl, _, _ in self.channel]))
        return groups


class KeysightInfiniiVision(ScopeProfile):
    """Keysight InfiniiVision 2000/3000 X-series. Everything here was
    established against an MSO-X 2014A on firmware 2.65."""

    key = "msox2014a"
    name = "MSO-X 2014A"
    idn_keys = ("KEYSIGHT", "AGILENT")
    resource_hints = ("USB",)
    # Prefer Keysight VISA explicitly so a primary/secondary VISA mixup with
    # NI-VISA cannot break us.
    visa_dll = r"C:\Windows\System32\ktvisa32.dll"
    channels = (1, 2, 3, 4)
    # The scope sends 800x503 PNGs, which fit this at ~72%.
    preview_size = (576, 362)

    timebase = [
        ("Timebase s/div", ":TIMebase:SCALe", "num", None),
        ("Position (s)", ":TIMebase:POSition", "num", None),
        ("Reference", ":TIMebase:REFerence", "choice", ("LEFT", "CENT", "RIGH")),
        ("Sweep mode", ":TIMebase:MODE", "choice", ("MAIN", "WIND", "XY", "ROLL")),
        ("Acquisition", ":ACQuire:TYPE", "choice", ("NORM", "AVER", "HRES", "PEAK")),
        ("Averages", ":ACQuire:COUNt", "num", None),
    ]
    trigger = [
        ("Type", ":TRIGger:MODE", "choice",
         ("EDGE", "GLIT", "PATT", "TV", "EBUR", "OR", "RUNT", "SHOL", "TRAN", "DEL")),
        ("Sweep", ":TRIGger:SWEep", "choice", ("AUTO", "NORM")),
        ("Source", ":TRIGger:EDGE:SOURce", "choice",
         ("CHAN1", "CHAN2", "CHAN3", "CHAN4", "EXT", "LINE", "WGEN")),
        ("Level (V)", ":TRIGger:EDGE:LEVel", "num", None),
        ("Slope", ":TRIGger:EDGE:SLOPe", "choice", ("POS", "NEG", "EITH", "ALT")),
        ("Reject", ":TRIGger:EDGE:REJect", "choice", ("OFF", "LFR", "HFR")),
        ("Noise reject", ":TRIGger:NREJect", "bool", ("ON", "OFF")),
        ("Holdoff (s)", ":TRIGger:HOLDoff", "num", None),
    ]
    channel = [
        ("V/div", ":CHANnel{ch}:SCALe", "num", None),
        ("Offset", ":CHANnel{ch}:OFFSet", "num", None),
        ("Coupling", ":CHANnel{ch}:COUPling", "choice", ("AC", "DC")),
        ("Probe", ":CHANnel{ch}:PROBe", "num", None),
        ("Units", ":CHANnel{ch}:UNITs", "choice", ("VOLT", "AMP")),
        ("BW lim", ":CHANnel{ch}:BWLimit", "bool", ("ON", "OFF")),
        ("Invert", ":CHANnel{ch}:INVert", "bool", ("ON", "OFF")),
        ("Display", ":CHANnel{ch}:DISPlay", "bool", ("ON", "OFF")),
    ]
    # Read-only values, refreshed on the same pass as the settings above. They
    # are per-acquisition results rather than knobs, so the panel shows them but
    # never writes them.
    info = [
        ("Sample rate (Sa/s)", ":ACQuire:SRATe"),
        ("Points acquired", ":ACQuire:POINts"),
    ]
    # One-shot commands: (button, SCPI, what to log, confirmation text or None,
    # whether it rewrites the panel). They carry no value and there is nothing
    # to read back, so they are not part of the settings snapshot - the panel is
    # re-read afterwards instead. The last field marks the ones that rewrite the
    # settings, and so are allowed to overwrite an unapplied edit in the panel.
    actions = [
        ("Run", ":RUN", "running continuously", None, False),
        ("Stop", ":STOP", "stopped", None, False),
        ("Single", ":SINGle", "armed for one trigger", None, False),
        ("Force trig", ":TRIGger:FORCe", "trigger forced", None, False),
        ("Clear", ":CDISplay",
         "display cleared - averaging and persistence start over", None, False),
        ("Autoscale", ":AUToscale", "autoscaled",
         "Autoscale rewrites the timebase and every channel's V/div and offset "
         "from whatever signal it finds, discarding the current setup.\n\nGo ahead?",
         True),
    ]
    # Writes that have to land before others in the same Apply. The average count
    # is ignored unless the acquisition type is already AVERage, and the edge
    # fields belong to a trigger type that has to be selected first. Everything
    # else is written afterwards, in panel order.
    write_first = (":ACQuire:TYPE", ":TRIGger:MODE", ":TIMebase:MODE")
    # Fields the instrument only acts on in a particular mode. The panel greys
    # the others out rather than letting a value the scope is ignoring look live.
    # {field: (field that decides it, mnemonics that make it live)}
    depends_on = {
        ":ACQuire:COUNt": (":ACQuire:TYPE", ("AVER",)),
        ":TRIGger:EDGE:SOURce": (":TRIGger:MODE", ("EDGE",)),
        ":TRIGger:EDGE:LEVel": (":TRIGger:MODE", ("EDGE",)),
        ":TRIGger:EDGE:SLOPe": (":TRIGger:MODE", ("EDGE",)),
        ":TRIGger:EDGE:REJect": (":TRIGger:MODE", ("EDGE",)),
    }

    acq_type = ":ACQuire:TYPE"
    acq_count = ":ACQuire:COUNt"
    avg_prefix = "AVER"
    avg_name = "AVERage"
    ch_display = ":CHANnel{ch}:DISPlay"
    # How many hits are in the trace being read out. In AVERage mode that is the
    # averaging depth the capture actually got, which is not the same thing as
    # the count that was asked for - and nothing on the scope screen
    # distinguishes the two. It has no field of its own; it is folded into the
    # grab snapshot for the metadata file.
    #
    # Not part of a normal settings read: it describes a record rather than a
    # setting, and with acquisition memory empty - straight after an acquisition
    # type change, for one - the scope raises +109,"No Data For Operation"
    # instead of answering, leaving the query unterminated and the read waiting
    # out the VISA timeout. It is only ever asked where a record is known to
    # exist.
    wave_count = ":WAVeform:COUNt"

    cmd_run = ":RUN"
    cmd_stop = ":STOP"
    cmd_single = ":SINGle"

    meta_head = [
        ("sample rate (Sa/s)", ":ACQuire:SRATe"),
        ("points acquired", ":ACQuire:POINts"),
        ("acquisition type", ":ACQuire:TYPE"),
    ]
    meta_tail = [
        ("timebase s/div", ":TIMebase:SCALe"),
        ("timebase position", ":TIMebase:POSition"),
        ("timebase reference", ":TIMebase:REFerence"),
        ("timebase mode", ":TIMebase:MODE"),
        ("trigger type", ":TRIGger:MODE"),
        ("trigger sweep", ":TRIGger:SWEep"),
        ("trigger source", ":TRIGger:EDGE:SOURce"),
        ("trigger level", ":TRIGger:EDGE:LEVel"),
        ("trigger slope", ":TRIGger:EDGE:SLOPe"),
        ("trigger reject", ":TRIGger:EDGE:REJect"),
        ("trigger noise rej", ":TRIGger:NREJect"),
        ("trigger holdoff", ":TRIGger:HOLDoff"),
    ]
    meta_channel = [
        ("V/div", ":CHANnel{ch}:SCALe"),
        ("offset", ":CHANnel{ch}:OFFSet"),
        ("coupling", ":CHANnel{ch}:COUPling"),
        ("probe atten", ":CHANnel{ch}:PROBe"),
        ("units", ":CHANnel{ch}:UNITs"),
        ("bandwidth lim", ":CHANnel{ch}:BWLimit"),
        ("invert", ":CHANnel{ch}:INVert"),
    ]

    def open(self, dev):
        dev.timeout = 30000
        dev.chunk_size = 1024 * 1024

    def running(self, scope):
        # Bit 3 of the Operation Status Condition register is the Run bit.
        return bool(int(scope.inst.query(":OPERegister:CONDition?")) & 8)

    def accumulate(self, scope, count, wait_s, cancelled, progress, source):
        """Acquire a true `count`-deep average, on hardware where nothing else is.

        Established against the MSO-X 2014A (firmware 2.65) on 2026-08-24:

        * :SINGle takes exactly one acquisition (an averaged single-shot grab
          claims the full depth while carrying one hit).
        * Under plain RUN the averager is a RUNNING average - each sweep folds
          in with weight 1/N, so the record carries an exponential memory with
          time constant N trigger periods of whatever played before, and a
          full-scale change takes ~8 of those time constants to fade from the
          trace. Nothing resets it: not :CDISplay, not rewriting the count,
          not a stop/run cycle. Worse, :WAVeform:COUNt reports the SETTING
          rather than the accumulated depth the moment RUN is involved, so a
          poll declares a contaminated average complete immediately.
        * :DIGitize is the one honest acquisition: it starts a fresh block,
          counts out exactly `count` triggers, stops itself, and afterwards
          the count reads true. Its record - like any record not stopped by
          :SINGle - answers only in the NORMal/MAXimum points modes; asking in
          RAW gets +109 "No Data For Operation" and nothing else.

        So: :DIGitize, with nothing in the waveform subsystem queried while it
        builds (those queries fail, and the device-clear recovery inside
        try_get can abort the acquisition being asked about). Completion is
        watched on the run bit and trigger liveness on :TER?, both answerable
        mid-acquisition. The hit count is read once at the end, in a mode the
        scope will serve.
        """
        inst = scope.inst
        inst.query("*OPC?")          # settings writes land before arming
        inst.query(":TER?")          # clear the event register of history
        inst.write(":DIGitize")
        started = time.time()
        alive = started
        bad_polls = 0
        completed = False     # the run bit cleared on its own = the full count
        while True:
            time.sleep(0.4)
            if cancelled is not None and cancelled():
                inst.write(":STOP")
                return None
            try:
                running = self.running(scope)
                if running and int(inst.query(":TER?")):
                    alive = time.time()
                bad_polls = 0
            except Exception:
                bad_polls += 1
                if bad_polls < 3:
                    continue
                inst.write(":STOP")
                running = False
            if not running:
                # Only a real reply says the digitize counted itself out. The
                # give-up path above puts the same False there without asking.
                completed = bad_polls == 0
                break
            if progress is not None:
                progress(time.time() - started)
            if wait_s > 0 and time.time() - alive > wait_s:
                inst.write(":STOP")
                break
        # The count is honest after a digitize, but only in a servable mode.
        inst.write(f":WAVeform:SOURce CHANnel{source}")
        inst.write(":WAVeform:POINts:MODE NORMal")
        got = scope.try_get(self.wave_count, timeout_ms=2000)
        try:
            return min(int(float(got)), count)
        except (TypeError, ValueError):
            # No answer is not the same as no hits. The record is there either
            # way - the transfer that follows reads it fine - so a digitize that
            # stopped itself gets its full count, and one that was stopped early
            # says the depth is unknown rather than being thrown away as a run
            # that never triggered.
            return count if completed else -1

    def read_waveform(self, scope, channel, points_mode, points):
        w = scope.inst
        w.write(f":WAVeform:SOURce CHANnel{channel}")
        w.write(f":WAVeform:POINts:MODE {points_mode}")
        # Setting the mode resets the point count, so ask for it afterwards. The
        # scope rounds to a value it likes; the preamble read below reports what
        # it actually gave, so the time axis stays right either way.
        if points:
            w.write(f":WAVeform:POINts {points}")
        # WORD, not BYTE. An averaged or high-res record holds finer values
        # than the 8-bit codes on screen - measured on this scope, a 256-deep
        # average reads back in 157 uV steps against the 40 mV display code, a
        # full 16x of real resolution that BYTE readback silently rounds off.
        # For NORM and PEAK the extra byte carries nothing and costs only
        # transfer time, so one format serves every mode.
        w.write(":WAVeform:FORMat WORD")
        w.write(":WAVeform:BYTeorder LSBFirst")
        w.write(":WAVeform:UNSigned ON")

        pre = w.query(":WAVeform:PREamble?").strip().split(",")
        xinc, xorig, xref = float(pre[4]), float(pre[5]), float(pre[6])
        yinc, yorig, yref = float(pre[7]), float(pre[8]), float(pre[9])

        raw = w.query_binary_values(":WAVeform:DATA?", datatype="H",
                                    container=np.array)
        t = (np.arange(len(raw)) - xref) * xinc + xorig
        v = (raw.astype(np.float64) - yref) * yinc + yorig
        return t, v

    def screenshot(self, scope):
        return scope.inst.query_binary_values(":DISPlay:DATA? PNG,COLor",
                                              datatype="B", container=bytearray)

    def transfer_plan(self, averaged, points):
        """A record stopped out of RUN - which is what an averaged build leaves -
        only answers in the NORMal/MAXimum points modes; RAW gets +109 "No Data
        For Operation". MAXimum serves everything there is (7680 points on this
        scope), and behaves as RAW on a record that a :SINGle left behind.

        The whole averaged record is those same 7680 points, so asking for more
        raises -222 "Data out of range" and a transfer-points limit is beside
        the point at that size."""
        if averaged:
            return "MAXimum", None
        return "RAW", points


PROFILES = {p.key: p for p in (KeysightInfiniiVision(),)}
DEFAULT_PROFILE = "msox2014a"


def get_profile(key):
    """The named profile, or the default if the name is not one we have - a
    config file naming a scope this version has never heard of should start the
    program on the usual one rather than refuse to start."""
    return PROFILES.get(key) or PROFILES[DEFAULT_PROFILE]
