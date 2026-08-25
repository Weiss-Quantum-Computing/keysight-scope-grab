#!/usr/bin/env python3
"""
Scope Grab - one-click capture from a Keysight InfiniiVision MSO-X 2014A.

Click a button, get a timestamped CSV of the waveform, a PNG of the screen,
and a metadata text file in your chosen folder. No licenses, no BenchVue.

Requires: Keysight IO Libraries Suite + `pip install pyvisa numpy pillow`
          (pillow only sharpens the screenshot preview - the rest works without it)
Run with:  pythonw scope_grab.py      (pythonw = no console window)
"""

import base64
import datetime
import io
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pyvisa

try:
    from PIL import Image, ImageTk        # smooth (Lanczos) preview rescale
except ImportError:                       # without pillow: Tk's integer subsample
    Image = ImageTk = None

KTVISA = r"C:\Windows\System32\ktvisa32.dll"
# Remembered between sessions: output folder, filename prefix, channel names.
# Kept out of the program folder so a git pull cannot clobber it.
CONFIG_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                           "ScopeGrab", "config.json")
# Named setups, saved and loaded by hand - the counterpart of the AWG GUI's
# awg_setups folder, and on the Desktop for the same reason: a setup is a lab
# record, so it lives where the data does rather than inside the program folder.
SETUP_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "scope_setups")
NOT_MEASURED = 9.9e37

# CSV number formats. Samples arrive as 8-bit codes - 256 levels - so six
# significant digits already record far more than the scope resolves, and the
# default %.18e was writing (and costing) fifteen digits of noise per sample.
# Time keeps more digits because a long record has to separate adjacent samples.
TIME_FMT = "%.9e"
VOLT_FMT = "%.6e"

# Screenshot preview box, sized to fill the panel width. The scope sends
# 800x503 PNGs, which fit this at ~72%.
PREVIEW_W, PREVIEW_H = 576, 362

# Widgets that already use Space themselves. Space is only the GRAB shortcut
# when the focus is not sitting on one of these - otherwise typing a space in
# the prefix box (or toggling a focused checkbox) would fire an acquisition.
SPACE_OWNERS = {
    "Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox",
    "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton",
    "Button", "TButton",
}

BAD_NAME_CHARS = r'<>:"/\|?*'

# Settings the panel shows and can push back. Each entry is
# (label, SCPI root, kind, choices); the root is queried as "<root>?" and
# written as "<root> <value>", and doubles as the dict key.
#   num    - free-form number
#   choice - fixed list, scope answers with the same mnemonics
#   bool   - fixed list, but the scope answers 1/0
TIMEBASE_SETTINGS = [
    ("Timebase s/div", ":TIMebase:SCALe", "num", None),
    ("Position (s)", ":TIMebase:POSition", "num", None),
    ("Reference", ":TIMebase:REFerence", "choice", ("LEFT", "CENT", "RIGH")),
    ("Sweep mode", ":TIMebase:MODE", "choice", ("MAIN", "WIND", "XY", "ROLL")),
    ("Acquisition", ":ACQuire:TYPE", "choice", ("NORM", "AVER", "HRES", "PEAK")),
    ("Averages", ":ACQuire:COUNt", "num", None),
]
TRIGGER_SETTINGS = [
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
# Writes that have to land before others in the same Apply. The average count is
# ignored unless the acquisition type is already AVERage, and the edge fields
# belong to a trigger type that has to be selected first. Everything else is
# written afterwards, in panel order.
WRITE_FIRST = (":ACQuire:TYPE", ":TRIGger:MODE", ":TIMebase:MODE")
# Fields the instrument only acts on in a particular mode. The panel greys the
# others out rather than letting a value the scope is ignoring look live.
# {field: (field that decides it, mnemonics that make it live)}
DEPENDS_ON = {
    ":ACQuire:COUNt": (":ACQuire:TYPE", ("AVER",)),
    ":TRIGger:EDGE:SOURce": (":TRIGger:MODE", ("EDGE",)),
    ":TRIGger:EDGE:LEVel": (":TRIGger:MODE", ("EDGE",)),
    ":TRIGger:EDGE:SLOPe": (":TRIGger:MODE", ("EDGE",)),
    ":TRIGger:EDGE:REJect": (":TRIGger:MODE", ("EDGE",)),
}
# One-shot commands: (button, SCPI, what to log, confirmation text or None).
# They carry no value and there is nothing to read back, so they are not part of
# the settings snapshot - the panel is re-read afterwards instead.
ACTIONS = [
    ("Run", ":RUN", "running continuously", None),
    ("Stop", ":STOP", "stopped", None),
    ("Single", ":SINGle", "armed for one trigger", None),
    ("Force trig", ":TRIGger:FORCe", "trigger forced", None),
    ("Clear", ":CDISplay",
     "display cleared - averaging and persistence start over", None),
    ("Autoscale", ":AUToscale", "autoscaled",
     "Autoscale rewrites the timebase and every channel's V/div and offset "
     "from whatever signal it finds, discarding the current setup.\n\nGo ahead?"),
]
# Read-only values, refreshed on the same pass as the settings above. They are
# per-acquisition results rather than knobs, so the panel shows them but never
# writes them.
INFO_SETTINGS = [
    ("Sample rate (Sa/s)", ":ACQuire:SRATe"),
    ("Points acquired", ":ACQuire:POINts"),
]
# How many hits are in the trace being read out. In AVERage mode that is the
# averaging depth the capture actually got, which is not the same thing as the
# count that was asked for - and nothing on the scope's own screen distinguishes
# the two. It has no field of its own; it is folded into the grab's snapshot for
# the metadata file.
#
# Not part of a normal settings read: it describes a record rather than a
# setting, and with acquisition memory empty - straight after an acquisition
# type change, for one - the scope raises +109,"No Data For Operation" instead
# of answering, leaving the query unterminated and the read waiting out the VISA
# timeout. It is only ever asked where a record is known to exist.
WAVE_COUNT = ":WAVeform:COUNt"
CHANNEL_SETTINGS = [
    ("V/div", ":CHANnel{ch}:SCALe", "num", None),
    ("Offset", ":CHANnel{ch}:OFFSet", "num", None),
    ("Coupling", ":CHANnel{ch}:COUPling", "choice", ("AC", "DC")),
    ("Probe", ":CHANnel{ch}:PROBe", "num", None),
    ("Units", ":CHANnel{ch}:UNITs", "choice", ("VOLT", "AMP")),
    ("BW lim", ":CHANnel{ch}:BWLimit", "bool", ("ON", "OFF")),
    ("Invert", ":CHANnel{ch}:INVert", "bool", ("ON", "OFF")),
    ("Display", ":CHANnel{ch}:DISPlay", "bool", ("ON", "OFF")),
]


def safe_column(name):
    """Turn a typed channel name into something safe to put in a CSV header:
    ASCII word characters only, so no delimiter or encoding surprises when the
    header is written or parsed."""
    out = "".join(c if (c.isascii() and (c.isalnum() or c in "-.")) else "_"
                  for c in name.strip())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def free_base(base):
    """Add a suffix rather than overwrite a capture that is already there."""
    if not os.path.exists(base + ".csv"):
        return base
    n = 2
    while os.path.exists(f"{base}_{n}.csv"):
        n += 1
    return f"{base}_{n}"


def fmt_setting(kind, raw):
    """Normalise a scope reply into what the panel displays."""
    raw = raw.strip()
    if kind == "bool":
        return "OFF" if raw in ("0", "OFF", "off") else "ON"
    if kind in ("num", "info"):
        try:
            return f"{float(raw):g}"
        except ValueError:
            return raw
    return raw.upper()


# ---------------------------------------------------------------------------
# Instrument layer
# ---------------------------------------------------------------------------

def setting_groups():
    """(group title, [(label, scpi root)]) in panel order. What a saved setup's
    .txt companion is laid out from."""
    groups = [("Timebase / acquisition",
               [(lbl, scpi) for lbl, scpi, _, _ in TIMEBASE_SETTINGS]),
              ("Trigger",
               [(lbl, scpi) for lbl, scpi, _, _ in TRIGGER_SETTINGS])]
    for ch in (1, 2, 3, 4):
        groups.append((f"CH{ch}", [(lbl, tmpl.format(ch=ch))
                                   for lbl, tmpl, _, _ in CHANNEL_SETTINGS]))
    return groups


def describe_setup(cfg):
    """The .txt written beside a saved setup: the same numbers, laid out to be
    read in a lab notebook rather than parsed. The .json is the one that gets
    loaded back."""
    lines = [f"Scope Grab setup - saved {cfg.get('saved', '?')}"]
    if cfg.get("instrument"):
        lines.append(f"Instrument: {cfg['instrument']}")
    if cfg.get("read_stamp"):
        lines.append(f"Panel last read from the scope at {cfg['read_stamp']}")
    else:
        lines.append("Panel had never been read from a scope when this was saved")
    pending = cfg.get("unapplied_edits") or []
    if pending:
        lines.append("")
        lines.append(f"{len(pending)} field(s) were unapplied edits at save time, so "
                     "this file records what")
        lines.append("was on screen, not what the scope had:")
        lines += [f"    {scpi}" for scpi in pending]
    settings = cfg.get("settings") or {}
    for title, items in setting_groups():
        rows = [(lbl, settings[scpi]) for lbl, scpi in items if scpi in settings]
        if not rows:
            continue
        width = max(len(lbl) for lbl, _ in rows)
        lines.append("")
        lines.append(title)
        lines += [f"  {lbl:<{width}}  {val}" for lbl, val in rows]
    grab = cfg.get("grab") or {}
    if grab:
        lines += ["", "Capture settings"]
        lines.append(f"  Prefix            {grab.get('prefix', '')}")
        lines.append(f"  Trigger wait (s)  {grab.get('trigger_wait', '')}")
        lines.append(f"  Transfer points   {grab.get('transfer_points', '')}")
        names = grab.get("channel_names") or {}
        ticked = grab.get("channels") or {}
        for ch in ("1", "2", "3", "4"):
            lines.append(f"  CH{ch} {'on ' if ticked.get(ch) else 'off'}"
                         f"  {names.get(ch, '')}".rstrip())
    return "\n".join(lines) + "\n"


class Scope:
    def __init__(self):
        self.rm = None
        self.inst = None
        self.idn = ""
        self.addr = ""

    def _make_rm(self):
        # Prefer Keysight VISA explicitly so a primary/secondary VISA mixup
        # with NI-VISA can't break us.
        if os.path.exists(KTVISA):
            try:
                rm = pyvisa.ResourceManager(KTVISA)
                rm.list_resources()
                return rm
            except Exception:
                pass
        return pyvisa.ResourceManager()

    def connect(self, addr=None):
        self.close()
        self.rm = self._make_rm()
        if addr:
            candidates = [addr]
        else:
            candidates = [r for r in self.rm.list_resources() if r.startswith("USB")]
        for res in candidates:
            try:
                dev = self.rm.open_resource(res)
                dev.timeout = 5000
                dev.read_termination = "\n"
                dev.write_termination = "\n"
                idn = dev.query("*IDN?").strip()
            except Exception:
                continue
            if "KEYSIGHT" in idn.upper() or "AGILENT" in idn.upper():
                dev.timeout = 30000
                dev.chunk_size = 1024 * 1024
                self.inst, self.idn, self.addr = dev, idn, res
                return idn
            dev.close()
        raise RuntimeError("No Keysight USB instrument found. Check the rear-panel "
                           "USB-B cable and that Connection Expert sees the scope.")

    def close(self):
        for obj in (self.inst, self.rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.inst = self.rm = None

    # -- acquisition ------------------------------------------------------

    def single(self, wait_s=10.0, cancelled=None):
        """Arm a single acquisition and wait for it to complete.

        Uses :SINGle rather than :DIGitize so the captured trace stays on the
        scope display - which matters if you also want the screenshot to match
        the data.

        wait_s <= 0 waits indefinitely, which is how a capture is primed before
        an experiment running elsewhere starts sending triggers. `cancelled` is
        polled so a long wait can be called off from the panel.

        Returns True if it triggered, False on timeout, None if cancelled.
        """
        self.inst.write(":SINGle")
        started = time.time()
        deadline = None if wait_s <= 0 else started + wait_s
        while deadline is None or time.time() < deadline:
            if cancelled is not None and cancelled():
                self.inst.write(":STOP")
                return None
            try:
                # Bit 3 of the Operation Status Condition register is the Run bit.
                cond = int(self.inst.query(":OPERegister:CONDition?"))
            except Exception:
                time.sleep(1.0 if wait_s <= 0 else min(wait_s, 1.0))
                return True
            if not (cond & 8):
                return True
            # Poll hard at first for a quick handoff, then back off: a wait of
            # minutes should not hammer the USB link 20 times a second.
            time.sleep(0.05 if time.time() - started < 2.0 else 0.25)
        self.inst.write(":STOP")
        return False

    def accumulate(self, count, wait_s=10.0, cancelled=None, progress=None,
                   source=1):
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

        wait_s is a STALL limit on the trigger: it restarts on every trigger
        event, so a deep average is allowed its many periods while a dead
        trigger is caught within one wait. <= 0 never gives up. `progress` is
        called with seconds elapsed; the full build takes count trigger
        periods (12.8 s for 256 at 20 Hz).

        Returns `count` on success, fewer if the triggers dried up, 0 if none
        ever came, None if cancelled. In every case but None the scope holds a
        stopped record readable in MAXimum mode (see the worker's read).
        """
        self.inst.query("*OPC?")          # settings writes land before arming
        self.inst.query(":TER?")          # clear the event register of history
        self.inst.write(":DIGitize")
        started = time.time()
        alive = started
        bad_polls = 0
        while True:
            time.sleep(0.4)
            if cancelled is not None and cancelled():
                self.inst.write(":STOP")
                return None
            try:
                # Bit 3 of the Operation Status Condition register is Run.
                running = bool(int(self.inst.query(":OPERegister:CONDition?")) & 8)
                if running and int(self.inst.query(":TER?")):
                    alive = time.time()
                bad_polls = 0
            except Exception:
                bad_polls += 1
                if bad_polls < 3:
                    continue
                self.inst.write(":STOP")
                running = False
            if not running:
                break
            if progress is not None:
                progress(time.time() - started)
            if wait_s > 0 and time.time() - alive > wait_s:
                self.inst.write(":STOP")
                break
        # The count is honest after a digitize, but only in a servable mode.
        self.inst.write(f":WAVeform:SOURce CHANnel{source}")
        self.inst.write(":WAVeform:POINts:MODE NORMal")
        got = self.try_get(WAVE_COUNT, timeout_ms=2000)
        try:
            return min(int(float(got)), count)
        except (TypeError, ValueError):
            return 0


    def is_running(self):
        # Bit 3 of the Operation Status Condition register is the Run bit.
        try:
            return bool(int(self.inst.query(":OPERegister:CONDition?")) & 8)
        except Exception:
            return False

    def freeze(self):
        """Use what the scope has already captured instead of arming a new
        acquisition. Stopping first matters: reading memory while the scope is
        still acquiring returns a record torn between two acquisitions. Returns
        whether it had been running, so its state can be put back."""
        was_running = self.is_running()
        self.inst.write(":STOP")
        return was_running

    def waveform(self, channel, points_mode="RAW", points=None):
        w = self.inst
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

    def screenshot(self):
        return self.inst.query_binary_values(":DISPlay:DATA? PNG,COLor",
                                             datatype="B", container=bytearray)

    # -- settings ---------------------------------------------------------

    def get(self, scpi):
        return self.inst.query(scpi + "?").strip()

    def put(self, scpi, value):
        self.inst.write(f"{scpi} {value}")

    def command(self, scpi):
        """Fire a one-shot command that carries no value and returns nothing."""
        self.inst.write(scpi)

    def try_get(self, scpi, timeout_ms=2000):
        """Ask for something the scope may decline to answer, and return None if
        it does. A refused query is not a reply that says so - the scope pushes
        an error and sends nothing, so the read waits out the whole timeout.
        Hence the short one here, a device clear to drop anything that then
        arrives late, and a drain of the error queue so what it left behind is
        not reported against the next thing the panel does."""
        saved = self.inst.timeout
        self.inst.timeout = timeout_ms
        try:
            return self.inst.query(scpi + "?").strip()
        except Exception:
            try:
                self.inst.clear()
            except Exception:
                pass
            self.errors()
            return None
        finally:
            self.inst.timeout = saved

    def errors(self):
        """Drain the scope's error queue, so a rejected setting gets reported
        instead of silently ignored."""
        found = []
        for _ in range(10):
            try:
                resp = self.inst.query(":SYSTem:ERRor?").strip()
            except Exception:
                break
            if resp.startswith("+0,") or resp.startswith("0,"):
                break
            found.append(resp)
        return found

    def metadata(self, channels, settings, names=None, label=None, existing=False):
        """Format the metadata file. `settings` is the raw {scpi root: reply}
        snapshot already read for the panel, so a grab only asks the scope once
        and the file describes the same instant the panel shows. Values are the
        instrument's own strings, unrounded."""
        s = lambda scpi: settings.get(scpi, "?")
        # An average count is only in force in AVERage mode, and the scope keeps
        # reporting the last one whatever the mode - so say when it is idle,
        # rather than leave a file that reads as averaged when it was not.
        averaging = s(":ACQuire:TYPE").upper().startswith("AVER")
        avg_note = "" if averaging else "   (not in use: acquisition type is not AVERage)"
        lines = [
            f"captured           : {datetime.datetime.now().isoformat()}",
            f"instrument         : {self.idn}",
            f"visa address       : {self.addr}",
        ] + ([f"sequence label     : {label}"] if label else []) + (
            ["capture mode       : existing trace on the scope, not a new trigger"]
            if existing else []) + [
            f"sample rate (Sa/s) : {s(':ACQuire:SRATe')}",
            f"points acquired    : {s(':ACQuire:POINts')}",
            f"acquisition type   : {s(':ACQuire:TYPE')}",
            f"averages           : {s(':ACQuire:COUNt')}{avg_note}",
        ] + ([f"averages taken     : {s(WAVE_COUNT)} of {s(':ACQuire:COUNt')}"
              f"   (hits actually in the trace that was read out)"]
             if averaging and WAVE_COUNT in settings else []) + [
            f"timebase s/div     : {s(':TIMebase:SCALe')}",
            f"timebase position  : {s(':TIMebase:POSition')}",
            f"timebase reference : {s(':TIMebase:REFerence')}",
            f"timebase mode      : {s(':TIMebase:MODE')}",
            f"trigger type       : {s(':TRIGger:MODE')}",
            f"trigger sweep      : {s(':TRIGger:SWEep')}",
            f"trigger source     : {s(':TRIGger:EDGE:SOURce')}",
            f"trigger level      : {s(':TRIGger:EDGE:LEVel')}",
            f"trigger slope      : {s(':TRIGger:EDGE:SLOPe')}",
            f"trigger reject     : {s(':TRIGger:EDGE:REJect')}",
            f"trigger noise rej  : {s(':TRIGger:NREJect')}",
            f"trigger holdoff    : {s(':TRIGger:HOLDoff')}",
        ]
        for ch in channels:
            if names and names.get(ch):
                lines.append(f"CH{ch} name          : {names[ch]}")
            lines += [
                f"CH{ch} V/div         : {s(f':CHANnel{ch}:SCALe')}",
                f"CH{ch} offset        : {s(f':CHANnel{ch}:OFFSet')}",
                f"CH{ch} coupling      : {s(f':CHANnel{ch}:COUPling')}",
                f"CH{ch} probe atten   : {s(f':CHANnel{ch}:PROBe')}",
                f"CH{ch} units         : {s(f':CHANnel{ch}:UNITs')}",
                f"CH{ch} bandwidth lim : {s(f':CHANnel{ch}:BWLimit')}",
                f"CH{ch} invert        : {s(f':CHANnel{ch}:INVert')}",
            ]
        return "\n".join(lines) + "\n"

    def run(self):
        try:
            self.inst.write(":RUN")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.scope = Scope()
        self.msgs = queue.Queue()
        self.busy = False
        self.auto_job = None
        self.seq_active = False   # a numbered sequence is running
        self.seq_job = None       # pending after() for the next run
        self.seq_index = 0
        self.seq_last = 0
        self.seq_width = 3
        self.seq_gap = 0.0
        self.seq_started = 0.0
        self.seq_t0 = 0.0
        self.seq_inflight = None  # label of the run currently being captured
        self.stop_flag = threading.Event()   # asks a waiting capture to give up
        self.grab_wrote = False              # did the last run produce files?
        self.seq_done = 0
        # The setups window is built when asked for, so these are None until it
        # exists and go back to None when it closes. set_busy checks.
        self.setup_win = None
        self.setup_save_btn = self.setup_load_btn = None

        root.title("Scope Grab - MSO-X 2014A")
        # Tall enough for the screenshot preview, but never taller than the
        # screen - otherwise the log ends up behind the taskbar.
        win_w = min(1200, root.winfo_screenwidth() - 80)
        win_h = min(960, root.winfo_screenheight() - 120)
        root.geometry(f"{win_w}x{win_h}+40+20")

        pad = dict(padx=8, pady=4)

        # Capture controls and scope settings on the left, screenshot preview
        # and log on the right.
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        # --- connection row
        top = ttk.Frame(left)
        top.pack(fill="x", **pad)
        self.status = ttk.Label(top, text="Not connected", foreground="#a00")
        self.status.pack(side="left")
        ttk.Button(top, text="Connect", command=self.do_connect).pack(side="right")
        ttk.Button(top, text="Load/save setups...",
                   command=self.do_setups).pack(side="right", padx=(0, 6))

        # --- channels
        chf = ttk.LabelFrame(left, text="Channels")
        chf.pack(fill="x", **pad)
        self.ch_vars = {}
        self.ch_names = {}
        ttk.Label(chf, text="capture").grid(row=0, column=0, padx=(8, 4))
        ttk.Label(chf, text="name").grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(chf, text="CSV column").grid(row=0, column=2, sticky="w", padx=4)
        for i, ch in enumerate((1, 2, 3, 4)):
            v = tk.BooleanVar(value=(ch == 1))
            ttk.Checkbutton(chf, text=f"CH{ch}", variable=v).grid(
                row=i + 1, column=0, sticky="w", padx=(8, 4), pady=1)
            self.ch_vars[ch] = v
            name = tk.StringVar()
            self.ch_names[ch] = name
            ttk.Entry(chf, textvariable=name, width=18).grid(
                row=i + 1, column=1, sticky="w", padx=4, pady=1)
            # Show the header the name will actually produce, so the sanitising
            # is never a surprise after the fact.
            shown = tk.StringVar(value=f"CH{ch}_V")
            ttk.Label(chf, textvariable=shown, foreground="#666").grid(
                row=i + 1, column=2, sticky="w", padx=4, pady=1)
            name.trace_add("write",
                           lambda *_, c=ch, s=shown: s.set(self.column_name(c)))

        # --- output folder + prefix
        of = ttk.LabelFrame(left, text="Save to")
        of.pack(fill="x", **pad)
        default_dir = os.path.join(os.path.expanduser("~"), "Desktop", "scope_data")
        self.outdir = tk.StringVar(value=default_dir)
        ttk.Entry(of, textvariable=self.outdir).pack(side="left", fill="x",
                                                    expand=True, padx=6, pady=6)
        ttk.Button(of, text="...", width=3, command=self.pick_dir).pack(side="left", padx=6)

        pf = ttk.Frame(left)
        pf.pack(fill="x", **pad)
        ttk.Label(pf, text="Filename prefix:").pack(side="left")
        self.prefix = tk.StringVar(value="scope")
        ttk.Entry(pf, textvariable=self.prefix).pack(side="left", fill="x",
                                                    expand=True, padx=6)

        # --- grab
        gf = ttk.Frame(left)
        gf.pack(fill="x", **pad)
        self.grab_btn = ttk.Button(gf, text="GRAB  (or press Space)",
                                   command=self.do_grab, state="disabled")
        self.grab_btn.pack(side="left", fill="x", expand=True, ipady=8)
        self.peek_btn = ttk.Button(gf, text="Peek (saves nothing)",
                                   command=self.do_peek, state="disabled")
        self.peek_btn.pack(side="left", padx=(6, 0), ipady=8)

        # The scope's own front-panel buttons, next to the one that captures:
        # these are things it does once rather than states it holds, so there is
        # nothing to edit and nothing to apply.
        cf = ttk.Frame(left)
        cf.pack(fill="x", padx=8)
        ttk.Label(cf, text="Scope:").pack(side="left", padx=(0, 4))
        self.action_btns = []
        for text, scpi, note, confirm in ACTIONS:
            btn = ttk.Button(cf, text=text, width=max(6, len(text) + 1),
                             state="disabled",
                             command=lambda s=scpi, n=note, k=confirm:
                             self.do_action(s, n, k))
            btn.pack(side="left", padx=(0, 4))
            self.action_btns.append(btn)

        ef = ttk.Frame(left)
        ef.pack(fill="x", **pad)
        # Deliberately not remembered between sessions: leaving it on by
        # accident would quietly save a stale trace as if it were a new capture.
        self.use_existing = tk.BooleanVar(value=False)
        ttk.Checkbutton(ef, text="take the trace already on the scope (no new trigger)",
                        variable=self.use_existing,
                        command=self.toggle_existing).pack(side="left")

        tf = ttk.Frame(left)
        tf.pack(fill="x", **pad)
        ttk.Label(tf, text="Wait for trigger:").pack(side="left")
        self.trig_wait = tk.StringVar(value="10")
        self.trig_entry = ttk.Entry(tf, textvariable=self.trig_wait, width=7)
        self.trig_entry.pack(side="left", padx=4)
        ttk.Label(tf, text="s (0 = no limit)").pack(side="left")
        self.phase = ttk.Label(tf, text="", foreground="#060")
        self.phase.pack(side="left", padx=10)

        nf = ttk.Frame(left)
        nf.pack(fill="x", **pad)
        ttk.Label(nf, text="Transfer points:").pack(side="left")
        self.trans_pts = tk.StringVar(value="max")
        ttk.Entry(nf, textvariable=self.trans_pts, width=10).pack(side="left", padx=4)
        ttk.Label(nf, text='"max" = whole acquisition memory',
                  foreground="#666").pack(side="left")

        af = ttk.Frame(left)
        af.pack(fill="x", **pad)
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(af, text="Auto-grab every", variable=self.auto,
                        command=self.toggle_auto).pack(side="left")
        self.interval = tk.StringVar(value="10")
        ttk.Entry(af, textvariable=self.interval, width=6).pack(side="left", padx=4)
        ttk.Label(af, text="seconds").pack(side="left")
        self.save_png = tk.BooleanVar(value=True)
        ttk.Checkbutton(af, text="save screenshot?",
                        variable=self.save_png).pack(side="left", padx=12)

        # --- numbered sequence
        qf = ttk.LabelFrame(left, text="Sequence (numbered instead of timestamped)")
        qf.pack(fill="x", **pad)
        row = ttk.Frame(qf)
        row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row, text="Runs:").pack(side="left")
        self.seq_count = tk.StringVar(value="10")
        ttk.Entry(row, textvariable=self.seq_count, width=6).pack(side="left", padx=(4, 10))
        ttk.Label(row, text="Interval (s):").pack(side="left")
        self.seq_interval = tk.StringVar(value="1")
        ttk.Entry(row, textvariable=self.seq_interval, width=6).pack(side="left", padx=(4, 10))
        ttk.Label(row, text="First label:").pack(side="left")
        self.seq_start = tk.StringVar(value="1")
        ttk.Entry(row, textvariable=self.seq_start, width=6).pack(side="left", padx=4)

        row2 = ttk.Frame(qf)
        row2.pack(fill="x", padx=6, pady=(2, 6))
        self.seq_btn = ttk.Button(row2, text="Start sequence",
                                  command=self.do_sequence, state="disabled")
        self.seq_btn.pack(side="left")
        self.seq_status = ttk.Label(row2, text="idle", foreground="#666")
        self.seq_status.pack(side="left", padx=8)
        self.seq_next = tk.StringVar()
        ttk.Label(qf, textvariable=self.seq_next, foreground="#666").pack(
            anchor="w", padx=8, pady=(0, 6))
        for var in (self.prefix, self.seq_start, self.seq_count):
            var.trace_add("write", lambda *_: self.show_next_name())
        self.show_next_name()

        self.toggle_existing()
        self.build_settings(left, pad)

        # --- last screenshot
        self.shot_frame = ttk.LabelFrame(right, text="Last screenshot")
        self.shot_frame.pack(fill="x", **pad)
        box = tk.Frame(self.shot_frame, width=PREVIEW_W, height=PREVIEW_H)
        box.pack(padx=4, pady=4)
        box.pack_propagate(False)          # keep the box from shrinking to the label
        self.preview = ttk.Label(box, text="(no screenshot yet)", anchor="center")
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Double-Button-1>", self.open_preview)
        # Wheel over the picture steps through the run, which is what "scroll
        # through them" means with a mouse in hand.
        self.preview.bind("<MouseWheel>",
                          lambda e: self.step_shot(-1 if e.delta > 0 else 1))
        self.preview_img = None
        self.preview_path = None
        self.shots = []           # screenshots of the current prefix, in order
        self.shot_i = -1
        self.follow = True        # sit on the newest as captures arrive

        nav = ttk.Frame(self.shot_frame)
        nav.pack(fill="x", padx=4, pady=(0, 4))
        self.prev_btn = ttk.Button(nav, text="< prev", width=8,
                                   command=lambda: self.step_shot(-1))
        self.prev_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="next >", width=8,
                                   command=lambda: self.step_shot(1))
        self.next_btn.pack(side="left", padx=4)
        self.newest_btn = ttk.Button(nav, text="newest", width=8,
                                     command=lambda: self.refresh_shots(newest=True))
        self.newest_btn.pack(side="left")
        self.shot_pos = ttk.Label(nav, text="", foreground="#666")
        self.shot_pos.pack(side="left", padx=8)

        # --- log
        lf = ttk.LabelFrame(right, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(lf, height=6, wrap="word", font=("Consolas", 9))
        # Wrapped continuations are indented, so a long message reads as one
        # entry rather than as several. Wrapping by width rather than at a fixed
        # column means it still fits after the window is resized.
        self.logbox.tag_configure("entry", lmargin2=30)
        self.logbox.pack(fill="both", expand=True, padx=4, pady=4)

        root.bind("<space>", self.on_space)
        root.bind("<Left>", lambda e: self.on_arrow(e, -1))
        root.bind("<Right>", lambda e: self.on_arrow(e, 1))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.pump)
        self.root.after(300, self.do_connect)
        self.saved_cfg = None
        self.load_config()
        self.load_latest_preview()

    # -- helpers ----------------------------------------------------------

    def log(self, text):
        self.msgs.put(text)

    def pump(self):
        while not self.msgs.empty():
            self.logbox.insert("end", self.msgs.get() + "\n", "entry")
            self.logbox.see("end")
        self.root.after(100, self.pump)

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or ".")
        if d:
            self.outdir.set(d)
            self.load_latest_preview()
            self.save_config()

    def current_cfg(self):
        return {
            "outdir": self.outdir.get(),
            "prefix": self.prefix.get(),
            "channel_names": {str(ch): var.get() for ch, var in self.ch_names.items()},
            "channels": {str(ch): var.get() for ch, var in self.ch_vars.items()},
            "trigger_wait": self.trig_wait.get(),
            "transfer_points": self.trans_pts.get(),
        }

    def load_config(self):
        """Restore what the last session was using. Anything missing, malformed
        or of the wrong type is ignored and leaves the default in place."""
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            return
        except Exception as exc:
            self.log(f"Ignoring unreadable {CONFIG_PATH}: {exc}")
            return

        outdir = cfg.get("outdir")
        if isinstance(outdir, str) and outdir.strip():
            self.outdir.set(outdir)
        self.load_grab_prefs(cfg)

        self.saved_cfg = self.current_cfg()
        self.log(f"Restored last session from {CONFIG_PATH}")
        if not os.path.isdir(self.outdir.get()):
            self.log(f"  (that folder does not exist yet: {self.outdir.get()})")

    def load_grab_prefs(self, cfg):
        """The capture-side fields, restored from either the session config or a
        saved setup - the two hold them under the same keys. Anything missing or
        of the wrong type leaves what is already there.

        The output folder is deliberately not one of these. It belongs to where
        you are working now, not to the setup being recalled, and a setup from
        another experiment silently redirecting where captures land is the one
        surprise here that costs you a file."""
        for key, var in (("prefix", self.prefix),
                         ("trigger_wait", self.trig_wait),
                         ("transfer_points", self.trans_pts)):
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                var.set(value)
        names = cfg.get("channel_names")
        if isinstance(names, dict):
            for ch, var in self.ch_names.items():
                value = names.get(str(ch))
                if isinstance(value, str):
                    var.set(value)
        ticked = cfg.get("channels")
        if isinstance(ticked, dict):
            for ch, var in self.ch_vars.items():
                value = ticked.get(str(ch))
                if isinstance(value, (bool, int)):
                    var.set(bool(value))

    def do_setups(self):
        """The Load/save setups window, off the connection row.

        A window rather than two more buttons in the settings panel: saving and
        loading happen once at the start and once at the end of a session, and
        the settings bar is for the things pressed while working. Same button in
        the same corner as the AWG GUI, so the two panels are one habit.

        Not modal. Loading offers to send the setup straight to the scope, and
        that goes off on a thread whose progress is reported to the log behind
        this window.
        """
        if self.setup_win is not None and self.setup_win.winfo_exists():
            self.setup_win.lift()
            self.setup_win.focus_force()
            return
        win = self.setup_win = tk.Toplevel(self.root)
        win.title("Load / save setups")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._setups_close)

        ttk.Label(win, justify="left", foreground="#444", text=(
            "Save setup writes the settings panel to a timestamped JSON, with a "
            "readable .txt beside it.\n"
            "Load setup puts one back in the panel and offers to send it to the "
            "scope.\n"
            "Both work with nothing connected - a setup is the panel, not a "
            "reading.")
        ).pack(anchor="w", padx=8, pady=(8, 4))

        ff = ttk.Frame(win)
        ff.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(ff, text="Folder:").pack(side="left", padx=(0, 4))
        ttk.Label(ff, text=SETUP_DIR, foreground="#444").pack(side="left")

        pf = ttk.Frame(win)
        pf.pack(fill="x", padx=8, pady=2)
        ttk.Label(pf, text="Prefix:").pack(side="left")
        ttk.Entry(pf, textvariable=self.prefix, width=16).pack(side="left", padx=4)
        self.setup_save_btn = ttk.Button(pf, text="Save setup",
                                         command=self.do_save_setup)
        self.setup_save_btn.pack(side="left", padx=(8, 4))
        self.setup_load_btn = ttk.Button(pf, text="Load setup...",
                                         command=self.do_load_setup)
        self.setup_load_btn.pack(side="left")

        ttk.Button(win, text="Close", command=self._setups_close).pack(
            anchor="w", padx=8, pady=(6, 8))
        # The window may have been opened mid-grab, when both buttons should be
        # dead until it finishes.
        self.set_busy(self.busy)

    def _setups_close(self):
        if self.setup_win is not None:
            self.setup_win.destroy()
        self.setup_win = None
        self.setup_save_btn = self.setup_load_btn = None

    def do_save_setup(self):
        """Write the panel's settings to a named file.

        The panel, not the instrument: it works with nothing connected, and what
        you can see is what gets saved. That includes an edit not applied yet -
        which is recorded as such rather than quietly swapped for the scope's
        own value, so a setup never claims to be a reading it isn't."""
        if self.busy:
            return
        pending = sorted(scpi for scpi in self.set_marks if self.edited(scpi))
        settings = {scpi: var.get().strip() for scpi, var in self.set_vars.items()
                    if self.set_kinds[scpi] != "info" and var.get().strip()}
        if not settings:
            self.log("Nothing to save - read from the scope first, or fill the "
                     "panel in by hand.")
            return
        # Greyed-out fields are saved even though Send all will not write them:
        # a setup that switches acquisition to AVERage has to carry the count
        # that goes with it, and which fields are live is decided on load by the
        # modes in the same file.
        cfg = {
            "app": "scope-grab",
            "version": 1,
            "saved": datetime.datetime.now().isoformat(timespec="seconds"),
            "instrument": self.scope.idn,
            "read_stamp": self.read_stamp,
            "unapplied_edits": pending,
            "settings": settings,
            "grab": {
                "prefix": self.prefix.get(),
                "channels": {str(ch): var.get() for ch, var in self.ch_vars.items()},
                "channel_names": {str(ch): var.get()
                                  for ch, var in self.ch_names.items()},
                "trigger_wait": self.trig_wait.get(),
                "transfer_points": self.trans_pts.get(),
            },
        }
        try:
            os.makedirs(SETUP_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.join(SETUP_DIR, f"{self.safe_prefix()}_{stamp}")
            with open(base + ".json", "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            with open(base + ".txt", "w", encoding="utf-8") as fh:
                fh.write(describe_setup(cfg))
        except Exception as exc:
            self.log(f"ERROR saving setup: {exc}")
            return
        self.log(f"Saved setup: {base}.json (+ .txt)")
        if pending:
            self.log(f"  ({len(pending)} field(s) saved as shown here, which is not "
                     "what the scope currently has)")

    def do_load_setup(self):
        """Fill the panel from a saved file.

        Loading never writes to the instrument by itself. The values land in the
        panel first, marked as edits against whatever the scope last reported,
        so you can see what is about to change - and then it offers to send
        them. Answer no and Send all does it whenever you are ready."""
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Load setup",
            initialdir=SETUP_DIR if os.path.isdir(SETUP_DIR) else ".",
            filetypes=[("Setup files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            settings = cfg["settings"]
            if not isinstance(settings, dict):
                raise ValueError("'settings' is not a JSON object")
        except Exception as exc:
            messagebox.showerror("Cannot read setup", str(exc), parent=self.root)
            self.log(f"Could not read {path}: {exc}")
            return

        loaded = skipped = 0
        for scpi, value in settings.items():
            if (scpi not in self.set_vars or self.set_kinds[scpi] == "info"
                    or not isinstance(value, (str, int, float))):
                skipped += 1
                continue
            self.set_vars[scpi].set(str(value).strip())
            loaded += 1
        grab = cfg.get("grab")
        if isinstance(grab, dict):
            self.load_grab_prefs(grab)
        self.log(f"Loaded {os.path.basename(path)}: {loaded} setting(s) into the panel"
                 + (f", {skipped} not recognised and skipped" if skipped else ""))
        if not loaded:
            return
        if not self.scope.inst:
            self.log("  Not connected - once you are, press Apply changes and say "
                     "yes when it offers to send the lot.")
            return
        writable = len(self.panel_settings())
        if messagebox.askyesno(
                "Load setup",
                f"{loaded} setting(s) are now in the panel.\n\n"
                f"Send {writable} of them to the scope now? This overwrites "
                "whatever the scope currently has, including anything changed "
                "at the front panel.\n\n"
                "Say no and they stay in the panel, marked as edits, for Apply "
                "changes to send later.",
                parent=self.root):
            self.do_send_all()

    def save_config(self):
        """Called after a grab, when the folder is picked, and on close. Writes
        only when something actually changed."""
        cfg = self.current_cfg()
        if cfg == self.saved_cfg:
            return
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            self.saved_cfg = cfg
        except Exception as exc:
            self.log(f"Could not save {CONFIG_PATH}: {exc}")

    def widget_owns_key(self, event):
        """True when the focused widget uses the key itself - typing in an entry
        must not fire a capture or jump the screenshot browser."""
        try:
            return event.widget.winfo_class() in SPACE_OWNERS
        except AttributeError:
            return False

    def on_space(self, event):
        if self.widget_owns_key(event):
            return
        self.do_grab()

    def on_arrow(self, event, delta):
        if self.widget_owns_key(event):
            return
        self.step_shot(delta)

    def safe_prefix(self):
        p = "".join("_" if c in BAD_NAME_CHARS else c
                    for c in self.prefix.get()).strip()
        return p or "scope"

    def render_preview(self, source):
        """Put a PNG in the preview box: `source` is a file path, or PNG bytes for
        a screenshot that was never written to disk. Main thread only, since Tk
        images are not thread safe."""
        try:
            if Image is not None:
                im = Image.open(source if isinstance(source, str)
                                else io.BytesIO(source))
                im.load()
                k = min(PREVIEW_W / im.width, PREVIEW_H / im.height, 1.0)
                if k < 1.0:
                    im = im.resize((max(1, round(im.width * k)),
                                    max(1, round(im.height * k))),
                                   Image.LANCZOS)
                img = ImageTk.PhotoImage(im)
            else:
                # Tk 8.6 reads PNG natively, from a file or from base64 data.
                img = (tk.PhotoImage(file=source) if isinstance(source, str)
                       else tk.PhotoImage(data=base64.b64encode(source)))
                k = 1
                while img.width() // k > PREVIEW_W or img.height() // k > PREVIEW_H:
                    k += 1
                if k > 1:
                    img = img.subsample(k)         # integer factors only
        except Exception as exc:
            self.log(f"  (preview failed: {exc})")
            return False
        self.preview_img = img            # keep a reference or Tk drops it
        self.preview.configure(image=img, text="")
        return True

    def show_preview(self, path):
        if self.render_preview(path):
            self.preview_path = path
            self.shot_frame.configure(
                text=f"Screenshot - {os.path.basename(path)}  "
                     f"(wheel or arrow keys to scroll, double-click to open)")

    def show_peek(self, data):
        """A screenshot held only in the window. preview_path goes to None: there
        is no file to open, and the browser has nothing new to point at."""
        if self.render_preview(data):
            self.preview_path = None
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            self.shot_frame.configure(
                text=f"Screenshot - scope screen at {stamp}, not saved")
            self.shot_pos.configure(text="not saved", foreground="#c60")

    def shot_paths(self):
        """Screenshots in the output folder that belong to the current prefix.
        Sorted by name, which puts a numbered sequence in run order and a
        timestamped set in capture order."""
        outdir, prefix = self.outdir.get(), self.safe_prefix() + "_"
        try:
            names = sorted(n for n in os.listdir(outdir)
                           if n.lower().endswith(".png") and n.startswith(prefix))
        except OSError:
            return []
        return [os.path.join(outdir, n) for n in names]

    def refresh_shots(self, newest=False):
        """Rescan after a capture or a folder change. Stays on the picture being
        looked at, so screenshots arriving mid-sequence do not yank the view
        forward - unless the newest was already on show, or `newest` is set."""
        # Tracked by index rather than by what is on screen, so a peek - which
        # displays no file at all - does not stop new captures being followed.
        current = self.shots[self.shot_i] if 0 <= self.shot_i < len(self.shots) else None
        self.shots = self.shot_paths()
        if not self.shots:
            self.shot_i = -1
            self.follow = True
            self.preview_path = None
            self.preview.configure(image="", text="(no screenshot yet)")
            self.shot_frame.configure(text="Last screenshot")
            self.shot_pos.configure(text="")
            for btn in (self.prev_btn, self.next_btn, self.newest_btn):
                btn.configure(state="disabled")
            return
        if newest or self.follow or current not in self.shots:
            self.shot_i = len(self.shots) - 1
        else:
            self.shot_i = self.shots.index(current)
        self.show_shot()

    def show_shot(self):
        if not self.shots:
            return
        self.shot_i = max(0, min(self.shot_i, len(self.shots) - 1))
        last = len(self.shots) - 1
        self.follow = self.shot_i == last
        self.show_preview(self.shots[self.shot_i])
        behind = last - self.shot_i
        self.shot_pos.configure(
            text=f"{self.shot_i + 1} / {len(self.shots)}"
                 + (f"   ({behind} newer)" if behind else ""),
            foreground="#c60" if behind else "#666")
        self.prev_btn.configure(state="normal" if self.shot_i > 0 else "disabled")
        self.next_btn.configure(state="normal" if behind else "disabled")
        self.newest_btn.configure(state="normal" if behind else "disabled")

    def step_shot(self, delta):
        if self.shots:
            self.shot_i += delta
            self.show_shot()

    def load_latest_preview(self):
        self.refresh_shots(newest=True)

    def open_preview(self, _event=None):
        if not self.preview_path:
            self.log("That screenshot was not saved, so there is no file to open.")
            return
        try:
            os.startfile(self.preview_path)
        except Exception as exc:
            self.log(f"ERROR: {exc}")

    def column_name(self, ch):
        """CSV header for a channel. The channel number is kept even when named,
        so a column stays traceable to the settings in the metadata file and two
        channels sharing a name cannot collide."""
        name = safe_column(self.ch_names[ch].get())
        return f"CH{ch}_{name}_V" if name else f"CH{ch}_V"

    def channels(self):
        return [ch for ch, v in self.ch_vars.items() if v.get()]

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy or self.seq_active or not self.scope.inst else "normal"
        for btn in (self.read_btn, self.apply_btn, self.peek_btn, *self.action_btns):
            btn.configure(state=state)
        # Save and Load live in a window that is usually not open, and they only
        # need the panel rather than the instrument - a setup is worth saving
        # whether or not the scope is plugged in, and loading one is how you
        # fill the panel before it is. So they follow the busy flag alone.
        for btn in (self.setup_save_btn, self.setup_load_btn):
            if btn is not None and btn.winfo_exists():
                btn.configure(state="disabled" if busy or self.seq_active
                              else "normal")
        # During a one-off grab the GRAB button becomes the way to call off a
        # long trigger wait. A sequence has its own Stop button instead.
        if busy and not self.seq_active:
            self.grab_btn.configure(text="Cancel wait", state="normal",
                                    command=self.cancel_grab)
        else:
            self.grab_btn.configure(text="GRAB  (or press Space)", state=state,
                                    command=self.do_grab)
        # The sequence button stays live while a sequence runs, so it can stop it.
        self.seq_btn.configure(
            state="normal" if self.scope.inst and (self.seq_active or not busy)
            else "disabled")

    # -- actions ----------------------------------------------------------

    def do_connect(self):
        def work():
            try:
                idn = self.scope.connect()
                self.root.after(0, lambda: self.status.configure(
                    text=idn[:70], foreground="#060"))
                self.log(f"Connected: {idn}")
                self.log(f"Address:   {self.scope.addr}")
                self.root.after(0, lambda: self.set_busy(False))
                values = self.read_all_settings()
                self.root.after(0, lambda v=values: self.show_settings(v))
            except Exception as exc:
                self.root.after(0, lambda: self.status.configure(
                    text="Not connected", foreground="#a00"))
                self.log(f"ERROR: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def do_peek(self):
        """Show the scope's screen without writing a file. Deliberately does not
        arm, stop or run the scope: it only asks for the rendered display, so a
        test in progress is left exactly as it was."""
        if self.busy or not self.scope.inst:
            return
        self.set_busy(True)
        threading.Thread(target=self._peek_worker, daemon=True).start()

    def _peek_worker(self):
        try:
            img = self.scope.screenshot()
            self.log(f"screenshot pulled into the window, nothing saved "
                     f"({len(img)} bytes)")
            self.root.after(0, lambda d=bytes(img): self.show_peek(d))
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            # Not grab_done: a peek is not a run and must not advance a sequence.
            self.root.after(0, lambda: self.set_busy(False))

    def do_grab(self):
        if self.busy or not self.scope.inst:
            return
        chans = self.channels()
        if not chans:
            self.log("Pick at least one channel.")
            return
        self.stop_flag.clear()
        self.set_busy(True)
        threading.Thread(target=self._grab_worker, args=(chans,), daemon=True).start()

    def toggle_existing(self):
        """The trigger wait is meaningless when we are not waiting for one."""
        self.trig_entry.configure(
            state="disabled" if self.use_existing.get() else "normal")

    def cancel_grab(self):
        self.stop_flag.set()
        self.log("Cancel requested - takes effect while waiting for a trigger")
        self.log("  a transfer already under way will finish")

    def set_phase(self, text):
        """Called from the capture thread."""
        self.root.after(0, lambda: self.phase.configure(text=text))

    def trigger_wait_s(self):
        try:
            return max(0.0, float(self.trig_wait.get()))
        except ValueError:
            return 10.0

    def averaging_depth(self):
        """How deep an average the scope is set to build, or None for a plain
        grab. Asked of the instrument rather than the panel: the panel's copy is
        whatever was last read, and the front panel may have moved since."""
        try:
            if not self.scope.get(":ACQuire:TYPE").upper().startswith("AVER"):
                return None
            n = int(float(self.scope.get(":ACQuire:COUNt")))
            return n if n > 1 else None
        except Exception:
            return None

    def transfer_points(self):
        """None means take everything in acquisition memory."""
        text = self.trans_pts.get().strip().lower()
        if text in ("", "max", "all", "0"):
            return None
        try:
            return max(100, int(float(text)))
        except ValueError:
            self.log(f"Transfer points: '{self.trans_pts.get()}' is not a number, "
                     f"taking the whole record.")
            return None

    def _grab_worker(self, chans, label=None):
        self.grab_wrote = False
        try:
            outdir = self.outdir.get()
            os.makedirs(outdir, exist_ok=True)
            # A sequence run is identified by its number; a one-off by the clock.
            # Either way the wall-clock time is recorded inside the .txt file.
            tag = label or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.join(outdir, f"{self.safe_prefix()}_{tag}")
            if label is None:
                # The timestamp only resolves to a second, so two grabs inside
                # the same second would otherwise overwrite each other. Sequence
                # labels have their own collision check in first_free().
                base = free_base(base)

            existing = self.use_existing.get()
            # Asked even for a use-existing grab: the read further down needs to
            # know whether it is looking at an averaged record, because those
            # only answer in the NORMal/MAXimum points modes.
            avg_want = self.averaging_depth()
            armed_at = time.time()
            if existing:
                # Take what is in acquisition memory now. Put the run state back
                # afterwards so a scope that was live stays live.
                self.set_phase("reading the trace on screen")
                resume = self.scope.freeze()
                self.log("  using the trace already on the scope"
                         + (" (it was running, so it was stopped first)" if resume
                            else " (it was already stopped)"))
            elif avg_want:
                # The scope is averaging, and :SINGle would take exactly one
                # acquisition of the requested depth - the trap report_averaging
                # warns about after the fact. Accumulate the full average
                # instead, restarting it so the record is entirely this grab's
                # waveform and none of whatever played before.
                resume = True
                wait_s = self.trigger_wait_s()
                self.set_phase(f"building a {avg_want}-deep average")
                hits = self.scope.accumulate(
                    avg_want, wait_s=wait_s, cancelled=self.stop_flag.is_set,
                    progress=lambda sec: self.set_phase(
                        f"building a {avg_want}-deep average - {sec:.0f} s"),
                    source=chans[0])
                if hits is None:
                    self.log("  cancelled while the average was building - "
                             "nothing saved")
                    self.scope.run()
                    return
                if hits == 0:
                    self.log(f"  no trigger within {wait_s:g} s - nothing saved")
                    self.log("    raise 'Wait for trigger', or set it to 0 to "
                             "wait indefinitely")
                    self.scope.run()
                    return
                if hits < avg_want:
                    self.log(f"  ! triggers dried up at {hits} of {avg_want} "
                             f"averages - saving the shallow trace they left")
            else:
                resume = True
                wait_s = self.trigger_wait_s()
                self.set_phase("waiting for trigger" + ("" if wait_s else " (no limit)"))
                triggered = self.scope.single(wait_s=wait_s,
                                              cancelled=self.stop_flag.is_set)
                if triggered is None:
                    self.log("  cancelled while waiting for a trigger - nothing saved")
                    self.scope.run()
                    return
                if triggered is False:
                    self.log(f"  no trigger within {wait_s:g} s - nothing saved")
                    self.log("    raise 'Wait for trigger', or set it to 0 to wait "
                             "indefinitely")
                    self.scope.run()
                    return
            t_armed = time.time() - armed_at
            # Everything that needs the instrument happens first, so the scope
            # goes live again before the slow business of writing files. It is
            # not armed during either phase - see the missed-trigger note below.
            self.set_phase("reading from scope")
            read_at = time.time()
            points = self.transfer_points()
            names = {ch: self.ch_names[ch].get().strip() for ch in chans}
            # A record stopped out of RUN - which is what an averaged build
            # leaves - only answers in the NORMal/MAXimum points modes; RAW gets
            # +109 "No Data For Operation". MAXimum serves everything there is
            # (7680 points on this scope), and behaves as RAW on a record that
            # a :SINGle left behind.
            mode = "MAXimum" if avg_want else "RAW"
            if avg_want:
                # The whole averaged record is 7680 points on this scope;
                # asking for more raises -222 "Data out of range" and a
                # transfer-points limit is beside the point at that size.
                points = None
            cols = {}
            for ch in chans:
                t, v = self.scope.waveform(ch, points_mode=mode, points=points)
                if "time_s" not in cols:
                    cols["time_s"] = t
                cols[self.column_name(ch)] = v

            # One settings read per grab: the panel and the metadata file are
            # built from the same snapshot.
            settings = self.read_all_settings()
            # Asked here rather than in the settings read: the waveform transfer
            # above has just succeeded, so there is certainly a record for the
            # scope to describe.
            hits = self.scope.try_get(WAVE_COUNT)
            if hits is not None:
                settings[WAVE_COUNT] = hits
            self.report_averaging(settings)
            # The screenshot has to be taken before :RUN, while the captured
            # trace is still the one on screen.
            img = self.scope.screenshot() if self.save_png.get() else None
            if resume:
                self.scope.run()
            t_read = time.time() - read_at

            self.set_phase("writing files")
            write_at = time.time()
            data = np.column_stack([cols[k] for k in cols])
            csv_path = base + ".csv"
            np.savetxt(csv_path, data, delimiter=",",
                       header=",".join(cols.keys()), comments="",
                       fmt=[TIME_FMT] + [VOLT_FMT] * (data.shape[1] - 1))
            self.log(f"{os.path.basename(csv_path)}  "
                     f"({data.shape[0]} pts x {data.shape[1]} cols)")

            with open(base + ".txt", "w") as fh:
                fh.write(self.scope.metadata(chans, settings, names, label, existing))
            self.grab_wrote = True

            if img is not None:
                png_path = base + ".png"
                with open(png_path, "wb") as fh:
                    fh.write(img)
                self.log(f"{os.path.basename(png_path)}  ({len(img)} bytes)")
                self.root.after(0, self.refresh_shots)
            t_write = time.time() - write_at

            self.root.after(0, lambda v=settings: self.show_settings(v))
            self.log(f"  {'run ' + label if label else 'grab'}: {t_armed:.1f} s armed, "
                     f"{t_read:.1f} s reading, {t_write:.1f} s writing "
                     f"= {t_armed + t_read + t_write:.1f} s")
            # Only a sequence can silently lose shots to this. On a one-off
            # grab a trigger already waiting is just a running signal.
            #
            # An interval of 0 is the same case: it asks for runs back to back
            # as fast as the readout allows, so a trigger already pending at
            # every re-arm is what was ordered, not a fault. Warning about it
            # once per run only buries the timing line under advice to slow down
            # a sequence that was deliberately set to full speed.
            if (label is not None and t_armed < 0.5 and not existing
                    and self.seq_gap > 0):
                # The scope cannot be armed while it is being read out, so a
                # trigger that is already pending the moment it re-arms means
                # earlier ones came and went unrecorded.
                self.log("  ! a trigger was already waiting when the scope armed:")
                self.log("    triggers are arriving faster than a run takes, so some "
                         "are being missed")
                self.log("    lower 'Transfer points', or leave more than "
                         f"{t_read + t_write:.0f} s between triggers")
            self.root.after(0, self.save_config)
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            self.root.after(0, self.grab_done)

    def report_averaging(self, settings):
        """Averaging is the one setting where what was asked for and what the
        trace actually got can differ without anything on the scope saying so.
        A capture armed with :SINGle takes one acquisition; the average builds up
        over successive triggers, so a grab can come back with a fraction of the
        requested depth and still look like an averaged trace."""
        if not settings.get(":ACQuire:TYPE", "").upper().startswith("AVER"):
            return
        try:
            got = int(float(settings[WAVE_COUNT]))
            want = int(float(settings[":ACQuire:COUNt"]))
        except (KeyError, TypeError, ValueError):
            self.log("  averaging: the scope would not say how many hits are in "
                     "this trace")
            return
        if got < want:
            self.log(f"  ! averaging: {got} of {want} hits in this trace")
            self.log("    the trace is less averaged than the setting says - leave "
                     "the scope running on more triggers to build the average up")
        else:
            self.log(f"  averaging: {got} hits")

    # -- settings panel ---------------------------------------------------

    def build_settings(self, parent, pad):
        self.set_vars = {}        # scpi root -> StringVar shown in the panel
        self.set_widgets = {}     # scpi root -> the widget showing it
        self.set_marks = {}       # scpi root -> "edited" marker label
        self.set_kinds = {}       # scpi root -> num/choice/bool
        self.set_scope = {}       # scpi root -> value the scope last reported
        self.set_live = {}        # scpi root -> state to restore when re-enabled
        self.read_stamp = ""      # when the panel last matched the instrument

        sf = ttk.LabelFrame(parent, text="Scope settings")
        sf.pack(fill="x", **pad)

        # Timebase and trigger side by side, one setting per row: the two groups
        # are the same height, and each label sits next to the value it names
        # rather than sharing a row with an unrelated one.
        cols = ttk.Frame(sf)
        cols.pack(fill="x", padx=6, pady=(2, 2))
        tbf = ttk.LabelFrame(cols, text="Timebase / acquisition")
        tbf.pack(side="left", fill="both", expand=True)
        self.setting_rows(tbf, TIMEBASE_SETTINGS
                          + [(lbl, scpi, "info", None) for lbl, scpi in INFO_SETTINGS],
                          11)
        tgf = ttk.LabelFrame(cols, text="Trigger")
        tgf.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.setting_rows(tgf, TRIGGER_SETTINGS, 11)

        c = ttk.Frame(sf)
        c.pack(fill="x", padx=6, pady=(6, 2))
        for j, (label, _, _, _) in enumerate(CHANNEL_SETTINGS):
            ttk.Label(c, text=label).grid(row=0, column=j + 1, pady=(0, 2))
        for i, ch in enumerate((1, 2, 3, 4)):
            ttk.Label(c, text=f"CH{ch}").grid(row=i + 1, column=0, sticky="e", padx=(0, 4))
            for j, (_, tmpl, kind, choices) in enumerate(CHANNEL_SETTINGS):
                self.setting_widget(c, tmpl.format(ch=ch), kind, choices, i + 1, j + 1, 8)

        bar = ttk.Frame(sf)
        bar.pack(fill="x", padx=6, pady=(2, 2))
        self.read_btn = ttk.Button(bar, text="Read from scope",
                                   command=self.do_read_settings, state="disabled")
        self.read_btn.pack(side="left")
        self.apply_btn = ttk.Button(bar, text="Apply changes",
                                    command=self.do_apply_settings, state="disabled")
        self.apply_btn.pack(side="left", padx=6)
        self.set_status = ttk.Label(bar, text="not read yet", foreground="#666")
        self.set_status.pack(side="left", padx=6)

    def setting_rows(self, parent, items, width):
        """Lay a group out as label/value pairs, one per row."""
        for row, (label, scpi, kind, choices) in enumerate(items):
            ttk.Label(parent, text=label + ":").grid(row=row, column=0, sticky="e",
                                                     padx=(6, 4))
            self.setting_widget(parent, scpi, kind, choices, row, 1, width, pady=0)

    def setting_widget(self, parent, scpi, kind, choices, row, col, width, pady=1):
        cell = ttk.Frame(parent)
        # A checkbox is much narrower than its column heading, so give those
        # cells more room or the headings collide.
        cell.grid(row=row, column=col, sticky="w",
                  padx=(10 if kind == "bool" else 2), pady=pady)
        var = tk.StringVar()
        if kind == "info":
            # read-only: no entry to edit, no edited-marker, never written back
            w = ttk.Label(cell, textvariable=var, width=width)
        elif kind == "num":
            w = ttk.Entry(cell, textvariable=var, width=width)
        elif kind == "bool":
            # A checkbox driving the same ON/OFF string, so the edited-marker,
            # the read-back and the write path all stay as they are. Before the
            # first read the value is "", which Tk shows as neither state.
            w = ttk.Checkbutton(cell, variable=var, onvalue="ON", offvalue="OFF")
        else:
            w = ttk.Combobox(cell, textvariable=var, values=list(choices),
                             width=max(4, width - 3), state="readonly")
        w.pack(side="left")
        if kind != "info":
            mark = ttk.Label(cell, text=" ", width=1, foreground="#c60")
            mark.pack(side="left")
            self.set_marks[scpi] = mark
            var.trace_add("write", lambda *_: self.setting_changed())
        self.set_vars[scpi] = var
        self.set_widgets[scpi] = w
        self.set_kinds[scpi] = kind
        self.set_scope[scpi] = ""
        # A combobox has to go back to "readonly", not "normal", or re-enabling
        # it would leave its text typeable.
        self.set_live[scpi] = "readonly" if kind == "choice" else "normal"

    def edited(self, scpi):
        """True if the panel value differs from what the scope last reported."""
        return self.set_vars[scpi].get().strip() != self.set_scope[scpi]

    def setting_changed(self):
        """Any field changing can move the edited count and, if it is a mode,
        change which other fields the scope is currently paying attention to."""
        self.refresh_marks()
        self.refresh_enabled()

    def setting_live(self, scpi):
        """True when the panel's own mode fields say the scope is acting on this
        one. Judged from the panel rather than the scope because that is the
        state being written: selecting AVERage and a count in the same Apply
        makes the count live, and WRITE_FIRST puts the mode down first."""
        owner, live_for = DEPENDS_ON.get(scpi, (None, None))
        if owner is None or owner not in self.set_vars:
            return True
        return self.set_vars[owner].get().strip().upper().startswith(live_for)

    def refresh_enabled(self):
        """Grey out the fields whose mode is not selected. The scope keeps
        answering with a stale value for those - an average count from the last
        time averaging was on, an edge level under a pulse-width trigger - and
        greying them is what says the number on show is not in force."""
        for scpi in DEPENDS_ON:
            if scpi not in self.set_widgets:
                continue
            self.set_widgets[scpi].configure(
                state=self.set_live[scpi] if self.setting_live(scpi) else "disabled")

    def panel_settings(self):
        """Every writable field the panel is actually asserting: what it holds,
        regardless of whether the scope is believed to already have it.

        Skipped are the info rows, which are results rather than knobs; blanks,
        which are fields never read or filled; and anything greyed out, whose
        displayed value is a stale reply the scope is not acting on and which
        would be written as though it were a real choice."""
        return {scpi: var.get().strip() for scpi, var in self.set_vars.items()
                if self.set_kinds[scpi] != "info" and var.get().strip()
                and self.setting_live(scpi)}

    def refresh_marks(self):
        pending = 0
        for scpi, mark in self.set_marks.items():
            if self.edited(scpi):
                pending += 1
                mark.configure(text="*")
            else:
                mark.configure(text=" ")
        if not self.read_stamp:
            self.set_status.configure(text="not read yet", foreground="#666")
        elif pending:
            self.set_status.configure(
                text=f"{pending} edit(s) not applied - press Apply changes",
                foreground="#c60")
        else:
            self.set_status.configure(text=f"in sync with scope ({self.read_stamp})",
                                      foreground="#060")

    def show_settings(self, values, overwrite=False):
        """Main thread only. Puts scope values in the panel, keeping any edit the
        user has not applied yet - unless overwrite is set, which is the case
        after an Apply, when the scope is the authority on what took effect."""
        kept = 0
        for scpi, raw in values.items():
            if scpi not in self.set_kinds:      # read for the metadata, not shown
                continue
            value = fmt_setting(self.set_kinds[scpi], raw)
            was_edited = self.edited(scpi)
            self.set_scope[scpi] = value
            if overwrite or not was_edited:
                self.set_vars[scpi].set(value)
            elif self.set_vars[scpi].get().strip() != value:
                kept += 1
        self.read_stamp = datetime.datetime.now().strftime("%H:%M:%S")
        if kept:
            self.log(f"  (panel: kept {kept} unapplied edit(s), scope value differs)")
        self.setting_changed()

    def read_all_settings(self):
        """Instrument thread only. Returns the scope's own replies, unrounded, as
        {scpi root: reply} - show_settings formats them for display and
        Scope.metadata writes them verbatim."""
        values = {}
        for scpi in self.set_kinds:
            try:
                values[scpi] = self.scope.get(scpi)
            except Exception as exc:
                self.log(f"  {scpi}? failed: {exc}")
        return values

    def do_read_settings(self):
        if self.busy or not self.scope.inst:
            return
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(None,), daemon=True).start()

    def do_apply_settings(self):
        """Write the fields edited in this window.

        Nothing marked does not mean nothing to do. The marks compare the panel
        against what the scope last *reported*, so after a knob is turned on the
        instrument itself the panel still holds the setting you want and still
        believes the scope has it: nothing is marked, and the one button you
        would reach for does nothing. Rather than a second button for the case,
        an empty Apply asks whether to send the panel as it stands - which is
        almost always why it was pressed with nothing marked."""
        if self.busy or not self.scope.inst:
            return
        changes = {scpi: var.get().strip() for scpi, var in self.set_vars.items()
                   if self.edited(scpi)}
        if not changes:
            self.offer_send_all()
            return
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(changes,), daemon=True).start()

    def offer_send_all(self):
        """Apply found nothing marked. Offer to write the whole panel instead."""
        live = self.panel_settings()
        if not live:
            self.log("No setting changes to apply - the panel has not been read "
                     "or filled in yet.")
            return
        if messagebox.askyesno(
                "Apply changes",
                "There are no apparent changes to be made - every field matches "
                "what the scope last reported.\n\n"
                "If a setting was changed on the scope itself, this window would "
                "not know, and nothing here is marked as edited.\n\n"
                f"Send all {len(live)} settings anyway? This puts the panel back "
                "onto the scope, overwriting anything changed at the front panel.",
                parent=self.root):
            self.do_send_all()
        else:
            self.log("No setting changes to apply.")

    def do_send_all(self):
        """Write every field the panel is asserting, edited here or not.

        Not a button of its own: it is what an empty Apply offers, and what a
        freshly loaded setup offers. Getting the panel back onto a scope whose
        knobs have been turned used to mean Read from scope - which overwrites
        the panel with the state you are trying to leave - then re-typing the
        old value from memory."""
        if self.busy or not self.scope.inst:
            return
        changes = self.panel_settings()
        if not changes:
            self.log("Nothing to send - the panel has not been read or filled in yet.")
            return
        self.log(f"Sending all {len(changes)} panel setting(s) to the scope:")
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(changes,), daemon=True).start()

    def _settings_worker(self, changes):
        try:
            if changes:
                # A mode has to be in place before the fields it governs, or the
                # scope takes the write and quietly does nothing with it. Sorting
                # is stable, so everything else keeps panel order.
                ordered = sorted(changes.items(),
                                 key=lambda kv: WRITE_FIRST.index(kv[0])
                                 if kv[0] in WRITE_FIRST else len(WRITE_FIRST))
                for scpi, value in ordered:
                    if self.set_kinds[scpi] == "num":
                        try:
                            value = f"{float(value):g}"
                        except ValueError:
                            self.log(f"  {scpi} <- '{value}' is not a number, skipped")
                            continue
                    self.scope.put(scpi, value)
                    self.log(f"  {scpi} <- {value}")
                for err in self.scope.errors():
                    self.log(f"  scope rejected something: {err}")
            # Read back either way: after a write the scope is the authority on
            # what it actually accepted, since it clamps values it dislikes.
            values = self.read_all_settings()
            self.root.after(0,
                            lambda v=values: self.show_settings(v, overwrite=bool(changes)))
            if changes:
                # Show what the change did. The display needs a sweep to redraw
                # after something like a timebase change, so give it a moment.
                time.sleep(0.4)
                try:
                    img = self.scope.screenshot()
                    self.root.after(0, lambda d=bytes(img): self.show_peek(d))
                except Exception as exc:
                    self.log(f"  (screenshot after applying failed: {exc})")
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            # Settings I/O must not advance a sequence - only a grab does that.
            self.root.after(0, lambda: self.set_busy(False))

    def do_action(self, scpi, note, confirm=None):
        """Run one of the scope's own buttons - run/stop/single, force trigger,
        clear display, autoscale."""
        if self.busy or not self.scope.inst:
            return
        if confirm and not messagebox.askyesno("Scope Grab", confirm, parent=self.root):
            self.log(f"  {scpi} cancelled")
            return
        self.set_busy(True)
        threading.Thread(target=self._action_worker, args=(scpi, note),
                         daemon=True).start()

    def _action_worker(self, scpi, note):
        try:
            self.scope.command(scpi)
            self.log(f"{scpi} - {note}")
            for err in self.scope.errors():
                self.log(f"  scope rejected it: {err}")
            values = self.read_all_settings()
            # Autoscale is the one that rewrites the settings, so it is the one
            # allowed to overwrite pending edits in the panel; the rest leave an
            # unapplied edit where it is.
            overwrite = scpi == ":AUToscale"
            self.root.after(0, lambda v=values: self.show_settings(v, overwrite=overwrite))
            # Same as after an Apply: show what it did, without saving anything.
            time.sleep(0.4)
            img = self.scope.screenshot()
            self.root.after(0, lambda d=bytes(img): self.show_peek(d))
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            # Like settings I/O, this is not a run and must not advance a sequence.
            self.root.after(0, lambda: self.set_busy(False))

    # -- numbered sequence -------------------------------------------------

    def show_next_name(self):
        """Live preview of the next file name the sequence will write."""
        try:
            start = max(1, int(self.seq_start.get()))
            count = max(1, int(self.seq_count.get()))
        except ValueError:
            self.seq_next.set("next file: (runs and first label must be whole numbers)")
            return
        width = max(3, len(str(start + count - 1)))
        first = self.first_free(start, width)
        name = f"{self.safe_prefix()}_{first:0{width}d}.csv"
        # The First label box stays where it was put, so when a previous run has
        # already taken those labels the preview is the only thing that says the
        # sequence will start further along.
        self.seq_next.set(f"next file: {name}" if first == start else
                          f"next file: {name}  (from {start:0{width}d} up already exist)")

    def do_sequence(self):
        if self.seq_active:
            self.stop_sequence(aborted=True)
            return
        if self.busy or not self.scope.inst:
            return
        try:
            count = int(self.seq_count.get())
            start = int(self.seq_start.get())
            gap = float(self.seq_interval.get())
        except ValueError:
            self.log("Sequence: runs, first label and interval must be numbers.")
            return
        if count < 1 or start < 1:
            self.log("Sequence: runs and first label must be at least 1.")
            return
        chans = self.channels()
        if not chans:
            self.log("Pick at least one channel.")
            return
        if self.use_existing.get():
            # Nothing re-arms, so acquisition memory never changes: every run
            # would write a copy of the same trace.
            self.log("Sequence: untick 'take the trace already on the scope' first")
            self.log("  without a new trigger, every run would save the same trace")
            self.log("  to capture successive triggers, leave it off and set "
                     "'Wait for trigger' to 0")
            return
        if self.auto.get():          # only one repeating mechanism at a time
            self.auto.set(False)
            self.toggle_auto()
            self.log("Sequence: switched auto-grab off.")

        self.seq_width = max(3, len(str(start + count - 1)))
        first = self.first_free(start, self.seq_width)
        if first != start:
            # Naming the labels rather than the whole filename keeps this on one
            # line whatever the prefix is.
            self.log(f"Sequence: {start:0{self.seq_width}d}-{first - 1:0{self.seq_width}d} "
                     f"already exist, starting at {first:0{self.seq_width}d}")
        self.seq_index = first
        self.seq_last = first + count - 1
        self.seq_gap = max(0.0, gap)
        self.seq_done = 0
        self.seq_t0 = time.time()
        self.stop_flag.clear()
        self.seq_active = True
        self.seq_btn.configure(text="Stop sequence")
        self.log(f"Sequence: {count} runs labelled "
                 f"{first:0{self.seq_width}d}-{self.seq_last:0{self.seq_width}d}, "
                 f"{self.seq_gap:g} s apart")
        self.run_sequence_step()

    def first_free(self, start, width):
        """First label whose CSV does not exist yet, so a repeated sequence adds
        to the series instead of overwriting it. This, rather than winding the
        First label box on, is what stacks one sequence on the next."""
        outdir, prefix = self.outdir.get(), self.safe_prefix()
        i = start
        while os.path.exists(os.path.join(outdir, f"{prefix}_{i:0{width}d}.csv")):
            i += 1
        return i

    def run_sequence_step(self):
        self.seq_job = None
        if not self.seq_active:
            return
        label = f"{self.seq_index:0{self.seq_width}d}"
        self.seq_inflight = label
        self.seq_status.configure(
            text=f"run {label} of {self.seq_last:0{self.seq_width}d}", foreground="#060")
        self.seq_started = time.time()
        self.set_busy(True)
        threading.Thread(target=self._grab_worker, args=(self.channels(), label),
                         daemon=True).start()

    def grab_done(self):
        """Every grab ends here, whether one-off or part of a sequence."""
        self.set_busy(False)
        self.phase.configure(text="")
        label, self.seq_inflight = self.seq_inflight, None
        if label is not None and self.grab_wrote:
            self.seq_done += 1        # its files are on disk, so it counts
        if not self.seq_active:
            if label is not None and self.grab_wrote:
                # Stop was pressed while this run was mid-flight; it still saved.
                self.log(f"  (run {label} was already under way and was saved)")
                self.seq_status.configure(text=f"stopped after {self.seq_done}")
            return
        if label is not None and not self.grab_wrote:
            # No trigger, a cancel, or an error: stop rather than burn through
            # the remaining labels writing nothing.
            self.log(f"Sequence stopped at {label}: that run saved no files.")
            self.stop_sequence(aborted=True)
            return
        elapsed = time.time() - self.seq_started
        if self.seq_index >= self.seq_last:
            self.log(f"Sequence finished: {self.seq_done} runs in "
                     f"{time.time() - self.seq_t0:.1f} s")
            self.stop_sequence()
            return
        self.seq_index += 1
        wait = self.seq_gap - elapsed
        if wait <= 0:
            wait = 0.0            # already late; the per-run breakdown says why
        self.seq_job = self.root.after(int(wait * 1000), self.run_sequence_step)

    def stop_sequence(self, aborted=False):
        if aborted:
            self.stop_flag.set()      # break out of a trigger wait in progress
        if self.seq_job is not None:
            self.root.after_cancel(self.seq_job)
            self.seq_job = None
        self.seq_active = False
        self.seq_btn.configure(text="Start sequence")
        self.seq_status.configure(
            text=f"stopped after {self.seq_done}" if aborted else f"done ({self.seq_done} runs)",
            foreground="#c60" if aborted else "#666")
        if aborted:
            self.log(f"Sequence stopped after {self.seq_done} run(s).")
        # A run already in flight finishes and writes its files; grab_done then
        # sees an inactive sequence and stops there.
        self.set_busy(self.busy)
        # First label is left exactly as it was typed. It used to be wound on to
        # the next free number so a second sequence stacked on the first, but
        # that made the box mean two different things - what you asked for until
        # the sequence ended, and where it got to afterwards - and there was no
        # way to run the same labels again without setting it back by hand every
        # time. Stacking still happens: first_free skips labels already on disk.
        # The preview line under the button says where the next one would start.
        self.show_next_name()

    def toggle_auto(self):
        if self.auto.get():
            self.schedule_auto()
        elif self.auto_job is not None:
            self.root.after_cancel(self.auto_job)
            self.auto_job = None

    def schedule_auto(self):
        try:
            ms = max(1000, int(float(self.interval.get()) * 1000))
        except ValueError:
            ms = 10000
        self.do_grab()
        self.auto_job = self.root.after(ms, self.schedule_auto)

    def on_close(self):
        self.stop_flag.set()
        self.stop_sequence()
        self.save_config()
        self.auto.set(False)
        self.toggle_auto()
        self.scope.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
