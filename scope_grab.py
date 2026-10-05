#!/usr/bin/env python3
"""
Scope Grab - one-click capture from a bench oscilloscope.

Click a button, get a timestamped data file of the waveform (compact NPZ or
CSV), a PNG of the screen, and a metadata text file in your chosen folder. No
licenses, no BenchVue.

Which scope it is talking to lives in scope_profiles.py, one profile per
instrument family. This file holds everything that does not depend on that:
the window, the capture and sequence logic, and the files that come out.

Requires: a VISA runtime + `pip install pyvisa numpy pillow`
          (Keysight IO Libraries Suite for the MSO-X; see the profile)
          (pillow only sharpens the screenshot preview - the rest works without it)
Run with:  pythonw scope_grab.py      (pythonw = no console window)
"""

import base64
import csv
import datetime
import io
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pyvisa

# scope_profiles.py sits beside this file, but a plain import is not enough to
# find it. EOM-ILC loads this module by path - ilc_bench.load_module does a
# spec_from_file_location and exec_module - and that does NOT put the containing
# directory on sys.path, so `import scope_profiles` there raises
# ModuleNotFoundError and the whole bench panel fails to start. Splitting this
# file in two is what introduced that; putting our own directory on the path is
# what pays for it. tests/test_path_import.py is the regression test.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import scope_profiles  # noqa: E402

try:
    from PIL import Image, ImageTk        # smooth (Lanczos) preview rescale
except ImportError:                       # without pillow: Tk's integer subsample
    Image = ImageTk = None

# The plot tabs. Capture works without matplotlib - the tabs then say what to
# install - and the ledgers (Statistics, Measurements) never needed it.
try:
    import matplotlib
    import matplotlib.colors as mcolors
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                                   NavigationToolbar2Tk)
except ImportError:
    matplotlib = mcolors = Figure = FigureCanvasTkAgg = NavigationToolbar2Tk = None
NO_MPL = ("matplotlib is not installed, so this tab cannot draw.\n"
          "pip install matplotlib   - the Statistics and Measurements tabs work "
          "without it.")

# Plot colours, the ILC panel's scheme: the current prefix's runs ride a viridis
# ramp, oldest dark to newest yellow, and each compare key takes one of these -
# hues that sit far from that ramp - with its older runs blended toward white.
# Compare traces draw under the current prefix's (CMP_ZORDER: below a line's
# default 2, above the grid's 1.5).
CMP_COLOURS = ["#ff7f0e", "#e377c2", "#8c564b", "#d62728", "#17becf", "#7f7f7f"]
CMP_ZORDER = 1.8
# Traces per pane past which the legend is left off - a 64-run sequence is a
# ramp of colour, not a list of names.
LEGEND_MAX = 12
# Points per trace past which a blank 'Draw 1 in' box starts thinning. A pane is
# some 600 pixels wide and a column of them can show a trace's lowest and
# highest sample and nothing between, so this is exact unzoomed and to about an
# 8x zoom. Measured in the window on synthetic ramps: ten 62500-point runs on
# four channels redraw Waveforms in 1.4 s instead of 4.8, three 1 Mpt runs in
# 0.7 s instead of 5.4.
PLOT_AUTO_PTS = 10000


def _cosine_window(n, a):
    """Generalised cosine window: sum_i (-1)^i a_i cos(2 pi i k/(N-1))."""
    k = np.arange(n) / max(n - 1, 1)
    return sum(((-1) ** i) * ai * np.cos(2 * np.pi * i * k)
               for i, ai in enumerate(a))


# FFT windows for the Spectrum tab, by name. The usual trade between side-lobe
# suppression and main-lobe width: hann for looking; the 4-term Blackman-Harris
# (-92 dB) for a weak line beside a strong one; flat-top for the height of a line
# rather than its position; rectangular for a record that already ends where it
# started, which is the only case it does not smear.
WINDOWS = {
    "hann": lambda n: _cosine_window(n, (0.5, 0.5)),
    "blackman-harris": lambda n: _cosine_window(
        n, (0.35875, 0.48829, 0.14128, 0.01168)),
    "flat-top": lambda n: _cosine_window(
        n, (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368)),
    "rectangular": np.ones,
}
SPEC_UNITS = {"V rms": "rms", "V/sqrt(Hz)": "asd"}
# The measurements the scope's own Snapshot All lists, in its order, with the
# unit each is shown in. Computed from the samples by measure().
MEAS_COLUMNS = [
    ("Vpp", "V"), ("Vmax", "V"), ("Vmin", "V"), ("Vtop", "V"), ("Vbase", "V"),
    ("Vamp", "V"), ("Vavg", "V"), ("Vrms", "V"), ("Vrms AC", "V"),
    ("Freq", "Hz"), ("Period", "s"), ("+Width", "s"), ("-Width", "s"),
    ("Duty", "%"), ("Rise", "s"), ("Fall", "s"),
    ("Overshoot", "%"), ("Preshoot", "%"),
    ("X@max", "s"), ("X@min", "s"), ("Area", "Vs"),
]
# What the scope answers for a measurement it could not make.
NOT_MEASURED = 9.9e37
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
# One ADC code as a fraction of the V/div setting, for the offset dither below.
# Why the offset dither exists: an 8-bit converter carries a fixed error
# pattern per code -- measured 2 Sep 2026 on the Trek monitor, ~3.4 mV pk-pk
# over a 40.25 mV code at 1 V/div, repeating exactly every code of input. A slow
# ramp sweeps through it at slope/code, which lands in the tens of kHz, and
# because it is a function of VOLTAGE an average of identical shots keeps it
# whole. Stepping the offset by whole codes across a sequence moves the pattern
# and averages it away instead. What a code is worth differs from scope to
# scope, so the number lives on the profile as adc_code_per_vdiv.

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
    """Add a suffix rather than overwrite a capture that is already there -
    in either format, so switching the Data box mid-folder cannot reuse a
    name."""
    if not capture_exists(base):
        return base
    n = 2
    while capture_exists(f"{base}_{n}"):
        n += 1
    return f"{base}_{n}"


# -- capture files -----------------------------------------------------------
#
# A capture is saved one of two ways, picked by the Data box beside Save
# screenshot. Both carry a .txt sidecar, which is the same either way.
#
#   CSV  time_s,CH1_..._V,... as decimal text. Opens in anything, and the AWG
#        panels replay it. MEASURED on a 500 kpt four-channel HRES record from
#        the MSO-X: 35.6 MB.
#   NPZ  what the scope sent: each channel's integer codes plus the preamble
#        numbers that scale them, in a compressed numpy archive. Same record:
#        1.5 MB (MEASURED on that capture converted by tools/csv_to_npz.py; a
#        fresh grab's WORD codes are not yet sized). Nothing is rounded - the CSV is the one that rounds, to
#        seven digits - and the volts rebuild bit for bit as the panel computed
#        them at grab time. Read with load_capture(), np.load, or EOM-ILC's
#        eomilc/scope.py, which carries its own copy of read_npz.
#
# The NPZ layout, version 1 (everything a plain array, so np.load needs no
# pickle):
#   format   "scope-grab-npz/1"
#   columns  the CSV header as an array of strings, time_s first
#   x, n     [x_inc, x_orig, x_ref] and the sample count:
#            t = (i - x_ref) * x_inc + x_orig
#   t        instead of x and n, the times outright (an average, or a
#            converted CSV whose time column was not a clean grid)
#   y<j>     column j's data, j = 1.. in header order. With s<j> beside it,
#            unsigned integer codes stored as differences from the previous
#            sample (the first as itself) - a cumulative sum in the same dtype
#            undoes it, wrap-around included. Without s<j>, volts as float64.
#   s<j>     [y_inc, y_ref, y_orig]:  v = (code - y_ref) * y_inc + y_orig
# Storing differences rather than codes is what takes a record from 2.1 MB to
# 1.5 MB: consecutive samples sit close together, so the differences are small
# numbers that compress better.

DATA_FORMATS = ("NPZ", "CSV")
CAPTURE_EXTS = (".npz", ".csv")
NPZ_FORMAT = "scope-grab-npz/1"


def capture_stem(path):
    """A capture's path without its extension, for finding its sidecar."""
    for ext in CAPTURE_EXTS:
        if path.lower().endswith(ext):
            return path[:-len(ext)]
    return path


def capture_exists(base):
    return any(os.path.exists(base + ext) for ext in CAPTURE_EXTS)


def _delta(codes):
    d = codes.copy()
    # Unsigned, so a step down wraps; the cumulative sum in the same dtype
    # wraps back. No warning either way - numpy only warns on scalars.
    d[1:] = codes[1:] - codes[:-1]
    return d


def write_npz(path, columns, t, ys):
    """Save a capture as NPZ (layout above). `t` is the preamble's
    (x_inc, x_orig, x_ref) when the time base is the scope's own, else an
    array of times. `ys` holds, per channel column, a scope_profiles.Record -
    its codes are what get stored - or an array of volts. Written to a
    temporary name and moved into place, so a crash mid-write cannot leave a
    truncated capture where a whole one is expected."""
    arrs = {"format": np.array(NPZ_FORMAT), "columns": np.array(list(columns))}
    for j, y in enumerate(ys, 1):
        if isinstance(y, scope_profiles.Record) and y.codes is not None:
            arrs[f"y{j}"] = _delta(np.ascontiguousarray(y.codes))
            arrs[f"s{j}"] = np.array(y.y, dtype=np.float64)
        else:
            v = y.v() if isinstance(y, scope_profiles.Record) else y
            arrs[f"y{j}"] = np.asarray(v, dtype=np.float64)
    if isinstance(t, tuple):
        arrs["x"] = np.array(t, dtype=np.float64)
        arrs["n"] = np.array(len(ys[0]) if ys else 0, dtype=np.int64)
    else:
        arrs["t"] = np.asarray(t, dtype=np.float64)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        np.savez_compressed(fh, **arrs)
    os.replace(tmp, path)
    return path


def read_npz(path):
    """(columns, data) of an NPZ capture, data[:, 0] being time_s - the same
    two things a CSV's header and np.loadtxt give."""
    with np.load(path, allow_pickle=False) as z:
        tag = str(z["format"]) if "format" in z.files else ""
        if not tag.startswith("scope-grab-npz/"):
            raise ValueError(f"{os.path.basename(path)} is not a Scope Grab "
                             f"capture (no format tag)")
        if tag != NPZ_FORMAT:
            raise ValueError(f"{os.path.basename(path)} is {tag}; this "
                             f"version of Scope Grab reads {NPZ_FORMAT}")
        columns = [str(c) for c in z["columns"]]
        if "t" in z.files:
            t = z["t"].astype(np.float64)
        else:
            x_inc, x_orig, x_ref = (float(a) for a in z["x"])
            t = (np.arange(int(z["n"])) - x_ref) * x_inc + x_orig
        data = np.empty((len(t), len(columns)))
        data[:, 0] = t
        for j in range(1, len(columns)):
            y = z[f"y{j}"]
            if f"s{j}" in z.files:
                y_inc, y_ref, y_orig = (float(a) for a in z[f"s{j}"])
                codes = np.cumsum(y, dtype=y.dtype)
                data[:, j] = (codes.astype(np.float64) - y_ref) * y_inc + y_orig
            else:
                data[:, j] = y
    return columns, data


def load_capture(path):
    """(columns, data) of a capture in either format: the header names, and
    the samples with time_s in the first column."""
    if path.lower().endswith(".npz"):
        return read_npz(path)
    with open(path, encoding="utf-8") as fh:
        header = fh.readline().strip()
    return header.split(","), np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)


def write_csv(path, columns, data):
    np.savetxt(path, data, delimiter=",", header=",".join(columns),
               comments="", fmt=[TIME_FMT] + [VOLT_FMT] * (data.shape[1] - 1))
    return path


def write_capture(base, ext, recs, metadata):
    """Write one capture: `base` + `ext` (".npz" or ".csv") holding `recs`,
    {column name: scope_profiles.Record} in channel order, and `base`.txt
    holding `metadata`, the text Scope.metadata() formatted. Returns the data
    file's path.

    The panel's grab and any program driving a Scope without the panel (the
    ramp polarimeter loads this file by path) write through here, so their
    captures cannot drift apart in layout. The time base is the first
    channel's, as it always was."""
    columns = ["time_s"] + list(recs)
    first = next(iter(recs.values()))
    if ext == ".npz":
        path = write_npz(base + ext, columns, first.x, list(recs.values()))
    else:
        path = write_csv(base + ext, columns, np.column_stack(
            [first.t()] + [r.v() for r in recs.values()]))
    # utf-8 explicitly: the file records channel names exactly as typed, and
    # the machine default here is cp1252, which cannot encode half of what a
    # name in this lab has in it. Failing on one would abort the grab with the
    # data file already written and the run reported as having saved nothing.
    with open(base + ".txt", "w", encoding="utf-8") as fh:
        fh.write(metadata)
    return path


def setting_roots(prof):
    """Every SCPI root the panel reads for its settings snapshot, in the order
    the panel lays them out. Scope.read_settings() asks exactly these, so a
    capture made without the panel carries the same metadata as one made with
    it."""
    roots = [scpi for _, scpi, _, _ in prof.timebase]
    roots += [scpi for _, scpi in prof.info]
    roots += [scpi for _, scpi, _, _ in prof.trigger]
    roots += [tmpl.format(ch=ch) for ch in prof.channels
              for _, tmpl, _, _ in prof.channel]
    return list(dict.fromkeys(roots))


def dither_offset(off0, span, k, count):
    """Channel offset for run k of `count` in an offset dither: evenly spaced
    across `span` volts, centred on the original offset `off0`. The preamble's
    yorigin carries the offset, so the volts read back are true at every
    step - only the converter's per-code error pattern moves."""
    return off0 + span * ((k + 0.5) / count - 0.5)


class DitherError(Exception):
    """A channel's scale or offset could not be read to plan a dither."""

    def __init__(self, ch, exc):
        super().__init__(f"CH{ch}: {exc}")
        self.ch = ch
        self.exc = exc


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
        if grab.get("data_format"):
            lines.append(f"  Data format       {grab['data_format']}")
        names = grab.get("channel_names") or {}
        ticked = grab.get("channels") or {}
        for ch in prof.channels:
            lines.append(f"  CH{ch} {'on ' if ticked.get(str(ch)) else 'off'}"
                         f"  {names.get(str(ch), '')}".rstrip())
    return "\n".join(lines) + "\n"


# -- averaging a numbered sequence ------------------------------------------
#
# A sequence is N single shots of the same thing. Their mean has the
# shot-to-shot noise down by sqrt(N) and keeps anything that repeats -- which
# is how a ripple that is a few tenths of a millivolt against a millivolt of
# single-shot scatter becomes visible. Pure file work: no instrument, so it
# runs whether or not the scope is connected, and other programs can import it.

def sequence_files(outdir, prefix):
    """The numbered captures of a sequence, {label: path}, in run order.
    Labels keep their zero-padding as found, so a series written as _001
    reads back as '001'. A run saved in both formats is read from its NPZ."""
    pat = re.compile(re.escape(prefix) + r"_(\d+)\.(csv|npz)$", re.IGNORECASE)
    out = {}
    try:
        for n in os.listdir(outdir):
            m = pat.match(n)
            if m and (m.group(1) not in out or m.group(2).lower() == "npz"):
                out[m.group(1)] = os.path.join(outdir, n)
    except OSError:
        pass
    return dict(sorted(out.items(), key=lambda kv: int(kv[0])))


def average_sequence(outdir, prefix, first=None, last=None, log=None,
                     fmt="CSV"):
    """Average the numbered runs of `prefix` (optionally labels first..last)
    into <prefix>_avg_<first>-<last>.csv (or .npz, by `fmt`), with a .txt
    sidecar made from the first run's, headed by what was averaged. The runs
    may be in either format, or a mix.

    Every run has to carry the same columns, the same point count and the
    same time base -- a sequence taken through one scope setup does, and
    one that changed setup mid-way is refused rather than blended. A gap in
    the series (a deleted run) is skipped and reported. Returns
    (path written, [labels used]). Raises ValueError on anything it cannot
    do.
    """
    say = log or (lambda *_: None)
    files = sequence_files(outdir, prefix)
    if not files:
        raise ValueError(f"no numbered files {prefix}_NNN.csv/.npz in {outdir}")
    labels = list(files)
    if first is not None:
        labels = [l for l in labels if int(l) >= int(first)]
    if last is not None:
        labels = [l for l in labels if int(l) <= int(last)]
    if len(labels) < 2:
        raise ValueError(f"need at least two runs to average; {prefix} has "
                         f"{len(labels)} in that range (of {len(files)} on disk)")
    lo, hi = int(labels[0]), int(labels[-1])
    missing = sorted(set(range(lo, hi + 1)) - {int(l) for l in labels})
    if missing:
        say(f"  labels missing from the series and skipped: "
            f"{', '.join(map(str, missing))}")

    header, acc, t0, n = None, None, None, 0
    for lab in labels:
        cols, data = load_capture(files[lab])
        head = ",".join(cols)
        if header is None:
            header, t0, acc = head, data[:, 0], np.zeros_like(data[:, 1:])
        elif head != header:
            raise ValueError(f"{os.path.basename(files[lab])} has columns "
                             f"{head!r}; the first run has {header!r}")
        elif data.shape != (len(t0), acc.shape[1] + 1):
            raise ValueError(f"{os.path.basename(files[lab])} has "
                             f"{data.shape[0]} points x {data.shape[1]} cols; "
                             f"the first run has {len(t0)} x {acc.shape[1] + 1}")
        elif np.abs(data[:, 0] - t0).max() > 1e-3 * float(np.median(np.diff(t0))):
            raise ValueError(f"{os.path.basename(files[lab])} is on a different "
                             f"time base from the first run -- the setup changed "
                             f"mid-sequence")
        acc += data[:, 1:]
        n += 1
    mean = acc / n
    width = len(labels[0])
    base = os.path.join(outdir, f"{prefix}_avg_{lo:0{width}d}-{hi:0{width}d}")
    # A mean of codes is not a code, so an NPZ average stores volts - still
    # under half the CSV, and without the CSV's rounding.
    if fmt.upper() == "NPZ":
        path = write_npz(base + ".npz", header.split(","), t0,
                         [mean[:, j] for j in range(mean.shape[1])])
    else:
        path = write_csv(base + ".csv", header.split(","),
                         np.column_stack([t0, mean]))
    side = capture_stem(files[labels[0]]) + ".txt"
    body = open(side, "r", encoding="utf-8").read() if os.path.exists(side) else ""
    with open(base + ".txt", "w", encoding="utf-8") as fh:
        fh.write(f"averaged from      : {n} runs, labels {labels[0]}-{labels[-1]}"
                 + (f" (missing: {', '.join(map(str, missing))})" if missing else "")
                 + "\n")
        fh.write(f"averaging          : mean of the samples per channel; "
                 f"time base from run {labels[0]}; single-shot scatter down by "
                 f"sqrt({n}) = {n ** 0.5:.1f}x\n")
        fh.write(f"settings below are : run {labels[0]}'s\n")
        fh.write(body)
    say(f"{os.path.basename(path)}  (mean of {n} runs, "
        f"{len(t0)} pts x {mean.shape[1]} cols)")
    return path, labels


# ---------------------------------------------------------------------------
# Reading captures back: what the plot tabs draw and the ledgers tabulate.
# Everything here works from the files alone, so it serves a capture from any
# folder and needs no instrument.
# ---------------------------------------------------------------------------

def capture_files(outdir, prefix):
    """Every capture of `prefix` in `outdir`, {run: path} in name order. The
    run is whatever follows the prefix: '003' for a sequence run,
    '20260903_120000' for a one-off, 'avg_001-064' for an averaged sequence.
    Name order puts a sequence in run order and one-offs in capture order. A
    run saved in both formats is read from its NPZ."""
    head = prefix + "_"
    out = {}
    try:
        names = sorted(n for n in os.listdir(outdir)
                       if n.lower().endswith(CAPTURE_EXTS) and n.startswith(head))
    except OSError:
        return out
    for n in names:
        run = capture_stem(n)[len(head):]
        if run not in out or n.lower().endswith(".npz"):
            out[run] = os.path.join(outdir, n)
    return out


def split_capture_name(path):
    """(prefix, run) of a capture's filename, by the patterns Scope Grab
    writes: prefix_NNN, prefix_YYYYMMDD_HHMMSS[_n], prefix_avg_A-B. A file
    named any other way is its own prefix with no run."""
    stem = capture_stem(os.path.basename(path))
    # Shortest prefix that leaves a whole run behind it, so a timestamp with a
    # _2 collision suffix is one run and a prefix that ends in digits keeps them.
    m = re.match(r"^(.+?)_(\d+|\d{8}_\d{6}(?:_\d+)?|avg_\d+-\d+)$", stem)
    return (m.group(1), m.group(2)) if m else (stem, "")


def select_runs(files, spec, notes=None):
    """Which of a prefix's runs a Runs box names: [(run, path)] in name order.

    files is capture_files()' {run: path}. The grammar, space or comma
    separated: N or A-B pick numbered runs; 'last' or 'lastN' the newest N by
    file time; 'all' every file; 'avg' every averaged file; anything else is
    a run as it appears after the prefix. Blank means the newest file. What
    did not resolve goes to `notes`."""
    say = notes.append if notes is not None else (lambda *_: None)
    if not files:
        return []
    tokens = [t for t in re.split(r"[\s,]+", spec.strip()) if t]
    if not tokens:
        tokens = ["last"]
    numbered = {int(r): r for r in files if r.isdigit()}
    chosen = set()
    for tok in tokens:
        low = tok.lower()
        if low == "all":
            chosen.update(files)
        elif low == "avg":
            hit = [r for r in files if r.startswith("avg_")]
            chosen.update(hit)
            if not hit:
                say(f"'{tok}': no averaged file for this prefix")
        elif re.fullmatch(r"last(\d*)", low):
            n = int(low[4:] or 1)
            newest = sorted(files, key=lambda r: os.path.getmtime(files[r]))
            chosen.update(newest[-n:])
        elif re.fullmatch(r"\d+-\d+", tok):
            a, b = (int(x) for x in tok.split("-"))
            hit = [numbered[i] for i in range(min(a, b), max(a, b) + 1)
                   if i in numbered]
            chosen.update(hit)
            if not hit:
                say(f"'{tok}': no numbered runs in that range")
        elif tok.isdigit() and int(tok) in numbered:
            chosen.add(numbered[int(tok)])
        elif tok in files:
            chosen.add(tok)
        else:
            say(f"'{tok}': no such run")
    return [(r, files[r]) for r in files if r in chosen]


def read_sidecar(path):
    """The .txt beside a capture as {key: value}, keys as written. The first
    colon separates key from value, which is where the metadata writer puts
    it, so a value with colons in it - a timestamp, a VISA address - survives."""
    meta = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key, sep, val = line.partition(":")
                key = key.strip()
                if sep and key and key not in meta:
                    meta[key] = val.strip()
    except OSError:
        pass
    return meta


def spectrum(t, v, window="hann", units="rms"):
    """One-sided spectrum of a uniformly sampled record, DC bin dropped.

    The mean is removed first, so the window's leakage from a DC offset does
    not bury the low bins. units 'rms': the amplitude of a sine at that
    frequency, in V rms - what a spectrum analyser shows; 'asd': V/sqrt(Hz),
    the amplitude spectral density, which is what a noise floor is quoted in
    and does not change with the record length."""
    n = len(v)
    dt = float(np.median(np.diff(t)))
    w = WINDOWS[window](n)
    x = np.abs(np.fft.rfft((v - v.mean()) * w))
    f = np.fft.rfftfreq(n, dt)
    if units == "asd":
        a = np.sqrt(2.0) * x / np.sqrt(np.sum(w * w) / dt)
    else:
        a = np.sqrt(2.0) * x / np.sum(w)
    return f[1:], a[1:]


def thin_index(v, step):
    """Which samples of `v` to draw when only 1 in `step` is: the lowest and
    the highest of each run of 2*step, in time order, and the two ends.

    Every point drawn is a real sample, and the trace keeps its envelope - a
    one-sample spike still shows, and a ripple faster than the thinned grid
    stays a band rather than turning into a slow beat that is not in the data,
    which is what drawing every step-th sample does to it."""
    n = len(v)
    if step <= 1 or n < 3:
        return np.arange(n)
    run = 2 * step
    m = n // run * run
    blocks = v[:m].reshape(-1, run)
    base = np.arange(0, m, run)
    idx = [base + blocks.argmin(axis=1), base + blocks.argmax(axis=1),
           np.array([0, n - 1])]
    if m < n:                       # what is left over is a short run of its own
        idx.append(m + np.array([v[m:].argmin(), v[m:].argmax()]))
    return np.unique(np.concatenate(idx))


def _top_base(v, vmax, vmin):
    """Vtop and Vbase the way the scope finds them: the most populated level
    in the upper and lower halves of a 256-bin histogram - the flats of a
    pulse. A level only counts as a flat when it holds a real share of the
    record; a sine or a ramp has none and takes max and min instead."""
    if vmax <= vmin:
        return vmax, vmin
    hist, edges = np.histogram(v, bins=256, range=(vmin, vmax))
    floor = 0.02 * len(v)

    def level(lo_bin, hi_bin, fallback):
        part = hist[lo_bin:hi_bin]
        if part.max() <= floor:
            return fallback
        k = lo_bin + int(part.argmax())
        # The flat's own samples, a bin either side of the fullest one: their
        # mean puts the level where the samples sit rather than at a bin
        # centre, which is half a bin off for a clean flat.
        sel = v[(v >= edges[max(k - 1, 0)]) & (v <= edges[min(k + 2, 256)])]
        return float(sel.mean()) if len(sel) else float((edges[k] + edges[k + 1]) / 2)

    return level(128, 256, vmax), level(0, 128, vmin)


def _crossing(t, v, level, j):
    """Time at which v crosses `level` between samples j and j+1."""
    dv = v[j + 1] - v[j]
    frac = (level - v[j]) / dv if dv else 0.0
    return float(t[j] + frac * (t[j + 1] - t[j]))


def _edges(t, v, base, amp):
    """Rising and falling edges by the scope's rule: a crossing of the 50 %
    level counts only once the signal has come from below 10 % and gone on
    above 90 % (or the reverse), so noise riding a flat does not fire edges.

    Returns (rising, falling, rise_times, fall_times): the mid-level times of
    each edge, and each edge's 10-90 % transition time."""
    lo, mid, hi = base + 0.1 * amp, base + 0.5 * amp, base + 0.9 * amp
    level = np.where(v > hi, 1, np.where(v < lo, -1, 0))
    nz = np.flatnonzero(level)
    if len(nz) < 2:
        return [], [], [], []
    idx = np.zeros(len(v), dtype=int)
    idx[nz] = nz
    state = level[np.maximum.accumulate(idx)]      # last decided level
    change = np.flatnonzero(np.diff(state) != 0) + 1
    up_mid = np.flatnonzero((v[:-1] < mid) & (v[1:] >= mid))
    dn_mid = np.flatnonzero((v[:-1] >= mid) & (v[1:] < mid))
    up_lo = np.flatnonzero((v[:-1] < lo) & (v[1:] >= lo))
    dn_hi = np.flatnonzero((v[:-1] >= hi) & (v[1:] < hi))
    rising, falling, rise_t, fall_t = [], [], [], []
    for i in change:
        if state[i - 1] == 0:                # the first decision, not an edge
            continue
        if state[i] == 1:
            k = np.searchsorted(up_mid, i) - 1
            if k < 0:
                continue
            rising.append(_crossing(t, v, mid, up_mid[k]))
            k2 = np.searchsorted(up_lo, i) - 1
            if k2 >= 0:
                rise_t.append(_crossing(t, v, hi, i - 1)
                              - _crossing(t, v, lo, up_lo[k2]))
        else:
            k = np.searchsorted(dn_mid, i) - 1
            if k < 0:
                continue
            falling.append(_crossing(t, v, mid, dn_mid[k]))
            k2 = np.searchsorted(dn_hi, i) - 1
            if k2 >= 0:
                fall_t.append(_crossing(t, v, lo, i - 1)
                              - _crossing(t, v, hi, dn_hi[k2]))
    return rising, falling, rise_t, fall_t


def _gaps(a, b):
    """For each time in a, the interval to the next time in b after it."""
    if not a or not b:
        return []
    b = np.asarray(b)
    out = []
    for x in a:
        k = np.searchsorted(b, x, side="right")
        if k < len(b):
            out.append(float(b[k] - x))
    return out


def measure(t, v):
    """The scope's Snapshot All, computed from the samples: {name: value} for
    every entry of MEAS_COLUMNS, NaN where the waveform does not define one.

    Period, widths, duty and the transition times are means over every full
    cycle in the record rather than the first one, which is what a record of
    many cycles is for. Overshoot and preshoot are relative to Vamp."""
    nan = float("nan")
    out = {name: nan for name, _ in MEAS_COLUMNS}
    if len(v) < 2:
        return out
    vmax, vmin = float(v.max()), float(v.min())
    out["Vpp"], out["Vmax"], out["Vmin"] = vmax - vmin, vmax, vmin
    out["Vavg"] = float(v.mean())
    out["Vrms"] = float(np.sqrt(np.mean(v * v)))
    out["Vrms AC"] = float(v.std())
    out["X@max"] = float(t[int(v.argmax())])
    out["X@min"] = float(t[int(v.argmin())])
    trap = getattr(np, "trapezoid", None) or np.trapz
    out["Area"] = float(trap(v, t))
    top, base = _top_base(v, vmax, vmin)
    amp = top - base
    out["Vtop"], out["Vbase"], out["Vamp"] = top, base, amp
    if amp <= 0:
        return out
    out["Overshoot"] = 100.0 * (vmax - top) / amp
    out["Preshoot"] = 100.0 * (base - vmin) / amp
    rising, falling, rise_t, fall_t = _edges(t, v, base, amp)
    periods = (np.diff(rising) if len(rising) >= 2 else
               np.diff(falling) if len(falling) >= 2 else [])
    if len(periods):
        out["Period"] = float(np.mean(periods))
        out["Freq"] = 1.0 / out["Period"]
    pos, neg = _gaps(rising, falling), _gaps(falling, rising)
    if pos:
        out["+Width"] = float(np.mean(pos))
    if neg:
        out["-Width"] = float(np.mean(neg))
    if pos and len(periods):
        out["Duty"] = 100.0 * out["+Width"] / out["Period"]
    if rise_t:
        out["Rise"] = float(np.mean(rise_t))
    if fall_t:
        out["Fall"] = float(np.mean(fall_t))
    return out


def fmt_si(x, unit=""):
    """4 significant figures with an SI prefix: 2.5e-05 s reads as 25 us."""
    if x is None or not np.isfinite(x):
        return "-"
    if x == 0:
        return f"0 {unit}".strip()
    exp = int(np.floor(np.log10(abs(x)) / 3)) * 3
    exp = max(-12, min(9, exp))
    pre = {-12: "p", -9: "n", -6: "u", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[exp]
    return f"{x / 10 ** exp:.4g} {pre}{unit}".strip()


def time_unit(span):
    """(scale, name) that puts a span of `span` seconds in the range 1-1000."""
    for scale, name in ((1.0, "s"), (1e3, "ms"), (1e6, "us"), (1e9, "ns")):
        if span * scale >= 1.0:
            return scale, name
    return 1e9, "ns"


def elide(text, n):
    return text if len(text) <= n else text[:n - 3] + "..."


def same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def key_safe(text):
    """A compare key has to survive the Compare box's split on whitespace, and
    this lab's prefixes and folders have spaces in them."""
    return re.sub(r"\s+", "-", text.strip())


def blend_white(colour, frac):
    """`colour` moved `frac` of the way to white: how a compare key's older
    runs fade behind its newest."""
    r, g, b = mcolors.to_rgb(colour)
    return (r + (1 - r) * frac, g + (1 - g) * frac, b + (1 - b) * frac)


PLOT_HINT = ("Runs: blank = newest, or  1-10  last3  avg  all.   "
             "Compare: PREFIX or PREFIX:RUNS, or Add files...")


class Capture:
    """One capture (CSV or NPZ) in memory, with its .txt sidecar parsed.

    key is the prefix it is known by in the plot boxes, run what follows the
    prefix in its filename. chan maps channel number to column, names carries
    the typed name the header was made from."""

    def __init__(self, path, key, run):
        self.path, self.key, self.run = path, key, run
        self.columns, data = load_capture(path)
        if self.columns[:1] != ["time_s"]:
            raise ValueError("not a Scope Grab capture: the first column is not time_s")
        if data.shape[1] != len(self.columns):
            raise ValueError(f"{data.shape[1]} columns of data under "
                             f"{len(self.columns)} headers")
        self.data = data
        self.t = data[:, 0]
        self.chan, self.names = {}, {}
        for i, col in enumerate(self.columns[1:], 1):
            m = re.fullmatch(r"CH(\d)(?:_(.*))?_V", col)
            if m:
                self.chan[int(m.group(1))] = i
                self.names[int(m.group(1))] = m.group(2) or ""
        self.dt = (float(np.median(np.diff(self.t))) if len(self.t) > 1
                   else float("nan"))
        self.meta = read_sidecar(capture_stem(path) + ".txt")
        self.label = f"{key} {run}" if run else key

    def v(self, ch):
        return self.data[:, self.chan[ch]]


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
        seen, refused = [], []
        for res in candidates:
            dev = None
            try:
                dev = self.rm.open_resource(res)
                dev.timeout = 5000
                dev.read_termination = "\n"
                dev.write_termination = "\n"
                idn = dev.query("*IDN?").strip()
            except Exception as exc:
                # Why it would not open is worth keeping. A device another VISA
                # session already holds refuses with VI_ERROR_NCIC, and without
                # this that reads to the user as "no scope found" - the one
                # diagnosis that sends someone to check a cable that is fine.
                refused.append((res, exc))
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
        # Nothing matched, so nothing owns this resource manager any more.
        # Leaving it open holds a VISA session for the life of the program and
        # is half of how a second copy of this program locks the first out.
        try:
            self.rm.close()
        except Exception:
            pass
        self.rm = None
        raise RuntimeError(self._nothing_found(seen, refused))

    def _nothing_found(self, seen, refused=()):
        """What to say when no instrument matched, including what did answer and
        what would not open."""
        # A device held by another session is the first thing to report, because
        # it is the one cause the usual advice actively misleads about: the
        # cable is fine, Connection Expert can see it, and it still will not
        # open. Two copies of this program is the usual reason.
        busy = [res for res, exc in refused
                if "NCIC" in str(exc) or "BUSY" in str(exc).upper()
                or "controller in charge" in str(exc).lower()]
        if busy:
            return ("Found the instrument, but something else already has it "
                    "open:\n" + "\n".join(f"  {r}" for r in busy)
                    + "\n\nAnother copy of Scope Grab is the usual reason - "
                    "close the other one and press Connect. A VISA session left "
                    "open by a script that crashed does the same thing.")
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
        if refused:
            return (msg + "\n\nWhat would not open:\n"
                    + "\n".join(f"  {r}: {exc}" for r, exc in refused))
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

    def record(self, channel, points_mode="RAW", points=None):
        """The same readout as waveform(), kept as the codes and preamble the
        scope sent - what a compact capture file stores."""
        return self.prof.read_record(self, channel, points_mode, points)

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
        if prof.meas_results and prof.meas_results in settings:
            # The scope's own measurement results, verbatim, when they
            # were asked for - see the Measurements tab. The scope's
            # format, not ours: label,value pairs, or
            # label,current,min,max,mean,sd,count with statistics on.
            lines.append(row("scope measurements",
                             s(prof.meas_results)))
        lines += [row(lbl, s(scpi)) for lbl, scpi in prof.meta_tail]
        for ch in channels:
            if names and names.get(ch):
                lines.append(chrow(f"CH{ch} name", names[ch]))
            lines += [chrow(f"CH{ch} {lbl}", s(scpi.format(ch=ch)))
                      for lbl, scpi in prof.meta_channel]
        return "\n".join(lines) + "\n"

    def read_settings(self, log=None):
        """The settings snapshot the panel takes before a capture, without the
        panel: {scpi root: reply} for every root in setting_roots(). A root
        that will not answer is left out (metadata() then prints '?') and
        reported to `log` if one is given."""
        values = {}
        for scpi in setting_roots(self.prof):
            try:
                values[scpi] = self.get(scpi)
            except Exception as exc:
                if log:
                    log(f"  {scpi}? failed: {exc}")
        return values

    def dither_plan(self, channels, codes):
        """{ch: (offset now, span in volts)} for an offset dither `codes` ADC
        codes wide on each channel - see the note at adc_code_per_vdiv for why
        it exists. Raises DitherError naming the first channel whose scale or
        offset would not read."""
        plan = {}
        for ch in channels:
            try:
                scale = float(self.get(self.prof.ch_scale.format(ch=ch)))
                off = float(self.get(self.prof.ch_offset.format(ch=ch)))
            except Exception as exc:
                raise DitherError(ch, exc) from exc
            plan[ch] = (off, codes * self.prof.adc_code_per_vdiv * scale)
        return plan

    def dither_step(self, plan, k, count):
        """Set every planned channel to its offset for run k of `count`.
        Returns {ch: exception} for any that refused; the rest are set."""
        failed = {}
        for ch, (off0, span) in plan.items():
            try:
                self.put(self.prof.ch_offset.format(ch=ch),
                         f"{dither_offset(off0, span, k, count):.6g}")
            except Exception as exc:
                failed[ch] = exc
        return failed

    def restore_offsets(self, plan):
        """Put every planned channel back on the offset it had before the
        dither. Returns {ch: exception} for any that refused."""
        failed = {}
        for ch, (off0, _) in plan.items():
            try:
                self.put(self.prof.ch_offset.format(ch=ch), f"{off0:.6g}")
            except Exception as exc:
                failed[ch] = exc
        return failed

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
        # Set only by the scope selector; __main__ reads it after the
        # window closes to decide whether to start a replacement.
        self.relaunch = False
        self.preview_w, self.preview_h = self.prof.preview_size
        self.busy = False
        self.auto_job = None
        self.prefix_job = None    # debounce for re-pointing the shot browser
        # The plot tabs' state. Captures are cached by path against their file
        # time; the tabs redraw lazily - a capture marks every tab dirty and
        # only the one on show is drawn, the others when they are turned to.
        self.plot_cache = {}      # path -> (mtime, Capture)
        self.plot_tabs = {}       # tab frame -> its draw function
        self.plot_dirty = set()
        self.plot_groups = None   # what the boxes last resolved to
        self.plot_notes_seen = set()
        self.plot_thin_warned = ""   # a 'Draw 1 in' entry already complained of
        self.cmp_paths = {}      # compare key -> ["prefix", folder, prefix] | ["file", path]
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
        # Which scope this panel is for. Everything below is laid out from the
        # profile - the settings rows, the channel list, the metadata file, the
        # screenshot box - so this cannot be switched in place without building
        # the whole window again. It restarts instead, which is honest and takes
        # a second; swapping scopes is a rare, deliberate act.
        self.model_names = {p.name: key
                            for key, p in sorted(scope_profiles.PROFILES.items())}
        self.model_var = tk.StringVar(value=self.prof.name)
        self.model_box = ttk.Combobox(
            top, textvariable=self.model_var, state="readonly", width=16,
            values=sorted(self.model_names))
        self.model_box.pack(side="right", padx=(0, 6))
        self.model_box.bind("<<ComboboxSelected>>", self.on_model_picked)
        ttk.Label(top, text="Scope:").pack(side="right", padx=(8, 3))

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
        # What the samples are saved as - see 'capture files' near the top.
        # NPZ is the codes the scope sent, ~20x smaller than the CSV's text;
        # CSV is for a file that has to open in Excel or replay on an AWG.
        ttk.Label(af, text="Data:").pack(side="left")
        self.data_fmt = tk.StringVar(value="NPZ")
        ttk.Combobox(af, textvariable=self.data_fmt, values=DATA_FORMATS,
                     state="readonly", width=5).pack(side="left", padx=(4, 0))

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
        # Averaging needs files, not the instrument, so it is live whenever
        # nothing is running -- connected or not.
        self.avg_btn = ttk.Button(row2, text="Average sequence...",
                                  command=self.do_average)
        self.avg_btn.pack(side="right")
        self.seq_next = tk.StringVar()
        ttk.Label(qf, textvariable=self.seq_next, foreground="#666").pack(
            anchor="w", padx=8, pady=(0, 6))
        # Offset dither: step every ticked channel's offset across a few ADC
        # codes over the runs, so the converter's per-code error pattern is
        # sampled at a different phase in every run and 'Average sequence...'
        # averages it out. The preamble's yorigin already puts each run's
        # volts right, so the runs themselves are unchanged; only the mean of
        # them gains. Offsets go back when the sequence ends, however it ends.
        row3 = ttk.Frame(qf)
        row3.pack(fill="x", padx=6, pady=(0, 6))
        self.seq_dither = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, variable=self.seq_dither,
                        text="dither offsets across").pack(side="left")
        self.seq_dither_codes = tk.StringVar(value="3")
        ttk.Entry(row3, textvariable=self.seq_dither_codes, width=4).pack(
            side="left", padx=(4, 2))
        ttk.Label(row3, text="ADC codes over the runs (then Average sequence)",
                  foreground="#666").pack(side="left")
        self.seq_dither_plan = {}      # {ch: (offset0, span V)} while running
        for var in (self.prefix, self.seq_start, self.seq_count, self.data_fmt):
            var.trace_add("write", lambda *_: self.show_next_name())
        self.show_next_name()

        self.toggle_existing()
        self.build_settings(left, pad)

        # --- the right column: a notebook of tabs, the log underneath.
        # The screenshot browser is the first tab; the rest draw and tabulate
        # the captured data, the way the ILC panel's tabs do. The plot bar
        # (built with the tabs) sits above the notebook on every tab, greyed
        # out on Screenshot, so turning between tabs moves nothing.
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        shot_tab = ttk.Frame(self.nb)
        self.nb.add(shot_tab, text="Screenshot")

        # --- last screenshot
        self.shot_frame = ttk.LabelFrame(shot_tab, text="Last screenshot")
        self.shot_frame.pack(fill="x", padx=4, pady=4)
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

        self.build_plots(right)

        # --- log
        lf = ttk.LabelFrame(right, text="Log")
        lf.pack(fill="x", **pad)
        self.logbox = tk.Text(lf, height=7, wrap="word", font=("Consolas", 9))
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
            self.refresh_plots()
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
            "seq_dither": self.seq_dither.get(),
            "seq_dither_codes": self.seq_dither_codes.get(),
            "auto_interval": self.interval.get(),
            "save_png": self.save_png.get(),
            "data_format": self.data_fmt.get(),
            "plot_runs": self.plot_runs.get(),
            "plot_compare": self.plot_cmp.get(),
            "plot_show": {str(ch): var.get() for ch, var in self.plot_show.items()},
            "plot_thin": self.plot_thin.get(),
            "spec_window": self.spec_window.get(),
            "spec_units": self.spec_units.get(),
            "record_measurements": self.rec_meas.get(),
            "cmp_paths": self.cmp_paths,
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
        # The plot bar's state, app-level like the folders: what was being
        # looked at last time, not a property of any setup. A blank Runs box
        # is a real value (the newest capture), so blanks are restored too.
        for key, var in (("plot_runs", self.plot_runs),
                         ("plot_compare", self.plot_cmp),
                         ("plot_thin", self.plot_thin),
                         ("spec_window", self.spec_window),
                         ("spec_units", self.spec_units)):
            value = cfg.get(key)
            if isinstance(value, str):
                var.set(value)
        if self.spec_window.get() not in WINDOWS:
            self.spec_window.set("hann")
        if self.spec_units.get() not in SPEC_UNITS:
            self.spec_units.set("V rms")
        show = cfg.get("plot_show")
        if isinstance(show, dict):
            for ch, var in self.plot_show.items():
                value = show.get(str(ch))
                if isinstance(value, (bool, int)):
                    var.set(bool(value))
        rec = cfg.get("record_measurements")
        if isinstance(rec, (bool, int)):
            self.rec_meas.set(bool(rec))
        paths = cfg.get("cmp_paths")
        if isinstance(paths, dict):
            self.cmp_paths = {
                key: entry for key, entry in paths.items()
                if isinstance(key, str) and isinstance(entry, list) and entry
                and entry[0] in ("prefix", "file")
                and all(isinstance(x, str) for x in entry)}

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
                         ("seq_dither_codes", self.seq_dither_codes),
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
        data_format = cfg.get("data_format")
        if isinstance(data_format, str) and data_format.upper() in DATA_FORMATS:
            self.data_fmt.set(data_format.upper())
        dither = cfg.get("seq_dither")
        if isinstance(dither, (bool, int)):
            self.seq_dither.set(bool(dither))
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
                "data_format": self.data_fmt.get(),
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
        self.refresh_plots()

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
        self.avg_btn.configure(state="disabled" if busy or self.seq_active
                               else "normal")
        # During a one-off grab the GRAB button becomes the way to call off a
        # long trigger wait. A sequence has its own Stop button instead.
        if busy and not self.seq_active:
            self.grab_btn.configure(text="Cancel wait", state="normal",
                                    command=self.cancel_grab)
        else:
            self.grab_btn.configure(text="GRAB  (or press Space)", state=state,
                                    command=self.do_grab)
        # Switching scope restarts the program, so it is dead while a capture
        # is running - that would abandon a VISA session and a half-written file.
        self.model_box.configure(
            state="disabled" if busy or self.seq_active else "readonly")
        # The sequence button stays live while a sequence runs, so it can stop it.
        self.seq_btn.configure(
            state="normal" if self.scope.inst and (self.seq_active or not busy)
            else "disabled")

    # -- actions ----------------------------------------------------------

    def on_model_picked(self, _event=None):
        """Switch the panel to another instrument, by restarting on it.

        A profile decides the settings rows, which channels exist, the metadata
        layout and the screenshot size, so changing it means rebuilding almost
        every widget in the window. Restarting is the version of that which
        cannot leave a half-rebuilt panel behind, and the config is already
        where the choice is remembered.

        A grab or a sequence in flight is left alone - it holds the VISA session
        and has files half-written."""
        wanted = self.model_names.get(self.model_var.get())
        if wanted is None or wanted == self.prof.key:
            return
        if self.busy or self.seq_active:
            self.model_var.set(self.prof.name)
            self.log("Cannot switch scope while a capture is running.")
            return
        other = scope_profiles.PROFILES[wanted]
        if not messagebox.askyesno(
                "Switch scope",
                f"Switch this panel from {self.prof.name} to {other.name}?\n\n"
                "The settings panel, the channel list and the metadata file are "
                "all built from the instrument, so Scope Grab has to restart to "
                "lay them out again. It will reconnect on its own.\n\n"
                "Your folder, prefix and channel names are kept. Anything in the "
                "settings panel that has not been applied will be lost.",
                parent=self.root):
            self.model_var.set(self.prof.name)
            return
        # Written before the restart, because the new process reads the model
        # out of the config file - see read_config().
        self.prof = other
        try:
            self.save_config()
        except Exception as exc:
            self.log(f"Could not save the scope choice: {exc}")
            self.prof = scope_profiles.get_profile(self.model_names.get(
                self.model_box.get()))
            return
        self.log(f"Switching to {other.name} - restarting...")
        self.restart()

    def restart(self):
        """Ask for a relaunch and close this window.

        It does NOT start the new process here. Spawning before this one has
        finished shutting down leaves both alive for a moment, both auto-connect
        on startup, and the second one finds the instrument already claimed by
        the first - VI_ERROR_NCIC, which reads as "no scope found". That is not
        hypothetical; it is what happened the first time this shipped.

        So the flag is set, the window closes, mainloop returns, and __main__
        starts the replacement once this process has nothing open."""
        self.stop_flag.set()
        self.stop_sequence()
        if self.auto_job is not None:
            self.root.after_cancel(self.auto_job)
            self.auto_job = None
        self.scope.close()
        self.relaunch = True
        self.root.destroy()

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
            ext = "." + self.data_fmt.get().lower()
            if base != wanted:
                self.log(f"  {os.path.basename(wanted)} is already there - "
                         f"writing {os.path.basename(base)}{ext} instead")

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
            # Kept as the scope sent them; the CSV turns them into volts at
            # write time, the NPZ stores them as they are. The time base is the
            # first channel's either way, as it always was.
            recs = {}
            for ch in chans:
                recs[self.column_name(ch)] = self.scope.record(
                    ch, points_mode=mode, points=points)

            # One settings read per grab: the panel and the metadata file are
            # built from the same snapshot.
            settings = self.read_all_settings()
            # Asked here rather than in the settings read: the waveform transfer
            # above has just succeeded, so there is certainly a record for the
            # scope to describe.
            hits = self.scope.hit_count()
            if hits is not None:
                settings[self.prof.wave_count] = hits
            if self.rec_meas.get() and self.prof.meas_results:
                # The scope's own measurement results, for the Measurements
                # tab. RESults? reports what is already being measured on the
                # screen and installs nothing; it has not been tried on this
                # scope, so it is asked with the short timeout and the device
                # clear behind it rather than letting an unterminated reply
                # hold a fast sequence for the full VISA timeout.
                meas = self.scope.try_get(self.prof.meas_results)
                if meas:
                    settings[self.prof.meas_results] = meas
            self.report_averaging(settings, existing=existing)
            # The screenshot has to be taken before :RUN, while the captured
            # trace is still the one on screen.
            img = self.scope.screenshot() if self.save_png.get() else None
            if resume:
                self.scope.run()
            t_read = time.time() - read_at

            self.set_phase("writing files")
            write_at = time.time()
            data_path = write_capture(
                base, ext, recs,
                self.scope.metadata(chans, settings, names, label, existing))
            first = next(iter(recs.values()))
            self.log(f"{os.path.basename(data_path)}  "
                     f"({len(first)} pts x {len(recs) + 1} cols, "
                     f"{os.path.getsize(data_path) / 1e6:.1f} MB)")
            self.grab_wrote = True

            if img is not None:
                png_path = base + ".png"
                with open(png_path, "wb") as fh:
                    fh.write(img)
                self.log(f"{os.path.basename(png_path)}  ({len(img)} bytes)")
                self.root.after(0, self.refresh_shots)
            # The data tabs follow a capture the way the screenshot pane does:
            # with the Runs box blank the newest run is what they draw.
            self.root.after(0, self.refresh_plots)
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

    # -- plots ------------------------------------------------------------

    def build_plots(self, right):
        """The plot bar and the data tabs, after the Screenshot tab.

        The bar is shared by every data tab: which runs of the current prefix
        to draw, what to compare them with, and which channels to show. Each
        tab keeps its own knobs in a strip above its figure or table. The bar
        stays above the notebook on every tab, so the tabs do not jump when
        turning to or from Screenshot; _sync_plot_bar greys out whatever the
        tab on show does not read."""
        bar = self.plot_bar = ttk.LabelFrame(right, text="Plot data")
        bar.pack(fill="x", padx=8, pady=(4, 0), before=self.nb)
        # What picks the captures, and what picks the channels: greyed
        # separately, since XY reads the first and not the second.
        sel = self.plot_sel_widgets = []
        row = ttk.Frame(bar)
        row.pack(fill="x", padx=6, pady=(4, 2))
        sel.append(ttk.Label(row, text="Runs:"))
        sel[-1].pack(side="left")
        self.plot_runs = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.plot_runs, width=14)
        e.pack(side="left", padx=(4, 10))
        e.bind("<Return>", lambda _e: self.do_plot_redraw())
        sel.append(e)
        sel.append(ttk.Label(row, text="Compare:"))
        sel[-1].pack(side="left")
        self.plot_cmp = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.plot_cmp, width=24)
        e.pack(side="left", fill="x", expand=True, padx=(4, 6))
        e.bind("<Return>", lambda _e: self.do_plot_redraw())
        sel.append(e)
        sel.append(ttk.Button(row, text="Add files...",
                              command=self.do_compare_add))
        sel[-1].pack(side="left")
        self.cmp_clear_btn = ttk.Button(row, text="Clear",
                                        command=self.do_compare_clear)
        self.cmp_clear_btn.pack(side="left", padx=(4, 0))
        sel.append(ttk.Button(row, text="Redraw", command=self.do_plot_redraw))
        sel[-1].pack(side="left", padx=(10, 0))

        row2 = ttk.Frame(bar)
        row2.pack(fill="x", padx=6, pady=(0, 2))
        show = self.plot_show_widgets = [ttk.Label(row2, text="Show:")]
        show[-1].pack(side="left")
        self.plot_show = {}
        for ch in (1, 2, 3, 4):
            var = tk.BooleanVar(value=True)
            self.plot_show[ch] = var
            show.append(ttk.Checkbutton(row2, text=f"CH{ch}", variable=var,
                                        command=self.refresh_plots))
            show[-1].pack(side="left", padx=(4, 0))
        # How many of a trace's samples are drawn, for the tabs that draw them
        # against time or each other. What is computed - spectra, the ledgers -
        # always takes every sample, so the box is grey there.
        thin = self.plot_thin_widgets = [ttk.Label(row2, text="Draw 1 in")]
        thin[-1].pack(side="left", padx=(18, 0))
        self.plot_thin = tk.StringVar()
        e = ttk.Entry(row2, textvariable=self.plot_thin, width=5)
        e.pack(side="left", padx=(4, 4))
        e.bind("<Return>", lambda _e: self.do_plot_redraw())
        thin.append(e)
        thin.append(ttk.Label(row2, text="samples"))
        thin[-1].pack(side="left")
        self.plot_thin_hint = ttk.Label(row2, foreground="#666",
                                        text="(blank = auto, 1 = all)")
        self.plot_thin_hint.pack(side="left", padx=(6, 0))

        row3 = ttk.Frame(bar)
        row3.pack(fill="x", padx=6, pady=(0, 4))
        self.plot_status_colour = "#666"      # put back when the bar is live
        self.plot_status = ttk.Label(row3, text=PLOT_HINT, foreground="#666")
        self.plot_status.pack(side="left")

        ctl, self.fig_wave = self._fig_tab("Waveforms", self._plot_waveforms)
        self.plot_thin_tabs = {ctl.master}    # the tabs that draw samples

        ctl, self.fig_spec = self._fig_tab("Spectrum", self._plot_spectrum)
        ttk.Label(ctl, text="Window:").pack(side="left")
        self.spec_window = tk.StringVar(value="hann")
        cb = ttk.Combobox(ctl, textvariable=self.spec_window,
                          values=list(WINDOWS), width=15, state="readonly")
        cb.pack(side="left", padx=(4, 10))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh_plots())
        ttk.Label(ctl, text="Units:").pack(side="left")
        self.spec_units = tk.StringVar(value="V rms")
        cb = ttk.Combobox(ctl, textvariable=self.spec_units,
                          values=list(SPEC_UNITS), width=11, state="readonly")
        cb.pack(side="left", padx=(4, 0))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh_plots())
        ttk.Label(ctl, foreground="#666",
                  text="mean removed; V rms is the height of a line, "
                       "V/sqrt(Hz) a noise floor").pack(side="left", padx=(12, 0))

        ctl, self.fig_diff = self._fig_tab("Difference", self._plot_difference)
        self.plot_thin_tabs.add(ctl.master)
        ttk.Label(ctl, text="Reference:").pack(side="left")
        self.diff_ref = tk.StringVar()
        self.diff_ref_box = ttk.Combobox(ctl, textvariable=self.diff_ref,
                                         width=28, state="readonly")
        self.diff_ref_box.pack(side="left", padx=(4, 0))
        self.diff_ref_box.bind("<<ComboboxSelected>>",
                               lambda _e: self.refresh_plots())
        ttk.Label(ctl, foreground="#666",
                  text="every other selected capture minus this one, on its "
                       "time base").pack(side="left", padx=(12, 0))

        ctl, self.fig_xy = self._fig_tab("XY", self._plot_xy)
        self.plot_no_show = {ctl.master}      # its channels are the two below
        self.plot_thin_tabs.add(ctl.master)
        self.xy_x,self.xy_y = tk.StringVar(value="CH1"), tk.StringVar(value="CH2")
        for text, var in (("X:", self.xy_x), ("Y:", self.xy_y)):
            ttk.Label(ctl, text=text).pack(side="left",
                                           padx=(0 if text == "X:" else 10, 0))
            cb = ttk.Combobox(ctl, textvariable=var, width=5, state="readonly",
                              values=["CH1", "CH2", "CH3", "CH4"])
            cb.pack(side="left", padx=(4, 0))
            cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh_plots())
        ttk.Label(ctl, foreground="#666",
                  text="one channel against another, sample by sample, per "
                       "capture").pack(side="left", padx=(12, 0))

        self.stats_heads = ("key", "run", "CH", "name", "points", "dt", "rate",
                            "V/div", "offset (V)", "coupling", "mean", "rms",
                            "pk-pk")
        _ctl, self.stats_tv = self._table_tab(
            "Statistics", self.stats_heads,
            (70, 110, 36, 110, 60, 70, 84, 56, 70, 60, 84, 84, 84),
            self._fill_stats,
            "every selected capture, per shown channel: what was acquired, "
            "and its mean, rms and swing")

        self.meas_heads = ("key", "run", "CH") + tuple(
            f"{name} ({unit})" for name, unit in MEAS_COLUMNS)
        ctl, self.meas_tv = self._table_tab(
            "Measurements", self.meas_heads,
            (70, 110, 36) + (78,) * len(MEAS_COLUMNS),
            self._fill_measurements,
            "the scope's Snapshot All set, computed from the samples of every "
            "selected capture")
        # The scope's own results, as an extra: asked at grab time and written
        # into the .txt, shown here under the computed ones. Opt-in, because
        # the query has not been tried on this scope and a fast sequence should
        # not find out the hard way.
        self.rec_meas = tk.BooleanVar(value=False)
        foot = ttk.Frame(ctl.master)          # under the table, full width
        foot.pack(side="bottom", fill="x", padx=8, pady=(0, 4))
        ttk.Checkbutton(foot, variable=self.rec_meas,
                        text="also record the scope's own results with each "
                             "grab (:MEASure:RESults?, not yet tried on this "
                             "scope)").pack(anchor="w")
        self.scope_meas = ttk.Label(foot, text="", foreground="#666",
                                    justify="left", wraplength=700)
        self.scope_meas.pack(anchor="w", pady=(2, 0))

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._sync_plot_bar()

    def _fig_tab(self, name, draw):
        """A tab holding a matplotlib figure with the zoom/pan toolbar, plus a
        strip above it for that tab's own knobs. Returns (strip, figure); the
        figure carries its canvas and toolbar as _canvas and _toolbar the way
        the ILC panel's do. Without matplotlib the tab says so and draws
        nothing, but still counts as a data tab so the bar is live."""
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=name)
        ctl = ttk.Frame(frame)
        ctl.pack(fill="x", padx=4, pady=(4, 0))
        self.plot_dirty.add(frame)
        if Figure is None:
            ttk.Label(frame, text=NO_MPL, foreground="#a00",
                      justify="left").pack(anchor="w", padx=8, pady=12)
            self.plot_tabs[frame] = lambda groups: None
            return ctl, None
        matplotlib.rcParams["font.size"] = 8
        fig = Figure(figsize=(6.4, 4.6), dpi=100, constrained_layout=True)
        canvas = FigureCanvasTkAgg(fig, master=frame)
        toolbar = NavigationToolbar2Tk(canvas, frame)   # packs itself, bottom
        canvas.get_tk_widget().pack(fill="both", expand=True)
        fig._canvas, fig._toolbar = canvas, toolbar
        self.plot_tabs[frame] = draw
        return ctl, fig

    def _table_tab(self, name, heads, widths, fill, note):
        """A tab holding a ledger, saveable as CSV the way the figures save as
        PNG from their toolbars. Returns (strip, treeview)."""
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=name)
        ctl = ttk.Frame(frame)
        ctl.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(ctl, text=note, foreground="#666").pack(side="left")
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True, padx=4, pady=4)
        cols = [f"c{i}" for i in range(len(heads))]
        tv = ttk.Treeview(body, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            tv.heading(c, text=h)
            tv.column(c, width=w, minwidth=36, stretch=False,
                      anchor="w" if h in ("key", "run", "name", "coupling")
                      else "e")
        ysb = ttk.Scrollbar(body, orient="vertical", command=tv.yview)
        xsb = ttk.Scrollbar(body, orient="horizontal", command=tv.xview)
        tv.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        tv.pack(side="left", fill="both", expand=True)
        ttk.Button(ctl, text="Save CSV...",
                   command=lambda: self._save_table(tv, heads, name)).pack(
            side="right")
        self.plot_tabs[frame] = fill
        self.plot_dirty.add(frame)
        return ctl, tv

    def _current_tab(self):
        try:
            return self.nb.nametowidget(self.nb.select())
        except (tk.TclError, KeyError):
            return None

    def _on_tab_changed(self, _event=None):
        tab = self._current_tab()
        if tab in self.plot_tabs and tab in self.plot_dirty:
            self._draw_tab(tab)
        self._sync_plot_bar()

    def _sync_plot_bar(self):
        """Grey out what the tab on show does not read: all of the bar on
        Screenshot; the Show ticks on XY, which picks its two channels itself;
        and 'Draw 1 in' wherever samples are computed from rather than drawn.
        Clear is live only with something to clear."""
        tab = self._current_tab()
        live = tab in self.plot_tabs
        shows = live and tab not in self.plot_no_show
        thins = tab in self.plot_thin_tabs
        for w in self.plot_sel_widgets:
            w.configure(state="normal" if live else "disabled")
        for w in self.plot_show_widgets:
            w.configure(state="normal" if shows else "disabled")
        for w in self.plot_thin_widgets:
            w.configure(state="normal" if thins else "disabled")
        self.plot_thin_hint.configure(state="normal" if thins else "disabled",
                                      foreground="#666" if thins else "")
        self.cmp_clear_btn.configure(
            state="normal" if live and (self.plot_cmp.get().strip()
                                        or self.cmp_paths) else "disabled")
        # The status line's colour is its own, so the theme's grey only shows
        # through once that is taken off.
        self.plot_status.configure(
            state="normal" if live else "disabled",
            foreground=self.plot_status_colour if live else "")

    def refresh_plots(self):
        """Something moved - a capture landed, the folder or the prefix
        changed, a box or a tick - so every data tab is stale. Only the one on
        show is drawn now; the rest draw when they are turned to, so a fast
        sequence is not paying for four figures per run."""
        self.plot_dirty = set(self.plot_tabs)
        self.plot_groups = None
        tab = self._current_tab()
        if tab in self.plot_tabs:
            self._draw_tab(tab)

    def do_plot_redraw(self):
        """Redraw, and let a warning that was said once be said again: the
        boxes may have been edited to answer it."""
        self.plot_notes_seen.clear()
        self.plot_thin_warned = ""
        self.refresh_plots()

    def _draw_tab(self, tab):
        if self.plot_groups is None:
            groups, notes = self._resolve_plot_selection()
            self.plot_groups = groups
            for note in notes:
                if note not in self.plot_notes_seen:
                    self.plot_notes_seen.add(note)
                    self.log(f"plot: {note}")
            self._refresh_plot_status(groups, notes)
        try:
            self.plot_tabs[tab](self.plot_groups)
        except Exception as exc:
            self.log(f"plot: ERROR {exc}")
        self.plot_dirty.discard(tab)

    def _resolve_plot_selection(self):
        """The two boxes -> ([(key, [Capture], primary)], notes).

        Primary is the current prefix in the output folder, on the viridis
        ramp; every Compare token is a key, either a prefix the output folder
        answers for or something Add files... mapped, with the Runs grammar
        after a colon. What did not resolve is returned as notes, said once
        each in the log."""
        outdir, prefix = self.outdir.get(), self.safe_prefix()
        notes, groups = [], []
        files = capture_files(outdir, prefix)
        if not files:
            notes.append(f"{prefix}: no {prefix}_* capture in {outdir}")
        sub = []
        caps = self._load_runs(select_runs(files, self.plot_runs.get(), sub),
                               prefix, notes)
        notes += [f"Runs {n}" for n in sub]
        groups.append((prefix, caps, True))
        for tok in self.plot_cmp.get().split():
            key, _, spec = tok.partition(":")
            if not key:
                continue
            entry = self.cmp_paths.get(key)
            if entry and entry[0] == "file":
                cap = self._load_capture(entry[1], key, "", notes)
                caps = [cap] if cap else []
            else:
                folder, pre = (entry[1], entry[2]) if entry else (outdir, key)
                cfiles = capture_files(folder, pre)
                if not cfiles:
                    notes.append(f"{key}: no {pre}_* capture in {folder}")
                    continue
                sub = []
                caps = self._load_runs(select_runs(cfiles, spec, sub), key, notes)
                notes += [f"{key} {n}" for n in sub]
            groups.append((key, caps, False))
        # A different grid does not stop an overlay - it is a legitimate
        # comparison - but the spectra then have their own bin widths and a
        # difference is interpolated, and neither shows in a plot of curves.
        ref = next((c for c in groups[0][1]), None)
        if ref is not None:
            for key, caps, primary in groups[1:]:
                for cap in caps:
                    if (len(cap.t) != len(ref.t)
                            or abs(cap.dt - ref.dt) > 1e-6 * ref.dt):
                        notes.append(
                            f"NOTE: {cap.label} runs {len(cap.t)} pts @ "
                            f"{fmt_si(cap.dt, 's')} against {ref.label}'s "
                            f"{len(ref.t)} @ {fmt_si(ref.dt, 's')} - drawn on "
                            f"its own grid; its spectrum has its own bin width "
                            f"and a difference against it is interpolated")
        return groups, notes

    def _load_runs(self, runs, key, notes):
        caps = [self._load_capture(path, key, run, notes) for run, path in runs]
        return [c for c in caps if c is not None]

    def _load_capture(self, path, key, run, notes):
        """A Capture from the cache, or read now. Cached against the file's
        time, so a run rewritten under the same name is read again."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            notes.append(f"{os.path.basename(path)} is not there")
            return None
        hit = self.plot_cache.get(path)
        if hit is not None and hit[0] == mtime:
            cap = hit[1]
            cap.key, cap.run = key, run
            cap.label = f"{key} {run}" if run else key
            return cap
        try:
            cap = Capture(path, key, run)
        except Exception as exc:
            notes.append(f"{os.path.basename(path)}: {exc}")
            return None
        if len(self.plot_cache) >= 200:       # a long sequence, not the disk
            self.plot_cache.pop(next(iter(self.plot_cache)))
        self.plot_cache[path] = (mtime, cap)
        self.log(f"  {cap.label}: {len(cap.t)} pts @ {fmt_si(cap.dt, 's')}, "
                 + ", ".join(f"CH{ch}" for ch in sorted(cap.chan))
                 + ("" if cap.meta else "   (no .txt beside it)"))
        return cap

    def _refresh_plot_status(self, groups, notes):
        """Keep the status line telling the truth: what resolved, and whether
        the boxes asked for more than that."""
        shown = [(key, caps) for key, caps, _ in groups if caps]
        if shown:
            names = "; ".join(f"{key} ({len(caps)})" for key, caps in shown)
            total = sum(len(caps) for _, caps in shown)
            text = f"{total} capture(s): {names}"
            colour = "#060"
            if any(not n.startswith("NOTE") for n in notes):
                text += "   [not everything resolved - see the log]"
                colour = "#c60"
        elif notes:
            text, colour = "nothing resolved - see the log", "#c60"
        else:
            text, colour = PLOT_HINT, "#666"
        self.plot_status.configure(text=elide(text, 110))
        self.plot_status_colour = colour
        self._sync_plot_bar()

    def do_compare_add(self):
        """Pick captures to overlay, from anywhere on disk.

        Files that share a folder and a prefix become one key with the Runs
        grammar behind it, so a sequence from another day is picked in one go
        and addressed as KEY:1-10 like a prefix in the output folder. A file in
        the output folder needs no key of its own - the folder answers for its
        prefix - and a file not named the way Scope Grab names them is its own
        key. The Compare box stays the record of what is drawn: picking appends
        to it, so the box and the picks are one thing seen two ways."""
        paths = filedialog.askopenfilenames(
            title="Pick captures to compare",
            initialdir=self.outdir.get() if os.path.isdir(self.outdir.get()) else ".",
            filetypes=[("Captures", "*.npz *.csv"), ("All files", "*.*")],
            parent=self.root)
        if not paths:
            return
        outdir = self.outdir.get()
        spec, order = {}, []
        for tok in self.plot_cmp.get().split():
            key, _, runs = tok.partition(":")
            if key and key not in spec:
                spec[key] = []
                order.append(key)
            if key:
                spec[key] += [r for r in re.split(r"[\s,]+", runs)
                              if r and r not in spec[key]]
        for p in paths:
            p = os.path.abspath(p)
            if not os.path.exists(p):
                self.log(f"compare: {p} is not there - skipped")
                continue
            key, run = self._compare_key(p, outdir)
            if key not in spec:
                spec[key] = []
                order.append(key)
            if run and run not in spec[key]:
                spec[key].append(run)
        self.plot_cmp.set(" ".join(
            key + (":" + ",".join(spec[key]) if spec[key] else "")
            for key in order))
        self.do_plot_redraw()

    def _compare_key(self, path, outdir):
        """(key, run) for a picked file, adding to cmp_paths when the output
        folder cannot answer for it by itself."""
        folder = os.path.dirname(path)
        pre, run = split_capture_name(path)
        if run and key_safe(pre) == pre and same_path(folder, outdir):
            return pre, run               # the output folder answers for it
        for key, entry in self.cmp_paths.items():
            if run and entry[0] == "prefix" and entry[2] == pre \
                    and same_path(entry[1], folder):
                return key, run
            if not run and entry[0] == "file" and same_path(entry[1], path):
                return key, ""
        want = key_safe(pre if run else os.path.splitext(os.path.basename(path))[0])
        key = self._free_key(want, folder, outdir)
        self.cmp_paths[key] = ["prefix", folder, pre] if run else ["file", path]
        return key, run

    def _free_key(self, want, folder, outdir):
        """`want` unless something already answers to it - the current prefix,
        a prefix the output folder holds, or a mapped key - in which case the
        folder's name is appended, and a number after that."""
        taken = set(self.cmp_paths) | {self.safe_prefix()}
        try:
            taken |= {split_capture_name(n)[0] for n in os.listdir(outdir)
                      if n.lower().endswith(CAPTURE_EXTS)}
        except OSError:
            pass
        if want not in taken:
            return want
        stem = f"{want}@{key_safe(os.path.basename(os.path.normpath(folder)))}"
        key, n = stem, 2
        while key in taken:
            key = f"{stem}-{n}"
            n += 1
        return key

    def do_compare_clear(self):
        """Unload every comparison: the box, the picked keys, and the cache
        behind them, which would otherwise hold a sequence nobody is drawing."""
        self.plot_cmp.set("")
        self.cmp_paths.clear()
        self.plot_cache.clear()
        self.log("compare: cleared - only the current prefix is drawn.")
        self.do_plot_redraw()

    # -- the drawing itself

    def _captures(self, groups):
        """Every selected capture in box order: the current prefix's runs,
        then each compare key's. The order the ledgers list them in."""
        return [cap for _, caps, _ in groups for cap in caps]

    def _traces(self, groups):
        """[(capture, colour, width, zorder)] in draw order: compare keys
        first so they sit under the current prefix's, and within each key
        oldest first so the newest paints last and is drawn heaviest."""
        out, ci = [], 0
        for key, caps, primary in groups:
            if primary:
                continue
            base = CMP_COLOURS[ci % len(CMP_COLOURS)]
            ci += 1
            k = len(caps)
            for idx, cap in enumerate(caps):
                out.append((cap, blend_white(base, 0.6 * (k - 1 - idx) / max(k - 1, 1)),
                            1.0 if idx == k - 1 else 0.8, CMP_ZORDER))
        for key, caps, primary in groups:
            if not primary:
                continue
            n = len(caps)
            for idx, cap in enumerate(caps):
                col = matplotlib.colormaps["viridis"](0.1 + 0.75 * idx / max(n - 1, 1))
                out.append((cap, col, 1.3 if idx == n - 1 else 0.8,
                            2.2 if idx == n - 1 else 2.0))
        return out

    def _shown_channels(self, traces):
        """Channels ticked under Show that at least one selected capture has."""
        present = set()
        for cap, *_ in traces:
            present |= set(cap.chan)
        return [ch for ch in (1, 2, 3, 4)
                if self.plot_show[ch].get() and ch in present]

    def _ch_label(self, ch, traces, unit="V"):
        name = next((cap.names[ch] for cap, *_ in traces
                     if cap.names.get(ch)), "")
        return f"CH{ch}{' ' + name if name else ''} ({unit})"

    def _plot_title(self, groups):
        """The data record: the folder, and which runs of which keys."""
        parts = []
        for key, caps, _ in groups:
            if caps:
                runs = [cap.run or cap.key for cap in caps]
                parts.append(f"{key}: " + (", ".join(runs) if len(runs) <= 4 else
                                           f"{runs[0]} .. {runs[-1]} ({len(runs)})"))
        folder = os.path.basename(os.path.normpath(self.outdir.get())) or "?"
        return elide(f"{folder}   |   " + ";   ".join(parts), 120)

    def _panes(self, fig, n, sharex=True):
        """`n` stacked axes on a cleared figure, or a placeholder for none."""
        fig.clear()
        if n == 0:
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, "nothing to draw - see the plot bar and the log",
                    ha="center", va="center", color="#999", transform=ax.transAxes)
            ax.set_axis_off()
            return []
        return list(np.atleast_1d(fig.subplots(n, 1, sharex=sharex)))

    def _finish(self, fig):
        fig._canvas.draw_idle()
        fig._toolbar.update()       # the axes are new: the zoom stack was theirs

    def _legend(self, ax, count, loc="best"):
        if 0 < count <= LEGEND_MAX:
            ax.legend(loc=loc, fontsize=7, ncols=2 if count > 6 else 1)

    def _plot_note(self, ax, text, loc="nw"):
        """Method notes, small and grey, in a corner the data leaves empty."""
        xy = (0.01, 0.99) if loc == "nw" else (0.01, 0.01)
        ax.annotate(text, xy, xycoords="axes fraction", fontsize=6.5,
                    color="#999999", ha="left",
                    va="top" if loc == "nw" else "bottom")

    def _thin_step(self, n):
        """The 'Draw 1 in' box for a trace of `n` samples: blank = auto, the
        step that keeps it to PLOT_AUTO_PTS drawn; a number = that step
        whatever the length; 1 = every sample."""
        txt = self.plot_thin.get().strip()
        if txt:
            try:
                step = int(float(txt))
                if step >= 1:
                    return step
            except (ValueError, OverflowError):
                pass
            if txt != self.plot_thin_warned:
                self.plot_thin_warned = txt
                self.log(f"plot: 'Draw 1 in {txt}' is not a whole number of 1 "
                         "or more - drawing as if the box were blank")
        return max(1, -(-n // PLOT_AUTO_PTS))

    def _thinned(self, x, y, steps, envelope=True):
        """x and y as the 'Draw 1 in' box has them drawn, the step used added
        to `steps` for the figure's note. Against time a trace keeps its
        lowest and highest sample of each run (thin_index); one channel
        against another has no envelope to keep and takes every step-th."""
        step = self._thin_step(len(y))
        if step <= 1:
            return x, y
        steps.add(step)
        i = thin_index(y, step) if envelope else slice(None, None, step)
        return x[i], y[i]

    def _thin_note(self, steps, envelope=True):
        """What the figure says about it, so a saved PNG carries the fact."""
        if not steps:
            return ""
        lo, hi = min(steps), max(steps)
        text = f"1 in {lo if lo == hi else f'{lo} to {hi}'} samples drawn"
        if envelope:
            text += (f": the lowest and highest of each {2 * lo}" if lo == hi
                     else ": the lowest and highest of each run")
        return text

    def _plot_waveforms(self, groups):
        fig = self.fig_wave
        if fig is None:
            return
        traces = self._traces(groups)
        chans = self._shown_channels(traces)
        axes = self._panes(fig, len(chans))
        if not axes:
            return self._finish(fig)
        span = max((cap.t[-1] - cap.t[0] for cap, *_ in traces if len(cap.t) > 1),
                   default=1.0)
        scale, unit = time_unit(span)
        thinned = set()
        for ax, ch in zip(axes, chans):
            n = 0
            for cap, col, lw, z in traces:
                if ch in cap.chan:
                    x, y = self._thinned(cap.t, cap.v(ch), thinned)
                    ax.plot(x * scale, y, color=col, lw=lw,
                            zorder=z, label=cap.label)
                    n += 1
            ax.set_ylabel(self._ch_label(ch, traces))
            ax.grid(True, alpha=0.3)
            self._legend(ax, n)
        if thinned:
            self._plot_note(axes[0], self._thin_note(thinned))
        axes[-1].set_xlabel(f"time ({unit})")
        fig.suptitle(self._plot_title(groups), fontsize=8)
        self._finish(fig)

    def _plot_spectrum(self, groups):
        fig = self.fig_spec
        if fig is None:
            return
        traces = self._traces(groups)
        chans = self._shown_channels(traces)
        axes = self._panes(fig, len(chans))
        if not axes:
            return self._finish(fig)
        window = self.spec_window.get() if self.spec_window.get() in WINDOWS else "hann"
        units = SPEC_UNITS.get(self.spec_units.get(), "rms")
        ulabel = "V rms" if units == "rms" else "V/sqrt(Hz)"
        for ax, ch in zip(axes, chans):
            n, bins = 0, []
            for cap, col, lw, z in traces:
                if ch not in cap.chan or len(cap.t) < 8:
                    continue
                f, a = spectrum(cap.t, cap.v(ch), window, units)
                ax.loglog(f, a, color=col, lw=lw, zorder=z, label=cap.label)
                n += 1
                bins.append(f[0])
            ax.set_ylabel(self._ch_label(ch, traces, ulabel))
            ax.grid(True, which="both", alpha=0.3)
            # upper right: a falling spectrum leaves the lower left empty, and
            # that corner is the note's
            self._legend(ax, n, loc="upper right")
            if bins:
                width = fmt_si(min(bins), "Hz")
                if max(bins) > 1.5 * min(bins):
                    width += f" - {fmt_si(max(bins), 'Hz')}"
                self._plot_note(ax, f"{window} window, mean removed, bin {width}",
                                loc="sw")
        axes[-1].set_xlabel("frequency (Hz)")
        fig.suptitle(self._plot_title(groups), fontsize=8)
        self._finish(fig)

    def _plot_difference(self, groups):
        fig = self.fig_diff
        if fig is None:
            return
        traces = self._traces(groups)
        labels = [cap.label for cap, *_ in traces]
        self.diff_ref_box.configure(values=labels)
        if self.diff_ref.get() not in labels:
            # the current prefix's oldest selected run, failing that whatever
            # is first: the thing the others are held against
            first = next((cap.label for _, caps, primary in groups
                          if primary for cap in caps[:1]), labels[0] if labels else "")
            self.diff_ref.set(first)
        ref = next((cap for cap, *_ in traces if cap.label == self.diff_ref.get()),
                   None)
        chans = [ch for ch in self._shown_channels(traces)
                 if ref is not None and ch in ref.chan]
        axes = self._panes(fig, len(chans) if len(traces) > 1 else 0)
        if not axes:
            return self._finish(fig)
        scale, unit = time_unit(ref.t[-1] - ref.t[0] if len(ref.t) > 1 else 1.0)
        regridded, thinned = [], set()
        for ax, ch in zip(axes, chans):
            n = 0
            for cap, col, lw, z in traces:
                if cap is ref or ch not in cap.chan:
                    continue
                other = cap.v(ch)
                if (len(cap.t) != len(ref.t)
                        or np.abs(cap.t - ref.t).max() > 1e-3 * abs(ref.dt)):
                    other = np.interp(ref.t, cap.t, other)
                    if cap.label not in regridded:
                        regridded.append(cap.label)
                # subtracted sample for sample, and only then thinned
                x, y = self._thinned(ref.t, other - ref.v(ch), thinned)
                ax.plot(x * scale, y, color=col, lw=lw,
                        zorder=z, label=f"{cap.label} - {ref.label}")
                n += 1
            ax.axhline(0, color="#999999", lw=0.6)
            ax.set_ylabel(f"CH{ch} difference (V)")
            ax.grid(True, alpha=0.3)
            self._legend(ax, n)
        notes = []
        if regridded:
            notes.append("interpolated onto the reference's time base: "
                         + ", ".join(regridded[:4])
                         + (" ..." if len(regridded) > 4 else ""))
        if thinned:
            notes.append(self._thin_note(thinned))
        if notes:
            self._plot_note(axes[0], "\n".join(notes))
        axes[-1].set_xlabel(f"time ({unit})")
        fig.suptitle(elide(f"{self._plot_title(groups)}   -   minus {ref.label}", 130),
                     fontsize=8)
        self._finish(fig)

    def _plot_xy(self, groups):
        fig = self.fig_xy
        if fig is None:
            return
        traces = self._traces(groups)
        try:
            x, y = int(self.xy_x.get()[2:]), int(self.xy_y.get()[2:])
        except ValueError:
            x, y = 1, 2
        usable = [tr for tr in traces if x in tr[0].chan and y in tr[0].chan]
        axes = self._panes(fig, 1 if usable else 0)
        if not axes:
            return self._finish(fig)
        ax = axes[0]
        thinned = set()
        for cap, col, lw, z in usable:
            vx, vy = self._thinned(cap.v(x), cap.v(y), thinned, envelope=False)
            ax.plot(vx, vy, color=col, lw=0.8 * lw, zorder=z,
                    alpha=0.9, label=cap.label)
        if thinned:
            self._plot_note(ax, self._thin_note(thinned, envelope=False))
        ax.set_xlabel(self._ch_label(x, usable))
        ax.set_ylabel(self._ch_label(y, usable))
        ax.grid(True, alpha=0.3)
        self._legend(ax, len(usable))
        fig.suptitle(self._plot_title(groups), fontsize=8)
        self._finish(fig)

    def _fill_stats(self, groups):
        tv = self.stats_tv
        tv.delete(*tv.get_children())
        caps = self._captures(groups)
        chans = self._shown_channels([(cap,) for cap in caps])
        for cap in caps:
            for ch in chans:
                if ch not in cap.chan:
                    continue
                v, m = cap.v(ch), cap.meta
                rate = 1.0 / cap.dt if cap.dt and np.isfinite(cap.dt) else float("nan")
                tv.insert("", "end", values=(
                    cap.key, cap.run, f"CH{ch}", cap.names.get(ch, ""), len(v),
                    fmt_si(cap.dt, "s"), fmt_si(rate, "Sa/s"),
                    m.get(f"CH{ch} V/div", "-"), m.get(f"CH{ch} offset", "-"),
                    m.get(f"CH{ch} coupling", "-"),
                    fmt_si(float(v.mean()), "V"),
                    fmt_si(float(np.sqrt(np.mean(v * v))), "V"),
                    fmt_si(float(v.max() - v.min()), "V")))

    def _fill_measurements(self, groups):
        tv = self.meas_tv
        tv.delete(*tv.get_children())
        caps = self._captures(groups)
        chans = self._shown_channels([(cap,) for cap in caps])
        from_scope = []
        for cap in caps:
            for ch in chans:
                if ch not in cap.chan:
                    continue
                m = measure(cap.t, cap.v(ch))
                tv.insert("", "end", values=(cap.key, cap.run, f"CH{ch}") + tuple(
                    (f"{m[name]:.2f} %" if np.isfinite(m[name]) else "-")
                    if unit == "%" else fmt_si(m[name], unit)
                    for name, unit in MEAS_COLUMNS))
            raw = cap.meta.get("scope measurements")
            if raw:
                from_scope.append(f"scope's own, {cap.label}: {raw}")
        self.scope_meas.configure(
            text="\n".join(from_scope[:6]) if from_scope else
            "(no results from the scope itself in these captures - the tick "
            "above records them with each grab)")

    def _save_table(self, tv, heads, name):
        rows = [tv.item(iid, "values") for iid in tv.get_children()]
        if not rows:
            self.log(f"{name}: nothing to save yet.")
            return
        path = filedialog.asksaveasfilename(
            title=f"Save {name} as CSV", defaultextension=".csv",
            filetypes=[("CSV", "*.csv")], parent=self.root,
            initialdir=self.outdir.get() if os.path.isdir(self.outdir.get()) else ".",
            initialfile=f"{self.safe_prefix()}_{name.lower()}.csv")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(heads)
                w.writerows(rows)
        except OSError as exc:
            self.log(f"ERROR saving {name}: {exc}")
            return
        self.log(f"{name}: {len(rows)} row(s) saved to {path}")

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
        name = (f"{self.safe_prefix()}_{first:0{width}d}"
                f".{self.data_fmt.get().lower()}")
        # The First label box stays where it was put, so when a previous run has
        # already taken those labels the preview is the only thing that says the
        # sequence will start further along.
        self.seq_next.set(f"next file: {name}" if first == start else
                          f"next file: {name}  ({count} runs from "
                          f"{start:0{width}d} would land on files already there)")

    def do_average(self):
        """Average the current prefix's numbered runs into one file -- a
        small dialog picks the label range, prefilled with what is on disk."""
        outdir, prefix = self.outdir.get(), self.safe_prefix()
        files = sequence_files(outdir, prefix)
        if len(files) < 2:
            return messagebox.showerror(
                "Average sequence",
                f"{prefix} has {len(files)} numbered run(s) in\n{outdir}\n\n"
                f"An average needs at least two ({prefix}_NNN.csv/.npz).")
        labels = list(files)
        dlg = tk.Toplevel(self.root)
        dlg.title("Average sequence")
        dlg.transient(self.root)
        dlg.grab_set()
        fr = ttk.Frame(dlg, padding=10)
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text=f"{prefix}: {len(files)} numbered runs on disk, "
                           f"labels {labels[0]}-{labels[-1]}").grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(fr, text="from label").grid(row=1, column=0, sticky="w",
                                              pady=(8, 0))
        v_first = tk.StringVar(value=labels[0])
        ttk.Entry(fr, textvariable=v_first, width=8).grid(row=1, column=1,
                                                          sticky="w", pady=(8, 0))
        ttk.Label(fr, text="to label").grid(row=1, column=2, sticky="w",
                                            padx=(12, 0), pady=(8, 0))
        v_last = tk.StringVar(value=labels[-1])
        ttk.Entry(fr, textvariable=v_last, width=8).grid(row=1, column=3,
                                                         sticky="w", pady=(8, 0))
        ttk.Label(fr, foreground="#666", justify="left", text=(
            f"Writes <prefix>_avg_<from>-<to>.{self.data_fmt.get().lower()} "
            "beside the runs, plus a .txt\n"
            "made from the first run's, headed by what was averaged. Runs must\n"
            "share columns, point count and time base; a gap is skipped.")).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

        def go():
            try:
                first, last = int(v_first.get()), int(v_last.get())
            except ValueError:
                return messagebox.showerror("Average sequence",
                                            "labels are whole numbers",
                                            parent=dlg)
            dlg.destroy()
            try:
                path, used = average_sequence(outdir, prefix, first, last,
                                              log=self.log,
                                              fmt=self.data_fmt.get())
            except (ValueError, OSError) as e:
                self.log(f"Average sequence: {e}")
                return messagebox.showerror("Average sequence", str(e))
            self.log(f"  averaged runs {used[0]}-{used[-1]} ({len(used)}) -> "
                     f"{os.path.basename(path)}")
            self.refresh_plots()          # 'avg' in the Runs box now resolves

        bb = ttk.Frame(fr)
        bb.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(bb, text="Average", command=go).pack(side="left", fill="x",
                                                        expand=True)
        ttk.Button(bb, text="Cancel", command=dlg.destroy).pack(side="left",
                                                                 padx=(6, 0))

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
        self.seq_first = first
        self.seq_last = first + count - 1
        self.seq_gap = max(0.0, gap)
        self.seq_dither_plan = {}
        if self.seq_dither.get():
            try:
                codes = max(int(float(self.seq_dither_codes.get())), 1)
            except ValueError:
                self.log("Sequence: the dither width must be a number of codes.")
                return
            if count < 2:
                self.log("Sequence: a dither needs at least two runs to average.")
                return
            try:
                self.seq_dither_plan = self.scope.dither_plan(chans, codes)
            except DitherError as err:
                self.log(f"Sequence: could not read CH{err.ch}'s scale/offset "
                         f"for the dither ({err.exc}) - running without it")
                self.seq_dither_plan = {}
            if self.seq_dither_plan:
                self.log("Sequence: dithering " + ", ".join(
                    f"CH{ch} over {span*1e3:.0f} mV ({codes} code{'s' if codes > 1 else ''})"
                    for ch, (_, span) in self.seq_dither_plan.items())
                    + f" across the {count} runs; offsets restored at the end")
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
        """First label from which `count` consecutive captures are all free, so a
        repeated sequence adds to the series instead of overwriting it. This,
        rather than winding the First label box on, is what stacks one sequence
        on the next.

        The whole run has to be clear, not just its first label. Stopping at the
        first gap is what this used to do, and a series with a hole in it - one
        bad run deleted - then started in the hole and wrote straight over
        everything after it."""
        outdir, prefix = self.outdir.get(), self.safe_prefix()
        taken = lambda i: capture_exists(
            os.path.join(outdir, f"{prefix}_{i:0{width}d}"))
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
        if self.seq_dither_plan:
            # evenly spaced across the span, centred on the original offset;
            # the preamble's yorigin carries it, so the CSV volts are true
            failed = self.scope.dither_step(
                self.seq_dither_plan, self.seq_index - self.seq_first,
                self.seq_last - self.seq_first + 1)
            for ch, exc in failed.items():
                self.log(f"  dither: could not set CH{ch} offset ({exc})")
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
        if self.seq_dither_plan:
            failed = self.scope.restore_offsets(self.seq_dither_plan)
            for ch, exc in failed.items():
                off0 = self.seq_dither_plan[ch][0]
                self.log(f"  dither: could not restore CH{ch} offset "
                         f"{off0:+.5g} V ({exc})")
            self.log("  dither: offsets restored")
            self.seq_dither_plan = {}
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


def relaunch_now():
    """Start a fresh Scope Grab. Called after mainloop has returned, so this
    process is holding no VISA session by the time the new one scans.

    pythonw rather than sys.executable: that is python.exe when the app was
    started from a console, and relaunching through it would leave a console
    window behind that was not there before."""
    exe = sys.executable
    gui = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(gui):
        exe = gui
    os.spawnv(os.P_NOWAIT, exe, [f'"{exe}"', f'"{os.path.abspath(__file__)}"'])


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
    # Switching scope rebuilds the whole window, which is done by starting
    # again rather than rebuilding in place. Here, not in restart(), so the old
    # process has released its VISA sessions before the new one looks for them.
    if getattr(app, "relaunch", False):
        try:
            relaunch_now()
        except Exception as exc:
            print(f"could not restart Scope Grab: {exc}", file=sys.stderr)
