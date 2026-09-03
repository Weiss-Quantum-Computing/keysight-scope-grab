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
import csv
import datetime
import io
import json
import os
import queue
import re
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

KTVISA = r"C:\Windows\System32\ktvisa32.dll"
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
# The MSO-X 2014A's 8-bit converter carries a fixed error pattern per code --
# measured 2 Sep 2026 on the Trek monitor: ~3.4 mV pk-pk over a 40.25 mV code
# at 1 V/div, repeating exactly every 40.25 mV of input (10.24 V / 256). A slow
# ramp sweeps through it at slope/code, which lands in the tens of kHz, and
# because it is a function of VOLTAGE an average of identical shots keeps it
# whole. Another scope has another code size: change this for it.
ADC_CODE_PER_VDIV = 0.04025

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
        for ch in ("1", "2", "3", "4"):
            lines.append(f"  CH{ch} {'on ' if ticked.get(ch) else 'off'}"
                         f"  {names.get(ch, '')}".rstrip())
    return "\n".join(lines) + "\n"


# -- averaging a numbered sequence ------------------------------------------
#
# A sequence is N single shots of the same thing. Their mean has the
# shot-to-shot noise down by sqrt(N) and keeps anything that repeats -- which
# is how a ripple that is a few tenths of a millivolt against a millivolt of
# single-shot scatter becomes visible. Pure file work: no instrument, so it
# runs whether or not the scope is connected, and other programs can import it.

def sequence_files(outdir, prefix):
    """The numbered CSVs of a sequence, {label: path}, in run order. Labels
    keep their zero-padding as found, so a series written as _001 reads back
    as '001'."""
    pat = re.compile(re.escape(prefix) + r"_(\d+)\.csv$")
    out = {}
    try:
        for n in os.listdir(outdir):
            m = pat.match(n)
            if m:
                out[m.group(1)] = os.path.join(outdir, n)
    except OSError:
        pass
    return dict(sorted(out.items(), key=lambda kv: int(kv[0])))


def average_sequence(outdir, prefix, first=None, last=None, log=None):
    """Average the numbered runs of `prefix` (optionally labels first..last)
    into <prefix>_avg_<first>-<last>.csv, with a .txt sidecar made from the
    first run's, headed by what was averaged.

    Every run has to carry the same columns, the same point count and the
    same time base -- a sequence taken through one scope setup does, and
    one that changed setup mid-way is refused rather than blended. A gap in
    the series (a deleted run) is skipped and reported. Returns
    (csv_path, [labels used]). Raises ValueError on anything it cannot do.
    """
    say = log or (lambda *_: None)
    files = sequence_files(outdir, prefix)
    if not files:
        raise ValueError(f"no numbered files {prefix}_NNN.csv in {outdir}")
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
        with open(files[lab], "r", encoding="utf-8") as fh:
            head = fh.readline().strip()
        data = np.loadtxt(files[lab], delimiter=",", skiprows=1, ndmin=2)
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
    np.savetxt(base + ".csv", np.column_stack([t0, mean]), delimiter=",",
               header=header, comments="",
               fmt=[TIME_FMT] + [VOLT_FMT] * mean.shape[1])
    side = files[labels[0]][:-4] + ".txt"
    body = open(side, "r", encoding="utf-8").read() if os.path.exists(side) else ""
    with open(base + ".txt", "w", encoding="utf-8") as fh:
        fh.write(f"averaged from      : {n} runs, labels {labels[0]}-{labels[-1]}"
                 + (f" (missing: {', '.join(map(str, missing))})" if missing else "")
                 + "\n")
        fh.write(f"averaging          : mean of the CSV samples per channel; "
                 f"time base from run {labels[0]}; single-shot scatter down by "
                 f"sqrt({n}) = {n ** 0.5:.1f}x\n")
        fh.write(f"settings below are : run {labels[0]}'s\n")
        fh.write(body)
    say(f"{os.path.basename(base)}.csv  (mean of {n} runs, "
        f"{len(t0)} pts x {mean.shape[1]} cols)")
    return base + ".csv", labels


# ---------------------------------------------------------------------------
# Reading captures back: what the plot tabs draw and the ledgers tabulate.
# Everything here works from the files alone, so it serves a capture from any
# folder and needs no instrument.
# ---------------------------------------------------------------------------

def capture_files(outdir, prefix):
    """Every CSV of `prefix` in `outdir`, {run: path} in name order. The run
    is whatever follows the prefix: '003' for a sequence run, '20260903_120000'
    for a one-off, 'avg_001-064' for an averaged sequence. Name order puts a
    sequence in run order and one-offs in capture order."""
    head = prefix + "_"
    out = {}
    try:
        names = sorted(n for n in os.listdir(outdir)
                       if n.lower().endswith(".csv") and n.startswith(head))
    except OSError:
        return out
    for n in names:
        out[n[len(head):-4]] = os.path.join(outdir, n)
    return out


def split_capture_name(path):
    """(prefix, run) of a capture's filename, by the patterns Scope Grab
    writes: prefix_NNN, prefix_YYYYMMDD_HHMMSS[_n], prefix_avg_A-B. A file
    named any other way is its own prefix with no run."""
    stem = os.path.basename(path)
    if stem.lower().endswith(".csv"):
        stem = stem[:-4]
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
    """One capture's CSV in memory, with its .txt sidecar parsed.

    key is the prefix it is known by in the plot boxes, run what follows the
    prefix in its filename. chan maps channel number to column, names carries
    the typed name the header was made from."""

    def __init__(self, path, key, run):
        self.path, self.key, self.run = path, key, run
        with open(path, encoding="utf-8") as fh:
            header = fh.readline().strip()
        self.columns = header.split(",")
        if self.columns[:1] != ["time_s"]:
            raise ValueError("not a Scope Grab CSV: the first column is not time_s")
        data = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
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
        self.meta = read_sidecar(path[:-4] + ".txt")
        self.label = f"{key} {run}" if run else key

    def v(self, ch):
        return self.data[:, self.chan[ch]]


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
        # Cleared with the session: a saved setup stamps itself with scope.idn,
        # and holding the last instrument's name after it has gone would put it
        # on a setup saved with nothing plugged in.
        self.idn = self.addr = ""

    # -- acquisition ------------------------------------------------------

    def single(self, wait_s=10.0, cancelled=None):
        """Arm a single acquisition and wait for it to complete.

        Uses :SINGle rather than :DIGitize so the captured trace stays on the
        scope display - which matters if you also want the screenshot to match
        the data.

        wait_s <= 0 waits indefinitely, which is how a capture is primed before
        an experiment running elsewhere starts sending triggers. `cancelled` is
        polled so a long wait can be called off from the panel.

        Returns True if it triggered, False on timeout, None if cancelled, and
        raises if the scope stops answering the poll.
        """
        self.inst.write(":SINGle")
        started = time.time()
        deadline = None if wait_s <= 0 else started + wait_s
        bad_polls = 0
        while deadline is None or time.time() < deadline:
            if cancelled is not None and cancelled():
                self.inst.write(":STOP")
                return None
            try:
                # Bit 3 of the Operation Status Condition register is the Run bit.
                cond = int(self.inst.query(":OPERegister:CONDition?"))
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
                    self.inst.write(":STOP")
                except Exception:
                    pass
                raise
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
        ever came, -1 if a record was built but the scope would not say how deep
        it is, None if cancelled. In every case but None the scope holds a
        stopped record readable in MAXimum mode (see the worker's read).
        """
        self.inst.query("*OPC?")          # settings writes land before arming
        self.inst.query(":TER?")          # clear the event register of history
        self.inst.write(":DIGitize")
        started = time.time()
        alive = started
        bad_polls = 0
        completed = False     # the run bit cleared on its own = the full count
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
                # Only a real reply says the digitize counted itself out. The
                # give-up path above puts the same False there without asking.
                completed = bad_polls == 0
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
            # No answer is not the same as no hits. The record is there either
            # way - the transfer that follows reads it fine - so a digitize that
            # stopped itself gets its full count, and one that was stopped early
            # says the depth is unknown rather than being thrown away as a run
            # that never triggered.
            return count if completed else -1


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
             if averaging and WAVE_COUNT in settings else []) + (
            # The scope's own measurement results, verbatim, when they were
            # asked for - see the Measurements tab. The scope's format, not
            # ours: label,value pairs, or label,current,min,max,mean,sd,count
            # with statistics on.
            [f"scope measurements : {s(':MEASure:RESults')}"]
            if ":MEASure:RESults" in settings else []) + [
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
        self.prefix_job = None    # debounce for re-pointing the shot browser
        # The plot tabs' state. Captures are cached by path against their file
        # time; the tabs redraw lazily - a capture marks every tab dirty and
        # only the one on show is drawn, the others when they are turned to.
        self.plot_cache = {}      # path -> (mtime, Capture)
        self.plot_tabs = {}       # tab frame -> its draw function
        self.plot_dirty = set()
        self.plot_groups = None   # what the boxes last resolved to
        self.plot_notes_seen = set()
        self.cmp_paths = {}       # compare key -> ["prefix", folder, prefix] | ["file", path]
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
        for var in (self.prefix, self.seq_start, self.seq_count):
            var.trace_add("write", lambda *_: self.show_next_name())
        self.show_next_name()

        self.toggle_existing()
        self.build_settings(left, pad)

        # --- the right column: a notebook of tabs, the log underneath.
        # The screenshot browser is the first tab; the rest draw and tabulate
        # the captured data, the way the ILC panel's tabs do. The plot bar
        # (built with the tabs) sits above the notebook only while a plot tab
        # is showing, so the Screenshot tab is as it always was.
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        shot_tab = ttk.Frame(self.nb)
        self.nb.add(shot_tab, text="Screenshot")

        # --- last screenshot
        self.shot_frame = ttk.LabelFrame(shot_tab, text="Last screenshot")
        self.shot_frame.pack(fill="x", padx=4, pady=4)
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
            "plot_runs": self.plot_runs.get(),
            "plot_compare": self.plot_cmp.get(),
            "plot_show": {str(ch): var.get() for ch, var in self.plot_show.items()},
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
            dark = []
            for ch in chans:
                try:
                    if self.scope.get(f":CHANnel{ch}:DISPlay") in ("0", "OFF"):
                        dark.append(ch)
                except Exception:
                    pass          # unanswerable is not the same as switched off
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
            if self.rec_meas.get():
                # The scope's own measurement results, for the Measurements
                # tab. RESults? reports what is already being measured on the
                # screen and installs nothing; it has not been tried on this
                # scope, so it is asked with the short timeout and the device
                # clear behind it rather than letting an unterminated reply
                # hold a fast sequence for the full VISA timeout.
                meas = self.scope.try_get(":MEASure:RESults")
                if meas:
                    settings[":MEASure:RESults"] = meas
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
        triggers out with :DIGitize and the count reads true afterwards, and a
        build that fell short has already been reported by the caller - so there
        is nothing to warn about here and the depth is simply recorded.

        A trace that was already on the scope is the other one. Under RUN the
        averager is a running average and :WAVeform:COUNt reports the SETTING
        rather than the accumulated depth, so neither a short count nor a full
        one describes what is in the record."""
        if not settings.get(":ACQuire:TYPE", "").upper().startswith("AVER"):
            return
        try:
            got = int(float(settings[WAVE_COUNT]))
            want = int(float(settings[":ACQuire:COUNt"]))
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
        is not packed here - _on_tab_changed puts it above the notebook while a
        data tab is showing and takes it away for the Screenshot tab."""
        bar = self.plot_bar = ttk.LabelFrame(right, text="Plot data")
        row = ttk.Frame(bar)
        row.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(row, text="Runs:").pack(side="left")
        self.plot_runs = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.plot_runs, width=14)
        e.pack(side="left", padx=(4, 10))
        e.bind("<Return>", lambda _e: self.do_plot_redraw())
        ttk.Label(row, text="Compare:").pack(side="left")
        self.plot_cmp = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.plot_cmp, width=24)
        e.pack(side="left", fill="x", expand=True, padx=(4, 6))
        e.bind("<Return>", lambda _e: self.do_plot_redraw())
        ttk.Button(row, text="Add files...",
                   command=self.do_compare_add).pack(side="left")
        self.cmp_clear_btn = ttk.Button(row, text="Clear",
                                        command=self.do_compare_clear)
        self.cmp_clear_btn.pack(side="left", padx=(4, 0))
        ttk.Button(row, text="Redraw",
                   command=self.do_plot_redraw).pack(side="left", padx=(10, 0))

        row2 = ttk.Frame(bar)
        row2.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(row2, text="Show:").pack(side="left")
        self.plot_show = {}
        for ch in (1, 2, 3, 4):
            var = tk.BooleanVar(value=True)
            self.plot_show[ch] = var
            ttk.Checkbutton(row2, text=f"CH{ch}", variable=var,
                            command=self.refresh_plots).pack(side="left",
                                                             padx=(4, 0))
        self.plot_status = ttk.Label(row2, text=PLOT_HINT, foreground="#666")
        self.plot_status.pack(side="left", padx=(14, 0))

        _ctl, self.fig_wave = self._fig_tab("Waveforms", self._plot_waveforms)

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
        self.xy_x, self.xy_y = tk.StringVar(value="CH1"), tk.StringVar(value="CH2")
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

    def _fig_tab(self, name, draw):
        """A tab holding a matplotlib figure with the zoom/pan toolbar, plus a
        strip above it for that tab's own knobs. Returns (strip, figure); the
        figure carries its canvas and toolbar as _canvas and _toolbar the way
        the ILC panel's do. Without matplotlib the tab says so and draws
        nothing, but still counts as a data tab so the bar shows."""
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
        if tab in self.plot_tabs:
            if self.plot_bar.winfo_manager() != "pack":
                self.plot_bar.pack(fill="x", padx=8, pady=(4, 0), before=self.nb)
            if tab in self.plot_dirty:
                self._draw_tab(tab)
        else:
            self.plot_bar.pack_forget()

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
            notes.append(f"{prefix}: no {prefix}_*.csv in {outdir}")
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
                    notes.append(f"{key}: no {pre}_*.csv in {folder}")
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
        self.plot_status.configure(text=elide(text, 110), foreground=colour)
        self.cmp_clear_btn.configure(
            state="normal" if self.plot_cmp.get().strip() or self.cmp_paths
            else "disabled")

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
            filetypes=[("Capture CSV", "*.csv"), ("All files", "*.*")],
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
                      if n.lower().endswith(".csv")}
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
        for ax, ch in zip(axes, chans):
            n = 0
            for cap, col, lw, z in traces:
                if ch in cap.chan:
                    ax.plot(cap.t * scale, cap.v(ch), color=col, lw=lw,
                            zorder=z, label=cap.label)
                    n += 1
            ax.set_ylabel(self._ch_label(ch, traces))
            ax.grid(True, alpha=0.3)
            self._legend(ax, n)
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
        regridded = []
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
                ax.plot(ref.t * scale, other - ref.v(ch), color=col, lw=lw,
                        zorder=z, label=f"{cap.label} - {ref.label}")
                n += 1
            ax.axhline(0, color="#999999", lw=0.6)
            ax.set_ylabel(f"CH{ch} difference (V)")
            ax.grid(True, alpha=0.3)
            self._legend(ax, n)
        if regridded:
            self._plot_note(axes[0], "interpolated onto the reference's time base: "
                            + ", ".join(regridded[:4])
                            + (" ..." if len(regridded) > 4 else ""))
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
        for cap, col, lw, z in usable:
            ax.plot(cap.v(x), cap.v(y), color=col, lw=0.8 * lw, zorder=z,
                    alpha=0.9, label=cap.label)
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

    def do_action(self, scpi, note, confirm=None):
        """Run one of the scope's own buttons - run/stop/single, force trigger,
        clear display, autoscale."""
        if self.busy or self.seq_active or not self.scope.inst:
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
        first = self.first_free(start, width, count)
        name = f"{self.safe_prefix()}_{first:0{width}d}.csv"
        # The First label box stays where it was put, so when a previous run has
        # already taken those labels the preview is the only thing that says the
        # sequence will start further along.
        self.seq_next.set(f"next file: {name}" if first == start else
                          f"next file: {name}  ({count} runs from "
                          f"{start:0{width}d} would land on files already there)")

    def do_average(self):
        """Average the current prefix's numbered runs into one CSV -- a
        small dialog picks the label range, prefilled with what is on disk."""
        outdir, prefix = self.outdir.get(), self.safe_prefix()
        files = sequence_files(outdir, prefix)
        if len(files) < 2:
            return messagebox.showerror(
                "Average sequence",
                f"{prefix} has {len(files)} numbered run(s) in\n{outdir}\n\n"
                f"An average needs at least two ({prefix}_NNN.csv).")
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
            "Writes <prefix>_avg_<from>-<to>.csv beside the runs, plus a .txt\n"
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
                                              log=self.log)
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
            for ch in chans:
                try:
                    scale = float(self.scope.get(f":CHANnel{ch}:SCALe"))
                    off = float(self.scope.get(f":CHANnel{ch}:OFFSet"))
                except Exception as exc:
                    self.log(f"Sequence: could not read CH{ch}'s scale/offset "
                             f"for the dither ({exc}) - running without it")
                    self.seq_dither_plan = {}
                    break
                self.seq_dither_plan[ch] = (off, codes * ADC_CODE_PER_VDIV * scale)
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
        if self.seq_dither_plan:
            # evenly spaced across the span, centred on the original offset;
            # the preamble's yorigin carries it, so the CSV volts are true
            count = self.seq_last - self.seq_first + 1
            k = self.seq_index - self.seq_first
            for ch, (off0, span) in self.seq_dither_plan.items():
                want = off0 + span * ((k + 0.5) / count - 0.5)
                try:
                    self.scope.put(f":CHANnel{ch}:OFFSet", f"{want:.6g}")
                except Exception as exc:
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
            for ch, (off0, _) in self.seq_dither_plan.items():
                try:
                    self.scope.put(f":CHANnel{ch}:OFFSet", f"{off0:.6g}")
                except Exception as exc:
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


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
