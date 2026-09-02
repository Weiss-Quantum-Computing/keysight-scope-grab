#!/usr/bin/env python3
"""
Scope Grab - one-click capture from a bench oscilloscope.

Click a button, get a timestamped CSV of the waveform, a PNG of the screen,
and a metadata text file in your chosen folder. No licenses, no BenchVue.

Which scope it is talking to lives in scope_profiles.py, one profile per
instrument family. This file holds everything that does not depend on that:
the window, the capture and sequence logic, and the files that come out.

Requires: a VISA runtime + `pip install pyvisa numpy pillow`
          (Keysight IO Libraries Suite for the MSO-X; see the profile)
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

import scope_profiles

try:
    from PIL import Image, ImageTk        # smooth (Lanczos) preview rescale
except ImportError:                       # without pillow: Tk's integer subsample
    Image = ImageTk = None

# Remembered between sessions: output folder, filename prefix, channel names.
# Kept out of the program folder so a git pull cannot clobber it.
CONFIG_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                           "ScopeGrab", "config.json")
# Named setups, saved and loaded by hand - the counterpart of the AWG GUI's
# awg_setups folder, and on the Desktop for the same reason: a setup is a lab
# record, so it lives where the data does rather than inside the program folder.
# Only the starting point. The folder is picked in the setups window, kept in
# the session config, and this is what a fresh install and a blank box fall
# back to.
SETUP_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "scope_setups")

# CSV number formats. Samples arrive as 8-bit codes - 256 levels - so six
# significant digits already record far more than the scope resolves, and the
# default %.18e was writing (and costing) fifteen digits of noise per sample.
# Time keeps more digits because a long record has to separate adjacent samples.
TIME_FMT = "%.9e"
VOLT_FMT = "%.6e"

# Widgets that already use Space themselves. Space is only the GRAB shortcut
# when the focus is not sitting on one of these - otherwise typing a space in
# the prefix box (or toggling a focused checkbox) would fire an acquisition.
SPACE_OWNERS = {
    "Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox",
    "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton",
    "Button", "TButton",
}

BAD_NAME_CHARS = r'<>:"/\|?*'

# The settings tables, the SCPI roots the capture path asks for by name, and
# the operations that differ from one instrument to the next all live in
# scope_profiles.py - one profile per scope family. The panel is laid out from
# whichever profile is in force, so a second instrument is a second profile
# rather than a second copy of this file.


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


def read_config():
    """The session config as a plain dict, or {} when there is not one to read.

    Called once before the window is built, because the scope model decides how
    the panel is laid out and so has to be known first. load_config does its own
    read afterwards for everything else - it reports what it could not use, and
    this one stays quiet because there is nowhere to report to yet."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def fmt_setting(kind, raw):
    """Normalise a scope reply into what the panel displays."""
    raw = raw.strip()
    if kind == "bool":
        # The scope answers 1/0 for these; the panel writes ON/OFF. Anything
        # else is not a yes - an unrecognised reply reads as OFF rather than
        # ticking a box to say a setting is on when nothing said it was.
        return "ON" if raw.upper() in ("1", "+1", "ON") else "OFF"
    if kind in ("num", "info"):
        try:
            return f"{float(raw):g}"
        except ValueError:
            return raw
    return raw.upper()


# ---------------------------------------------------------------------------
# Instrument layer
# ---------------------------------------------------------------------------

def describe_setup(cfg, prof):
    """The .txt written beside a saved setup: the same numbers, laid out to be
    read in a lab notebook rather than parsed. The .json is the one that gets
    loaded back. `prof` supplies the group order and the labels, so the file
    names what the scope it was saved from actually has."""
    lines = [f"Scope Grab setup - saved {cfg.get('saved', '?')}"]
    if cfg.get("model"):
        lines.append(f"Scope model: {cfg['model']}")
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
    for title, items in prof.setting_groups():
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
        if grab.get("seq_count"):
            lines.append(f"  Sequence          {grab.get('seq_count', '')} runs, "
                         f"{grab.get('seq_interval', '')} s apart, from label "
                         f"{grab.get('seq_start', '')}")
        if grab.get("auto_interval"):
            lines.append(f"  Auto-grab every   {grab.get('auto_interval', '')} s")
        if "save_png" in grab:
            lines.append(f"  Save screenshot   "
                         f"{'yes' if grab.get('save_png') else 'no'}")
        names = grab.get("channel_names") or {}
        ticked = grab.get("channels") or {}
        for ch in prof.channels:
            lines.append(f"  CH{ch} {'on ' if ticked.get(str(ch)) else 'off'}"
                         f"  {names.get(str(ch), '')}".rstrip())
    return "\n".join(lines) + "\n"


class Scope:
    """The instrument, as the panel sees it.

    Everything that differs between scopes is asked of the profile; what is left
    here is the same whichever one is plugged in. Nothing above this class knows
    a SCPI string."""

    def __init__(self, prof):
        self.prof = prof
        self.rm = None
        self.inst = None
        self.idn = ""
        self.addr = ""
        # Traces an accumulate() built here rather than on the scope, waiting
        # for waveform() to collect them, and how many sweeps went into them.
        # Both empty on any instrument whose own averager can be trusted to do
        # the job. See accumulate().
        self.averaged = {}
        self.averaged_depth = None

    def _make_rm(self):
        # A profile may name a VISA implementation to prefer, so a
        # primary/secondary mixup between two of them cannot break us.
        dll = self.prof.visa_dll
        if dll and os.path.exists(dll):
            try:
                rm = pyvisa.ResourceManager(dll)
                rm.list_resources()
                return rm
            except Exception:
                pass
        return pyvisa.ResourceManager()

    def connect(self, addr=None):
        """Find and open the instrument this profile describes.

        A device that answers but is not this profile's is not silently passed
        over: its *IDN? is kept, and if it turns out to be a scope one of the
        other profiles would have handled, the error says so. Having the wrong
        model selected otherwise looks exactly like an unplugged cable."""
        self.close()
        self.rm = self._make_rm()
        if addr:
            candidates = [addr]
        else:
            candidates = [r for r in self.rm.list_resources()
                          if r.startswith(tuple(self.prof.resource_hints))]
        seen = []
        for res in candidates:
            dev = None
            try:
                dev = self.rm.open_resource(res)
                dev.timeout = 5000
                dev.read_termination = "\n"
                dev.write_termination = "\n"
                idn = dev.query("*IDN?").strip()
            except Exception:
                # Close it even when the open half-succeeded and only *IDN?
                # failed. A session left open holds the resource, so every
                # Connect retry past a device that will not answer strands
                # another handle and the scope behind it stays unreachable.
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
                continue
            if self.prof.matches(idn):
                self.prof.open(dev)
                self.inst, self.idn, self.addr = dev, idn, res
                return idn
            seen.append(idn)
            dev.close()
        raise RuntimeError(self._nothing_found(seen))

    def _nothing_found(self, seen):
        """What to say when no instrument matched, including what did answer."""
        msg = (f"No {self.prof.name} found. Check the rear-panel USB-B cable and "
               f"that Connection Expert sees the scope.")
        for idn in seen:
            other = next((p for p in scope_profiles.PROFILES.values()
                          if p is not self.prof and p.matches(idn)), None)
            if other is not None:
                return (f"{msg}\n\nA {other.name} answered instead:\n{idn}\n\n"
                        f"That is a scope this program handles - it is the panel "
                        f"that is set to {self.prof.name}.")
        if seen:
            return f"{msg}\n\nWhat did answer: " + "; ".join(seen)
        return msg

    def close(self):
        for obj in (self.inst, self.rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.inst = self.rm = None
        # Cleared with the session: a saved setup stamps itself with scope.idn,
        # and holding the last instrument's name after it has gone would put it
        # on a setup saved with nothing plugged in.
        self.idn = self.addr = ""

    # -- acquisition ------------------------------------------------------

    def single(self, wait_s=10.0, cancelled=None):
        """Arm a single acquisition and wait for it to complete.

        Arms rather than digitizes, so the captured trace stays on the scope
        display - which matters if you also want the screenshot to match the
        data.

        wait_s <= 0 waits indefinitely, which is how a capture is primed before
        an experiment running elsewhere starts sending triggers. `cancelled` is
        polled so a long wait can be called off from the panel.

        Returns True if it triggered, False on timeout, None if cancelled, and
        raises if the scope stops answering the poll.
        """
        self.averaged.clear()
        self.averaged_depth = None
        # Arming is not free of side effects everywhere: on a Rigol :SINGle is
        # a :TRIGger:SWEep write and leaves the sweep changed. The profile gets
        # to save whatever it has to, and gets it back in the finally below.
        state = self.prof.before_single(self)
        try:
            return self._single(wait_s, cancelled)
        finally:
            self.prof.after_single(self, state)

    def _single(self, wait_s, cancelled):
        self.inst.write(self.prof.cmd_single)
        started = time.time()
        deadline = None if wait_s <= 0 else started + wait_s
        bad_polls = 0
        while deadline is None or time.time() < deadline:
            if cancelled is not None and cancelled():
                self.inst.write(self.prof.cmd_stop)
                return None
            try:
                running = self.prof.running(self)
                bad_polls = 0
            except Exception:
                # A poll that failed is not a trigger that arrived. Give the
                # link a couple more tries the way accumulate does, and if it
                # still will not answer, let the error out: reporting this as a
                # trigger would have the caller read out whatever happens to be
                # in acquisition memory - a trace from before, most likely - and
                # write it as a fresh capture with nothing saying otherwise.
                bad_polls += 1
                if bad_polls < 3:
                    time.sleep(0.25)
                    continue
                try:
                    self.inst.write(self.prof.cmd_stop)
                except Exception:
                    pass
                raise
            if not running:
                return True
            # Poll hard at first for a quick handoff, then back off: a wait of
            # minutes should not hammer the USB link 20 times a second.
            time.sleep(0.05 if time.time() - started < 2.0 else 0.25)
        self.inst.write(self.prof.cmd_stop)
        return False

    def accumulate(self, count, wait_s=10.0, cancelled=None, progress=None,
                   channels=(1,)):
        """Acquire a true `count`-deep average and report how deep it got.

        How that is done is the profile's business. What the averager does under
        RUN, whether anything resets it, which command builds a block that can
        be counted afterwards and whether the resulting count can be trusted are
        all per-instrument, and were established by measurement rather than from
        a programming guide - the MSO-X notes are on KeysightInfiniiVision.

        wait_s is a STALL limit on the trigger: it restarts on every trigger
        event, so a deep average is allowed its many periods while a dead
        trigger is caught within one wait. <= 0 never gives up. `progress` is
        called with seconds elapsed; the full build takes count trigger
        periods (12.8 s for 256 at 20 Hz).

        Returns `count` on success, fewer if the triggers dried up, 0 if none
        ever came, -1 if a record was built but the scope would not say how deep
        it is, None if cancelled. In every case but None the scope is left
        holding a stopped record that the transfer which follows can read.

        `channels` is every channel this capture wants, not just one. An
        instrument whose own averager cannot be trusted to count - see the
        DS1000Z profile - has to build the average here instead, and it must
        read every channel out of the same sweeps or they stop being
        simultaneous. Such a profile leaves its results in scope.averaged for
        waveform() to collect.
        """
        self.averaged.clear()
        self.averaged_depth = None
        return self.prof.accumulate(self, count, wait_s, cancelled, progress,
                                    tuple(channels))

    def is_running(self):
        """Whether an acquisition is in progress, with an unanswerable scope
        counted as stopped. Only for deciding whether to put the run state back
        afterwards - the paths where the difference between 'not running' and
        'would not say' matters call the profile directly, and let it raise."""
        try:
            return self.prof.running(self)
        except Exception:
            return False

    def freeze(self):
        """Use what the scope has already captured instead of arming a new
        acquisition. Stopping first matters: reading memory while the scope is
        still acquiring returns a record torn between two acquisitions. Returns
        whether it had been running, so its state can be put back."""
        self.averaged.clear()
        self.averaged_depth = None
        was_running = self.is_running()
        self.inst.write(self.prof.cmd_stop)
        return was_running

    def waveform(self, channel, points_mode="RAW", points=None):
        return self.prof.read_waveform(self, channel, points_mode, points)

    def transfer_plan(self, averaged, points):
        """The points mode to read a record in, and the point count to ask for.
        Which modes will serve a record depends on how it was stopped, which is
        per-instrument."""
        return self.prof.transfer_plan(averaged, points)

    def screenshot(self):
        return self.prof.screenshot(self)

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
        instead of silently ignored.

        The one SCPI string left in this file. It is mandated by SCPI itself
        rather than chosen by a maker, so it does not belong to a profile the
        way the rest do."""
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

    # -- what the capture path asks by name -------------------------------

    def averaging(self, acq_type):
        """Whether an acquisition-type reply says the scope is averaging."""
        return acq_type.strip().upper().startswith(self.prof.avg_prefix)

    def averaging_depth(self):
        """How deep an average the scope is set to build, or None for a plain
        grab. Asked of the instrument rather than the panel: the panel's copy is
        whatever was last read, and the front panel may have moved since."""
        try:
            if not self.averaging(self.get(self.prof.acq_type)):
                return None
            n = int(float(self.get(self.prof.acq_count)))
            return n if n > 1 else None
        except Exception:
            return None

    def hit_count(self):
        """How many hits are in the record being read out, or None. Asked only
        where a record is known to exist - some instruments answer an error
        rather than a number when acquisition memory is empty."""
        return self.prof.hit_count(self)

    def is_displayed(self, ch):
        """Whether the scope is showing a channel - one it is not has no record
        to hand over. None when it would not say, which is not the same as a no.
        """
        try:
            return self.get(self.prof.ch_display.format(ch=ch)) not in ("0", "OFF")
        except Exception:
            return None

    def metadata(self, channels, settings, names=None, label=None, existing=False):
        """Format the metadata file. `settings` is the raw {scpi root: reply}
        snapshot already read for the panel, so a grab only asks the scope once
        and the file describes the same instant the panel shows. Values are the
        instrument's own strings, unrounded.

        The rows come from the profile, so the file names what the scope in
        front of you actually has rather than a fixed list that would be part
        wrong on anything else."""
        prof = self.prof
        s = lambda scpi: settings.get(scpi, "?")
        row = lambda lbl, val: f"{lbl:<19}: {val}"
        chrow = lambda lbl, val: f"{lbl:<18}: {val}"
        # An average count is only in force in averaging mode, and the scope
        # keeps reporting the last one whatever the mode - so say when it is
        # idle, rather than leave a file that reads as averaged when it was not.
        averaging = self.averaging(s(prof.acq_type))
        avg_note = ("" if averaging else
                    f"   (not in use: acquisition type is not {prof.avg_name})")
        lines = [
            row("captured", datetime.datetime.now().isoformat()),
            row("instrument", self.idn),
            row("visa address", self.addr),
        ] + ([row("sequence label", label)] if label else []) + (
            [row("capture mode",
                 "existing trace on the scope, not a new trigger")]
            if existing else [])
        lines += [row(lbl, s(scpi)) for lbl, scpi in prof.meta_head]
        lines.append(row("averages", f"{s(prof.acq_count)}{avg_note}"))
        if averaging and prof.wave_count in settings:
            lines.append(row("averages taken",
                             f"{s(prof.wave_count)} of {s(prof.acq_count)}"
                             f"   (hits actually in the trace that was read out)"))
        lines += [row(lbl, s(scpi)) for lbl, scpi in prof.meta_tail]
        for ch in channels:
            if names and names.get(ch):
                lines.append(chrow(f"CH{ch} name", names[ch]))
            lines += [chrow(f"CH{ch} {lbl}", s(scpi.format(ch=ch)))
                      for lbl, scpi in prof.meta_channel]
        return "\n".join(lines) + "\n"

    def run(self):
        try:
            self.inst.write(self.prof.cmd_run)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.msgs = queue.Queue()
        # The profile has to be settled before anything else is built: the
        # settings panel, the metadata layout and the screenshot box are all
        # laid out from it. Remembered between sessions like the rest of the
        # config, and read here directly rather than in load_config, which runs
        # at the end of this method - long after the panel exists.
        saved = read_config().get("model")
        self.prof = scope_profiles.get_profile(saved)
        if saved and saved != self.prof.key:
            self.log(f"Config asks for a scope this version does not have "
                     f"({saved}) - starting on {self.prof.name}.")
        self.scope = Scope(self.prof)
        self.preview_w, self.preview_h = self.prof.preview_size
        self.busy = False
        self.auto_job = None
        self.prefix_job = None    # debounce for re-pointing the shot browser
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
        # Where setups are kept. A folder of its own rather than the capture
        # folder: that one moves with the experiment, while setups accumulate
        # in one place and are looked for there.
        self.setup_dir = tk.StringVar(value=SETUP_DIR)
        # Setup files are named independently of the captures. The two were one
        # box, which meant renaming a run renamed the setups too and a setup
        # saved under one experiment's prefix looked like it belonged to it.
        self.setup_prefix = tk.StringVar(value="setup")

        root.title(f"Scope Grab - {self.prof.name}")
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
        for i, ch in enumerate(self.prof.channels):
            v = tk.BooleanVar(value=(ch == self.prof.channels[0]))
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
        for text, scpi, note, confirm, rewrites in self.prof.actions:
            btn = ttk.Button(cf, text=text, width=max(6, len(text) + 1),
                             state="disabled",
                             command=lambda s=scpi, n=note, k=confirm, w=rewrites:
                             self.do_action(s, n, k, w))
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
        box = tk.Frame(self.shot_frame, width=self.preview_w,
                       height=self.preview_h)
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
        # Added here rather than beside the other prefix trace, because it drives
        # the preview widgets and they are only built further up this method.
        self.prefix.trace_add("write", lambda *_: self.prefix_changed())
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

    def pick_setup_dir(self):
        d = filedialog.askdirectory(
            title="Folder for saved setups",
            initialdir=self.setup_dir.get() or SETUP_DIR,
            parent=self.setup_win or self.root)
        if d:
            self.setup_dir.set(d)
            self.save_config()

    def current_cfg(self):
        return {
            "model": self.prof.key,
            "outdir": self.outdir.get(),
            "setup_dir": self.setup_dir.get(),
            "setup_prefix": self.setup_prefix.get(),
            "prefix": self.prefix.get(),
            "channel_names": {str(ch): var.get() for ch, var in self.ch_names.items()},
            "channels": {str(ch): var.get() for ch, var in self.ch_vars.items()},
            "trigger_wait": self.trig_wait.get(),
            "transfer_points": self.trans_pts.get(),
            "seq_count": self.seq_count.get(),
            "seq_interval": self.seq_interval.get(),
            "seq_start": self.seq_start.get(),
            "auto_interval": self.interval.get(),
            "save_png": self.save_png.get(),
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

        # App-level, so restored here rather than in load_grab_prefs: a setup
        # file must not be able to redirect or rename the setups themselves.
        for key, var in (("outdir", self.outdir), ("setup_dir", self.setup_dir),
                         ("setup_prefix", self.setup_prefix)):
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                var.set(value)
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
                         ("transfer_points", self.trans_pts),
                         ("seq_count", self.seq_count),
                         ("seq_interval", self.seq_interval),
                         ("seq_start", self.seq_start),
                         ("auto_interval", self.interval)):
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                var.set(value)
        # Auto-grab's interval comes back but auto-grab itself does not: a tick
        # that survived a restart would have the app capturing before anyone had
        # looked at what the scope was set to. Same reasoning as 'take the trace
        # already on the scope', which is also always off at launch.
        save_png = cfg.get("save_png")
        if isinstance(save_png, (bool, int)):
            self.save_png.set(bool(save_png))
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
        ttk.Entry(ff, textvariable=self.setup_dir, width=52).pack(
            side="left", fill="x", expand=True)
        ttk.Button(ff, text="...", width=3,
                   command=self.pick_setup_dir).pack(side="left", padx=6)

        pf = ttk.Frame(win)
        pf.pack(fill="x", padx=8, pady=2)
        ttk.Label(pf, text="Prefix:").pack(side="left")
        ttk.Entry(pf, textvariable=self.setup_prefix, width=16).pack(
            side="left", padx=4)
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
            # Which scope this is for, as opposed to which one was plugged in
            # when it was saved. The settings are that model's SCPI roots, so a
            # load onto another one is refused rather than half-applied.
            "model": self.prof.key,
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
                "seq_count": self.seq_count.get(),
                "seq_interval": self.seq_interval.get(),
                "seq_start": self.seq_start.get(),
                "auto_interval": self.interval.get(),
                "save_png": self.save_png.get(),
            },
        }
        outdir = self.setup_dir.get().strip() or SETUP_DIR
        try:
            os.makedirs(outdir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.join(outdir, f"{self.safe_setup_prefix()}_{stamp}")
            with open(base + ".json", "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            with open(base + ".txt", "w", encoding="utf-8") as fh:
                fh.write(describe_setup(cfg, self.prof))
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
        start = self.setup_dir.get().strip() or SETUP_DIR
        path = filedialog.askopenfilename(
            title="Load setup", initialdir=start if os.path.isdir(start) else ".",
            filetypes=[("Setup files", "*.json"), ("All files", "*.*")],
            parent=self.setup_win or self.root)
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

        # A setup is a set of SCPI roots, so one saved from another scope would
        # land as a panel full of fields this one does not have - counted as
        # loaded, marked as edits, and unsendable. Refuse it instead. Setups
        # written before profiles existed carry no model and are all MSO-X, so
        # a missing key loads as it always did.
        model = cfg.get("model")
        if model and model != self.prof.key:
            other = scope_profiles.PROFILES.get(model)
            named = other.name if other is not None else model
            messagebox.showerror(
                "Setup is for another scope",
                f"That setup was saved for {named}, and this panel is set up "
                f"for {self.prof.name}.\n\nIts settings name commands that "
                "scope has and this one may not, so loading it would fill the "
                "panel with values that cannot be sent.",
                parent=self.root)
            self.log(f"Not loading {os.path.basename(path)}: it was saved for "
                     f"{named}, and this panel is {self.prof.name}")
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
        # Loading is the last thing this window is for, and the panel behind it
        # is now the picture of what was loaded - marks and all. Leaving it up
        # only puts the send-it-now question over the thing being asked about.
        # A failed read keeps the window, since the next move is another file.
        self._setups_close()
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

    def safe_setup_prefix(self):
        p = "".join("_" if c in BAD_NAME_CHARS else c
                    for c in self.setup_prefix.get()).strip()
        return p or "setup"

    def render_preview(self, source):
        """Put a PNG in the preview box: `source` is a file path, or PNG bytes for
        a screenshot that was never written to disk. Main thread only, since Tk
        images are not thread safe."""
        try:
            if Image is not None:
                im = Image.open(source if isinstance(source, str)
                                else io.BytesIO(source))
                im.load()
                k = min(self.preview_w / im.width,
                        self.preview_h / im.height, 1.0)
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
                while (img.width() // k > self.preview_w
                       or img.height() // k > self.preview_h):
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

    def prefix_changed(self):
        """Re-point the screenshot browser at the new prefix, once the typing has
        stopped. It browses the files of one prefix, so renaming the run - typed
        in the box or restored by a loaded setup - otherwise leaves it showing
        the previous one's pictures until the next capture lands. Debounced: this
        fires on every keystroke, and a refresh lists the folder and decodes and
        rescales a PNG."""
        if self.prefix_job is not None:
            self.root.after_cancel(self.prefix_job)
        self.prefix_job = self.root.after(400, self._prefix_settled)

    def _prefix_settled(self):
        self.prefix_job = None
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
        if self.busy or self.seq_active or not self.scope.inst:
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
        # seq_active as well as busy: between a sequence's runs nothing is busy,
        # but the next run is already scheduled. GRAB is greyed out then and
        # Space is not - it calls this directly, bypassing the button - so
        # without the guard a space bar puts a second capture thread on the same
        # VISA session as the run that is about to start, and the two of them
        # interleave their SCPI and fight over seq_inflight and grab_wrote.
        if self.busy or self.seq_active or not self.scope.inst:
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
            # Never write over a capture that is already there. A one-off's
            # timestamp only resolves to a second, so two grabs inside the same
            # second would collide; a sequence's labels are checked ahead of the
            # run by first_free(), but the folder and the prefix can both be
            # changed while one is going, so both paths come through here.
            wanted, base = base, free_base(base)
            if base != wanted:
                self.log(f"  {os.path.basename(wanted)}.csv is already there - "
                         f"writing {os.path.basename(base)}.csv instead")

            existing = self.use_existing.get()
            # A channel the scope is not displaying has no record to hand over:
            # :WAVeform:DATA? answers +109 "No Data For Operation" and the read
            # then waits out the whole VISA timeout before failing with nothing
            # in it that names the channel. Asked of the instrument rather than
            # the panel, so a channel switched on at the front panel since the
            # last read does not get a grab refused over a stale copy.
            dark = [ch for ch in chans if self.scope.is_displayed(ch) is False]
            if dark:
                one = len(dark) == 1
                self.log(f"  {', '.join(f'CH{ch}' for ch in dark)} "
                         f"{'is' if one else 'are'} switched off on the scope, so "
                         f"there is nothing to read from {'it' if one else 'them'}"
                         f" - nothing saved")
                self.log("    turn the channel on under Display in the settings "
                         "panel, or untick it under Channels")
                return
            # Asked even for a use-existing grab: the read further down needs to
            # know whether it is looking at an averaged record, because which
            # points modes will serve one is not the same as for a plain trace.
            avg_want = self.scope.averaging_depth()
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
                # The scope is averaging, and arming a single acquisition can
                # take exactly one hit while claiming the full depth - the trap
                # report_averaging warns about after the fact. Accumulate the
                # full average instead, restarting it so the record is entirely
                # this grab's waveform and none of whatever played before.
                resume = True
                wait_s = self.trigger_wait_s()
                self.set_phase(f"building a {avg_want}-deep average")
                hits = self.scope.accumulate(
                    avg_want, wait_s=wait_s, cancelled=self.stop_flag.is_set,
                    progress=lambda sec: self.set_phase(
                        f"building a {avg_want}-deep average - {sec:.0f} s"),
                    channels=chans)
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
                if hits < 0:
                    self.log("  ! the scope would not say how deep the average "
                             "got - saving the trace it built anyway")
                elif hits < avg_want:
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
            # Which points mode will serve the record, and whether a
            # transfer limit still means anything at the size an averaged one
            # comes in at, are both per-instrument - see transfer_plan on the
            # profile for why this scope answers the way it does.
            mode, points = self.scope.transfer_plan(bool(avg_want), points)
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
            hits = self.scope.hit_count()
            if hits is not None:
                settings[self.prof.wave_count] = hits
            self.report_averaging(settings, existing=existing)
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

            # utf-8 explicitly: the file records channel names exactly as typed,
            # and the machine default here is cp1252, which cannot encode half
            # of what a name in this lab has in it. Failing on one would abort
            # the grab with the CSV already written and the run reported as
            # having saved nothing.
            with open(base + ".txt", "w", encoding="utf-8") as fh:
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

    def report_averaging(self, settings, existing=False):
        """Record how deep the average in this trace actually is.

        Averaging is the one setting where what was asked for and what the trace
        got can differ with nothing on the scope saying so, and which of the two
        the count describes depends on where the trace came from.

        A trace this grab built is the honest case: Scope.accumulate counts the
        triggers out and the count reads true afterwards, and a build that fell
        short has already been reported by the caller - so there is nothing to
        warn about here and the depth is simply recorded.

        A trace that was already on the scope is the other one. On the MSO-X the
        averager is a running average under RUN, and the hit count reports the
        SETTING rather than the accumulated depth, so neither a short count nor
        a full one describes what is in the record.

        The warning below states that as fact. It was measured on the MSO-X and
        is exactly the sort of thing a second scope may do differently, so it
        moves onto the profile once there is one to compare against."""
        if not self.scope.averaging(settings.get(self.prof.acq_type, "")):
            return
        try:
            got = int(float(settings[self.prof.wave_count]))
            want = int(float(settings[self.prof.acq_count]))
        except (KeyError, TypeError, ValueError):
            self.log("  averaging: the scope would not say how many hits are in "
                     "this trace")
            return
        if not existing:
            self.log(f"  averaging: {got} hits" if got >= want
                     else f"  averaging: {got} of {want} hits in this trace")
            return
        self.log(f"  ! averaging: the scope reports {got} of {want} hits")
        self.log("    this trace was not built by this grab, so that count is "
                 "not to be trusted: if the scope was running, it is the "
                 "setting rather than the depth, and the record carries an "
                 "exponential average of whatever played before")
        self.log("    untick 'take the trace already on the scope' to have the "
                 "average built and counted out instead")

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
        self.setting_rows(tbf, list(self.prof.timebase)
                          + [(lbl, scpi, "info", None) for lbl, scpi in self.prof.info],
                          11)
        tgf = ttk.LabelFrame(cols, text="Trigger")
        tgf.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.setting_rows(tgf, self.prof.trigger, 11)

        c = ttk.Frame(sf)
        c.pack(fill="x", padx=6, pady=(6, 2))
        for j, (label, _, _, _) in enumerate(self.prof.channel):
            ttk.Label(c, text=label).grid(row=0, column=j + 1, pady=(0, 2))
        for i, ch in enumerate(self.prof.channels):
            ttk.Label(c, text=f"CH{ch}").grid(row=i + 1, column=0, sticky="e", padx=(0, 4))
            for j, (_, tmpl, kind, choices) in enumerate(self.prof.channel):
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
        makes the count live, and the profile's write_first puts the mode down
        first."""
        owner, live_for = self.prof.depends_on.get(scpi, (None, None))
        if owner is None or owner not in self.set_vars:
            return True
        return self.set_vars[owner].get().strip().upper().startswith(live_for)

    def refresh_enabled(self):
        """Grey out the fields whose mode is not selected. The scope keeps
        answering with a stale value for those - an average count from the last
        time averaging was on, an edge level under a pulse-width trigger - and
        greying them is what says the number on show is not in force."""
        for scpi in self.prof.depends_on:
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
        """Pull the scope's settings into the panel.

        A read used to be unable to clear an unapplied edit - it landed in every
        other field and left yours alone. That is right while you are part-way
        through typing a change, and wrong when the edit is stale and what you
        want is the instrument's own state, which is the usual reason for
        pressing this. So it asks, rather than picking one for you."""
        if self.busy or self.seq_active or not self.scope.inst:
            return
        pending = [scpi for scpi in self.set_marks if self.edited(scpi)]
        overwrite = False
        if pending:
            overwrite = messagebox.askyesno(
                "Read from scope",
                f"{len(pending)} field(s) hold edits that have not been applied.\n\n"
                "Overwrite them with what the scope reports?\n\n"
                "Yes - the panel becomes a straight reading of the instrument and "
                "those edits are gone.\n"
                "No - the reading fills every other field and the edits stay put.",
                parent=self.root)
            if not overwrite:
                self.log(f"Read from scope: keeping {len(pending)} unapplied edit(s).")
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(None, overwrite),
                         daemon=True).start()

    def do_apply_settings(self):
        """Write the fields edited in this window.

        Nothing marked does not mean nothing to do. The marks compare the panel
        against what the scope last *reported*, so after a knob is turned on the
        instrument itself the panel still holds the setting you want and still
        believes the scope has it: nothing is marked, and the one button you
        would reach for does nothing. Rather than a second button for the case,
        an empty Apply asks whether to send the panel as it stands - which is
        almost always why it was pressed with nothing marked."""
        if self.busy or self.seq_active or not self.scope.inst:
            return
        # Info rows are excluded the way panel_settings() and a saved setup
        # exclude them: they are results rather than knobs, and writing one back
        # would be a command at a read-only node.
        changes = {scpi: var.get().strip() for scpi, var in self.set_vars.items()
                   if self.set_kinds[scpi] != "info" and self.edited(scpi)}
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
        if self.busy or self.seq_active or not self.scope.inst:
            return
        changes = self.panel_settings()
        if not changes:
            self.log("Nothing to send - the panel has not been read or filled in yet.")
            return
        self.log(f"Sending all {len(changes)} panel setting(s) to the scope:")
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(changes,), daemon=True).start()

    def _settings_worker(self, changes, overwrite=False):
        try:
            if changes:
                # A mode has to be in place before the fields it governs, or the
                # scope takes the write and quietly does nothing with it. Sorting
                # is stable, so everything else keeps panel order.
                first = self.prof.write_first
                ordered = sorted(changes.items(),
                                 key=lambda kv: first.index(kv[0])
                                 if kv[0] in first else len(first))
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
            # After a write the scope is the authority on what it accepted, so
            # that always overwrites. A plain read only does when asked to.
            wins = overwrite or bool(changes)
            self.root.after(0,
                            lambda v=values: self.show_settings(v, overwrite=wins))
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

    def do_action(self, scpi, note, confirm=None, rewrites=False):
        """Run one of the scope's own buttons - run/stop/single, force trigger,
        clear display, autoscale. `rewrites` says the command changes the
        settings rather than just the run state, which decides whether the
        re-read afterwards may overwrite an unapplied edit."""
        if self.busy or self.seq_active or not self.scope.inst:
            return
        if confirm and not messagebox.askyesno("Scope Grab", confirm, parent=self.root):
            self.log(f"  {scpi} cancelled")
            return
        self.set_busy(True)
        threading.Thread(target=self._action_worker, args=(scpi, note, rewrites),
                         daemon=True).start()

    def _action_worker(self, scpi, note, rewrites=False):
        try:
            self.scope.command(scpi)
            self.log(f"{scpi} - {note}")
            for err in self.scope.errors():
                self.log(f"  scope rejected it: {err}")
            values = self.read_all_settings()
            # Autoscale and its like rewrite the settings, so they are the ones
            # allowed to overwrite pending edits in the panel; the rest leave an
            # unapplied edit where it is.
            self.root.after(0, lambda v=values: self.show_settings(v, overwrite=rewrites))
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
        first = self.first_free(start, width, count)
        name = f"{self.safe_prefix()}_{first:0{width}d}.csv"
        # The First label box stays where it was put, so when a previous run has
        # already taken those labels the preview is the only thing that says the
        # sequence will start further along.
        self.seq_next.set(f"next file: {name}" if first == start else
                          f"next file: {name}  ({count} runs from "
                          f"{start:0{width}d} would land on files already there)")

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
        first = self.first_free(start, self.seq_width, count)
        if first != start:
            # Naming the labels rather than the whole filename keeps this on one
            # line whatever the prefix is.
            self.log(f"Sequence: {count} runs from {start:0{self.seq_width}d} would "
                     f"land on files already there - starting at "
                     f"{first:0{self.seq_width}d}, the first clear stretch")
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

    def first_free(self, start, width, count=1):
        """First label from which `count` consecutive CSVs are all free, so a
        repeated sequence adds to the series instead of overwriting it. This,
        rather than winding the First label box on, is what stacks one sequence
        on the next.

        The whole run has to be clear, not just its first label. Stopping at the
        first gap is what this used to do, and a series with a hole in it - one
        bad run deleted - then started in the hole and wrote straight over
        everything after it."""
        outdir, prefix = self.outdir.get(), self.safe_prefix()
        taken = lambda i: os.path.exists(
            os.path.join(outdir, f"{prefix}_{i:0{width}d}.csv"))
        i = start
        while True:
            clash = next((j for j in range(i, i + max(1, count)) if taken(j)), None)
            if clash is None:
                return i
            i = clash + 1

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
        if self.auto.get() and self.seq_active:
            # Two repeating mechanisms at once would have both of them firing
            # captures at one instrument. do_sequence switches auto off for the
            # same reason when a sequence starts on top of it; this is the other
            # order round. The checkbox is not one set_busy greys out, because
            # it has to stay usable for switching auto-grab off mid-run.
            self.auto.set(False)
            self.log("Auto-grab: a sequence is running - stop that first.")
            return
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
        # A tick that cannot fire says so. do_grab returns silently when it is
        # not in a position to run, so an interval shorter than a capture takes
        # was dropping most of the ticks with nothing but the gaps in the series
        # to show for it.
        if not self.scope.inst:
            self.log("Auto-grab: not connected, so this tick captured nothing.")
        elif self.busy or self.seq_active:
            self.log(f"Auto-grab: the last grab is still going, so this tick is "
                     f"skipped - it needs more than {ms / 1000:g} s.")
        else:
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
