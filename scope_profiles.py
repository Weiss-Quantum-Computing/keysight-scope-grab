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
    # The key the hit count is filed under in a settings snapshot. On a
    # Keysight it is also a real SCPI root; on a Rigol there is no such
    # command and the number comes out of the waveform preamble, so what
    # reads it is hit_count() below and this is only a label.
    wave_count = ""
    ch_scale = ""         # per-channel volts/div, with {ch}
    ch_offset = ""        # per-channel vertical offset, with {ch}
    # Volts per ADC code at 1 V/div. The 8-bit converter quantises the input,
    # and the offset dither steps by whole codes, so this is what a code is
    # worth. Every scope has its own: it is the full-scale span the converter
    # covers divided by 256, expressed per division.
    adc_code_per_vdiv = 0.0
    # The scope's own measurement results, if it has such a command, else None.
    # Recorded in the metadata when the Measurements tab asks for it; a scope
    # without it is never asked, rather than being asked and timing out.
    meas_results = None
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

    def accumulate(self, scope, count, wait_s, cancelled, progress, channels):
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

    def hit_count(self, scope):
        """How many hits are in the record being read out, as the instrument
        reports it, or None if it will not say. A string, so it lands in the
        settings snapshot beside the instrument's other unrounded replies."""
        raise NotImplementedError

    def before_single(self, scope):
        """Called before a single acquisition is armed; whatever it returns is
        handed back to after_single. For an instrument where arming is also a
        settings write - a Rigol :SINGle is a :TRIGger:SWEep write - this is
        where the setting is saved so the capture does not quietly rewrite the
        trigger setup."""
        return None

    def after_single(self, scope, state):
        """Undo whatever before_single saved. Runs on every exit from a single
        acquisition, including a cancel and a timeout."""

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
    ch_scale = ":CHANnel{ch}:SCALe"
    ch_offset = ":CHANnel{ch}:OFFSet"
    # MEASURED 2 Sep 2026 on the Trek monitor: a fixed ~3.4 mV pk-pk error
    # pattern repeating exactly every 40.25 mV of input at 1 V/div, which is
    # 10.24 V / 256. That is the code size.
    adc_code_per_vdiv = 0.04025
    meas_results = ":MEASure:RESults"

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

    def accumulate(self, scope, count, wait_s, cancelled, progress, channels):
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
        inst.write(f":WAVeform:SOURce CHANnel{channels[0]}")
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

    def hit_count(self, scope):
        # A real command here, and one the scope refuses when acquisition
        # memory is empty - hence try_get and its short timeout.
        return scope.try_get(self.wave_count, timeout_ms=2000)

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


class RigolDS1000Z(ScopeProfile):
    """Rigol MSO1000Z/DS1000Z. Established against a DS1054Z on firmware
    00.04.04.SP1, 2 Sep 2026; the probe reports are in docs/.

    More carries over from the Keysight than expected - the waveform preamble
    has the same ten fields in the same order, and :ACQuire:TYPE answers with
    the same NORM/AVER/PEAK/HRES - so most of this is a table. The two places
    it does not are accumulate() and before_single(), and both are commented
    where they sit."""

    def __init__(self):
        # y-scaling from the last preamble read, per channel, so an
        # averaged sum of raw codes can be turned into volts at the end.
        self._scale = {}

    key = "ds1054z"
    name = "Rigol DS1054Z"
    idn_keys = ("RIGOL",)
    # LAN as well as USB: unlike the MSO-X this one has an Ethernet port, and a
    # TCPIP resource is worth scanning for.
    resource_hints = ("USB", "TCPIP")
    # No preference. Keysight VISA does enumerate it over USBTMC, and so does
    # NI-VISA; nothing measured says either is better, so take what is there.
    visa_dll = None
    channels = (1, 2, 3, 4)
    # MEASURED: :DISPlay:DATA? ON,0,PNG returns 800x480, 36-43 kB.
    preview_size = (576, 346)

    timebase = [
        # :TIMebase:SCALe answers too - MAIN is optional in the guide and the
        # scope accepts both - but the explicit form is what the manual writes.
        ("Timebase s/div", ":TIMebase:MAIN:SCALe", "num", None),
        ("Position (s)", ":TIMebase:MAIN:OFFSet", "num", None),
        # No :TIMebase:REFerence on this scope; MEASURED as no answer at all.
        ("Sweep mode", ":TIMebase:MODE", "choice", ("MAIN", "XY", "ROLL")),
        ("Acquisition", ":ACQuire:TYPE", "choice", ("NORM", "AVER", "PEAK", "HRES")),
        # NOT :ACQuire:COUNt, which does not exist here, and powers of two only:
        # the guide gives 2^n for n in 1..10, so 2 to 1024.
        ("Averages", ":ACQuire:AVERages", "num", None),
    ]
    trigger = [
        ("Type", ":TRIGger:MODE", "choice",
         ("EDGE", "PULS", "RUNT", "WIND", "NEDG", "SLOP", "VID", "PATT",
          "DEL", "TIM", "DUR", "SHOL", "RS232", "IIC", "SPI")),
        # SINGle is a third sweep mode here rather than a separate command -
        # see before_single().
        ("Sweep", ":TRIGger:SWEep", "choice", ("AUTO", "NORM", "SING")),
        # Spelled EDGE, not the guide's EDGe: SCPI header matching is
        # case-insensitive and EDGE is the long form, so this is the same root
        # written the way the Keysight profile writes it. MEASURED: the scope
        # answers :TRIGger:EDGE:SOURce? identically.
        ("Source", ":TRIGger:EDGE:SOURce", "choice",
         ("CHAN1", "CHAN2", "CHAN3", "CHAN4", "AC")),
        ("Level (V)", ":TRIGger:EDGE:LEVel", "num", None),
        ("Slope", ":TRIGger:EDGE:SLOPe", "choice", ("POS", "NEG", "RFAL")),
        # The Keysight has :TRIGger:EDGE:REJect; this is a different command
        # with a different range, not a spelling of the same one.
        ("Coupling", ":TRIGger:COUPling", "choice", ("AC", "DC", "LFR", "HFR")),
        ("Noise reject", ":TRIGger:NREJect", "bool", ("ON", "OFF")),
        ("Holdoff (s)", ":TRIGger:HOLDoff", "num", None),
    ]
    channel = [
        ("V/div", ":CHANnel{ch}:SCALe", "num", None),
        ("Offset", ":CHANnel{ch}:OFFSet", "num", None),
        # Three values, not two: the Keysight has no GND position.
        ("Coupling", ":CHANnel{ch}:COUPling", "choice", ("AC", "DC", "GND")),
        ("Probe", ":CHANnel{ch}:PROBe", "choice",
         ("0.01", "0.02", "0.05", "0.1", "0.2", "0.5", "1", "2", "5", "10",
          "20", "50", "100", "200", "500", "1000")),
        ("Units", ":CHANnel{ch}:UNITs", "choice",
         ("VOLT", "WATT", "AMP", "UNKN")),
        # {20M|OFF}, so it cannot be a bool: the panel would write ON and the
        # scope would reject it. A choice with the scope's own mnemonics.
        ("BW lim", ":CHANnel{ch}:BWLimit", "choice", ("20M", "OFF")),
        ("Invert", ":CHANnel{ch}:INVert", "bool", ("ON", "OFF")),
        ("Display", ":CHANnel{ch}:DISPlay", "bool", ("ON", "OFF")),
    ]
    # Memory depth is read-only here on purpose. It is a writable command, but
    # MEASURED: which values it will take depends on how many channels are on,
    # and it rejects the rest SILENTLY - the value simply does not change and
    # the error queue stays empty, so errors() cannot report it and an Apply
    # would look as though it had worked. Read-only keeps the panel honest, and
    # matches the MSO-X, whose :ACQuire:POINts is a result rather than a knob.
    info = [
        ("Sample rate (Sa/s)", ":ACQuire:SRATe"),
        ("Memory depth", ":ACQuire:MDEPth"),
    ]
    actions = [
        ("Run", ":RUN", "running continuously", None, False),
        ("Stop", ":STOP", "stopped", None, False),
        # Marked as not rewriting the panel even though it does change the
        # trigger sweep. The flag decides whether the re-read afterwards may
        # overwrite edits the user has not applied yet, and this changes one
        # field: show_settings already updates any field the user is not
        # part-way through editing, so the sweep lands correctly without it,
        # and setting it would throw away pending edits on unrelated fields.
        ("Single", ":SINGle", "armed for one trigger - note this also sets the "
         "trigger sweep to SINGle, which is what the scope's own SINGLE button "
         "does", None, False),
        ("Force trig", ":TFORce", "trigger forced", None, False),
        ("Clear", ":CLEar", "display cleared", None, False),
        ("Autoscale", ":AUToscale", "autoscaled",
         "Autoscale rewrites the timebase and every channel's V/div and offset "
         "from whatever signal it finds, discarding the current setup.\n\nGo ahead?",
         True),
    ]
    write_first = (":ACQuire:TYPE", ":TRIGger:MODE", ":TIMebase:MODE")
    depends_on = {
        ":ACQuire:AVERages": (":ACQuire:TYPE", ("AVER",)),
        ":TRIGger:EDGE:SOURce": (":TRIGger:MODE", ("EDGE",)),
        ":TRIGger:EDGE:LEVel": (":TRIGger:MODE", ("EDGE",)),
        ":TRIGger:EDGE:SLOPe": (":TRIGger:MODE", ("EDGE",)),
        ":TRIGger:COUPling": (":TRIGger:MODE", ("EDGE",)),
    }

    acq_type = ":ACQuire:TYPE"
    acq_count = ":ACQuire:AVERages"
    avg_prefix = "AVER"
    avg_name = "AVERages"
    ch_display = ":CHANnel{ch}:DISPlay"
    # A label only. There is no :WAVeform:COUNt on this scope; the number comes
    # out of the preamble - see hit_count().
    wave_count = "hits in trace"
    ch_scale = ":CHANnel{ch}:SCALe"
    ch_offset = ":CHANnel{ch}:OFFSet"
    # MEASURED: the preamble's yincrement is exactly 0.04 V at 1 V/div and
    # 0.08 V at 2 V/div, so 25 codes per division - 200 over the eight-division
    # screen. Close to the MSO-X but not the same number.
    adc_code_per_vdiv = 0.04
    # No :MEASure:RESults on this scope. Asking would cost a timeout and a
    # device clear on every grab, so the metadata simply does not carry the
    # row - see Scope.metadata and the Measurements tab.
    meas_results = None

    cmd_run = ":RUN"
    cmd_stop = ":STOP"
    cmd_single = ":SINGle"

    meta_head = [
        ("sample rate (Sa/s)", ":ACQuire:SRATe"),
        ("memory depth", ":ACQuire:MDEPth"),
        ("acquisition type", ":ACQuire:TYPE"),
    ]
    meta_tail = [
        ("timebase s/div", ":TIMebase:MAIN:SCALe"),
        ("timebase position", ":TIMebase:MAIN:OFFSet"),
        ("timebase mode", ":TIMebase:MODE"),
        ("trigger type", ":TRIGger:MODE"),
        ("trigger sweep", ":TRIGger:SWEep"),
        ("trigger source", ":TRIGger:EDGE:SOURce"),
        ("trigger level", ":TRIGger:EDGE:LEVel"),
        ("trigger slope", ":TRIGger:EDGE:SLOPe"),
        ("trigger coupling", ":TRIGger:COUPling"),
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
        """MEASURED: :TRIGger:STATus? answers TD, WAIT, RUN, AUTO or STOP.
        Only STOP means the acquisition has finished; WAIT is armed and waiting,
        and AUTO is free-running, both of which are still running."""
        return scope.inst.query(":TRIGger:STATus?").strip().upper() != "STOP"

    def before_single(self, scope):
        """MEASURED: :SINGle here is a :TRIGger:SWEep SINGle write and leaves
        the sweep there afterwards. The guide says so in passing - it is
        "equivalent to ... sending the :TRIGger:SWEep SINGle command" - and it
        means an ordinary capture would quietly rewrite a trigger setting the
        front panel shows as the user's, and leave it rewritten.

        So the sweep is saved here and put back in after_single. Found by
        walking into it: the bench probe armed singles in two phases and
        restored the sweep in neither, which parked the scope in single-sweep
        mode and made everything afterwards wait forever for a trigger that a
        stopped scope was never going to produce."""
        return scope.try_get(":TRIGger:SWEep", timeout_ms=2000)

    def after_single(self, scope, state):
        if state:
            scope.inst.write(f":TRIGger:SWEep {state}")

    def accumulate(self, scope, count, wait_s, cancelled, progress, channels):
        """Average here, in software, rather than asking the scope to do it.

        Not a preference - the scope cannot be asked to do it honestly. MEASURED
        on a DS1054Z, 00.04.04.SP1, 2 Sep 2026:

        * There is no :DIGitize. It answers -113, "Undefined header". So there
          is no command that counts out N triggers and stops.
        * The preamble's count field reports the SETTING, not the hits so far.
          Asking for a 1024-deep average made it read 1024 within 0.4 s and
          stay there, while the noise on the trace was still visibly falling
          for another five seconds. Polling it cannot tell you an average is
          finished, exactly as on the MSO-X.
        * Nothing resets the averager. Not :CLEar, not a stop/run cycle. So
          whatever was on the probe before is still in the record.

        Three ways of being lied to and no way to check, which leaves building
        it here. That turns out to be better rather than merely safer:

        * The depth is exact and known, because we counted the sweeps.
        * There is no contamination: the average starts empty every time.
        * It is far higher resolution. The scope hands back 8-bit codes
          whatever the format, so its own average is re-quantised on the way
          out and arrives with the resolution thrown away. MEASURED, 64 deep:
          the scope's own average came back using 13-14 distinct codes, while
          64 raw traces averaged here came back using 188. Thirteen times the
          effective resolution, from the same 64 sweeps.

        The cost is speed. Each trace is a full VISA round trip, MEASURED at
        about 157 ms for 1200 points, so 64 deep takes ten seconds where the
        MSO-X's :DIGitize would take 64 trigger periods. Deep averages are
        correspondingly slow and the panel says so while it works.

        Results are left in scope.averaged for read_waveform to collect, one
        entry per channel, all from the same sweeps so the channels stay
        simultaneous.

        Returns the number of sweeps actually averaged, 0 if none arrived, or
        None if cancelled.
        """
        sums, t_axis, got = {}, None, 0
        started = time.time()
        alive = started
        scope.inst.write(self.cmd_run)
        # The readout settings do not change between sweeps, so set them once
        # rather than in every read - it is the largest part of the round trip.
        for ch in channels:
            scope.inst.write(f":WAVeform:SOURce CHANnel{ch}")
            scope.inst.write(":WAVeform:MODE NORMal")
            scope.inst.write(":WAVeform:FORMat BYTE")
        last = None
        while got < count:
            if cancelled is not None and cancelled():
                return None
            if wait_s > 0 and time.time() - alive > wait_s:
                break
            try:
                sweep = {ch: self._raw(scope, ch) for ch in channels}
            except Exception:
                continue
            first = sweep[channels[0]]
            if first is None or not len(first[1]):
                continue
            # Reading faster than the scope sweeps returns the same record
            # twice, and counting that as two hits would overstate the depth
            # the same way the scope's own counter does.
            codes = first[1].tobytes()
            if codes == last:
                continue
            last = codes
            for ch, (t, v) in sweep.items():
                sums[ch] = v.astype(np.float64) if ch not in sums else sums[ch] + v
                if t_axis is None:
                    t_axis = t
            got += 1
            alive = time.time()
            if progress is not None:
                progress(time.time() - started)
        if got:
            scope.averaged_depth = got
            for ch in sums:
                scope.averaged[ch] = (t_axis, sums[ch] / got)
        return got

    def _raw(self, scope, ch):
        """One channel's codes, with the readout settings assumed already set.
        Returns (t, raw codes) - unscaled, so summing stays in integers until
        the division at the end."""
        w = scope.inst
        w.write(f":WAVeform:SOURce CHANnel{ch}")
        pre = w.query(":WAVeform:PREamble?").strip().split(",")
        raw = w.query_binary_values(":WAVeform:DATA?", datatype="B",
                                    container=np.array)
        xinc, xorig, xref = float(pre[4]), float(pre[5]), float(pre[6])
        t = (np.arange(len(raw)) - xref) * xinc + xorig
        self._scale[ch] = (float(pre[7]), float(pre[8]), float(pre[9]))
        return t, raw

    def read_waveform(self, scope, channel, points_mode, points):
        """A trace this profile averaged, if there is one waiting, otherwise a
        live read.

        accumulate leaves its results in scope.averaged and they are collected
        exactly once - a later capture that is not averaging must not be handed
        the last one's trace."""
        if channel in scope.averaged:
            t, codes = scope.averaged.pop(channel)
            yinc, yorig, yref = self._scale[channel]
            return t, (codes - yref - yorig) * yinc
        w = scope.inst
        w.write(f":WAVeform:SOURce CHANnel{channel}")
        w.write(f":WAVeform:MODE {points_mode}")
        # BYTE, not WORD. MEASURED, and the guide agrees: WORD here is the same
        # eight bits padded into sixteen - "the lower 8 bits are valid and the
        # higher 8 bits are 0" - so it doubles the transfer for nothing. The
        # opposite of the MSO-X, where WORD carries a real 16x on an averaged
        # record.
        w.write(":WAVeform:FORMat BYTE")
        pre = w.query(":WAVeform:PREamble?").strip().split(",")
        xinc, xorig, xref = float(pre[4]), float(pre[5]), float(pre[6])
        yinc, yorig, yref = float(pre[7]), float(pre[8]), float(pre[9])
        raw = w.query_binary_values(":WAVeform:DATA?", datatype="B",
                                    container=np.array)
        t = (np.arange(len(raw)) - xref) * xinc + xorig
        v = (raw.astype(np.float64) - yref - yorig) * yinc
        return t, v

    def screenshot(self, scope):
        # MEASURED: 800x480 PNG, 36-43 kB. The parameters are
        # [<color>,<invert>,<format>] and the default format is BMP24, so all
        # three have to be given to get a PNG.
        return scope.inst.query_binary_values(":DISPlay:DATA? ON,0,PNG",
                                              datatype="B", container=bytearray)

    def hit_count(self, scope):
        """There is no :WAVeform:COUNt here; the count is preamble field 3.

        It is worth almost nothing on this scope - MEASURED, it reports the
        SETTING rather than the hits accumulated - so it is not what the
        metadata records. When accumulate built the trace, the honest number is
        the one it counted, and the grab reports that instead."""
        if scope.averaged_depth is not None:
            # accumulate counted these sweeps itself, so this number is the one
            # thing about averaging on this scope that is not a guess.
            return str(scope.averaged_depth)
        pre = scope.try_get(":WAVeform:PREamble", timeout_ms=2000)
        if not pre:
            return None
        parts = pre.split(",")
        return parts[3].strip() if len(parts) >= 4 else None

    def transfer_plan(self, averaged, points):
        """MEASURED: NORMal, MAXimum and RAW all serve a record here, both
        while running and after a stop - nothing like the MSO-X rule where a
        record stopped out of RUN refuses RAW. So one mode does for everything.

        NORMal rather than MAXimum or RAW because it is the screen record,
        1200 points, which is what this profile is built for. The other two
        expose the deep memory on a stopped record - MEASURED at 300000 points
        - and reading that needs the :WAVeform:STARt/:STOP loop this profile
        does not implement. See docs/ds1000z_notes.md."""
        return "NORMal", points


PROFILES = {p.key: p for p in (KeysightInfiniiVision(),
                               RigolDS1000Z())}
DEFAULT_PROFILE = "msox2014a"


def get_profile(key):
    """The named profile, or the default if the name is not one we have - a
    config file naming a scope this version has never heard of should start the
    program on the usual one rather than refuse to start."""
    return PROFILES.get(key) or PROFILES[DEFAULT_PROFILE]
