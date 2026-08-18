#!/usr/bin/env python3
"""
Scope Grab - one-click capture from a Keysight InfiniiVision MSO-X 2014A.

Click a button, get a timestamped CSV of the waveform, a PNG of the screen,
and a metadata text file in your chosen folder. No licenses, no BenchVue.

Requires: Keysight IO Libraries Suite + `pip install pyvisa numpy pillow`
          (pillow only sharpens the screenshot preview - the rest works without it)
Run with:  pythonw scope_grab.py      (pythonw = no console window)
"""

import datetime
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

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
GLOBAL_SETTINGS = [
    ("Timebase s/div", ":TIMebase:SCALe", "num", None),
    ("Position (s)", ":TIMebase:POSition", "num", None),
    ("Trigger source", ":TRIGger:EDGE:SOURce", "choice",
     ("CHAN1", "CHAN2", "CHAN3", "CHAN4", "EXT", "LINE", "WGEN")),
    ("Trigger level (V)", ":TRIGger:EDGE:LEVel", "num", None),
    ("Trigger slope", ":TRIGger:EDGE:SLOPe", "choice", ("POS", "NEG", "EITH")),
    ("Acquisition", ":ACQuire:TYPE", "choice", ("NORM", "AVER", "HRES", "PEAK")),
]
# Read-only values, refreshed on the same pass as the settings above. They are
# per-acquisition results rather than knobs, so the panel shows them but never
# writes them.
INFO_SETTINGS = [
    ("Sample rate (Sa/s)", ":ACQuire:SRATe"),
    ("Points acquired", ":ACQuire:POINts"),
]
CHANNEL_SETTINGS = [
    ("V/div", ":CHANnel{ch}:SCALe", "num", None),
    ("Offset", ":CHANnel{ch}:OFFSet", "num", None),
    ("Coupling", ":CHANnel{ch}:COUPling", "choice", ("AC", "DC")),
    ("Probe", ":CHANnel{ch}:PROBe", "num", None),
    ("BW lim", ":CHANnel{ch}:BWLimit", "bool", ("ON", "OFF")),
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

    def waveform(self, channel, points_mode="RAW", points=None):
        w = self.inst
        w.write(f":WAVeform:SOURce CHANnel{channel}")
        w.write(f":WAVeform:POINts:MODE {points_mode}")
        # Setting the mode resets the point count, so ask for it afterwards. The
        # scope rounds to a value it likes; the preamble read below reports what
        # it actually gave, so the time axis stays right either way.
        if points:
            w.write(f":WAVeform:POINts {points}")
        w.write(":WAVeform:FORMat BYTE")
        w.write(":WAVeform:UNSigned ON")

        pre = w.query(":WAVeform:PREamble?").strip().split(",")
        xinc, xorig, xref = float(pre[4]), float(pre[5]), float(pre[6])
        yinc, yorig, yref = float(pre[7]), float(pre[8]), float(pre[9])

        raw = w.query_binary_values(":WAVeform:DATA?", datatype="B",
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

    def metadata(self, channels, settings, names=None, label=None):
        """Format the metadata file. `settings` is the raw {scpi root: reply}
        snapshot already read for the panel, so a grab only asks the scope once
        and the file describes the same instant the panel shows. Values are the
        instrument's own strings, unrounded."""
        s = lambda scpi: settings.get(scpi, "?")
        lines = [
            f"captured           : {datetime.datetime.now().isoformat()}",
            f"instrument         : {self.idn}",
            f"visa address       : {self.addr}",
        ] + ([f"sequence label     : {label}"] if label else []) + [
            f"sample rate (Sa/s) : {s(':ACQuire:SRATe')}",
            f"points acquired    : {s(':ACQuire:POINts')}",
            f"acquisition type   : {s(':ACQuire:TYPE')}",
            f"timebase s/div     : {s(':TIMebase:SCALe')}",
            f"timebase position  : {s(':TIMebase:POSition')}",
            f"trigger source     : {s(':TRIGger:EDGE:SOURce')}",
            f"trigger level      : {s(':TRIGger:EDGE:LEVel')}",
            f"trigger slope      : {s(':TRIGger:EDGE:SLOPe')}",
        ]
        for ch in channels:
            if names and names.get(ch):
                lines.append(f"CH{ch} name          : {names[ch]}")
            lines += [
                f"CH{ch} V/div         : {s(f':CHANnel{ch}:SCALe')}",
                f"CH{ch} offset        : {s(f':CHANnel{ch}:OFFSet')}",
                f"CH{ch} coupling      : {s(f':CHANnel{ch}:COUPling')}",
                f"CH{ch} probe atten   : {s(f':CHANnel{ch}:PROBe')}",
                f"CH{ch} bandwidth lim : {s(f':CHANnel{ch}:BWLimit')}",
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

        root.title("Scope Grab - MSO-X 2014A")
        # Tall enough for the screenshot preview, but never taller than the
        # screen - otherwise the log ends up behind the taskbar.
        win_w = min(1160, root.winfo_screenwidth() - 80)
        win_h = min(880, root.winfo_screenheight() - 120)
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

        tf = ttk.Frame(left)
        tf.pack(fill="x", **pad)
        ttk.Label(tf, text="Wait for trigger:").pack(side="left")
        self.trig_wait = tk.StringVar(value="10")
        ttk.Entry(tf, textvariable=self.trig_wait, width=7).pack(side="left", padx=4)
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
        self.preview_img = None
        self.preview_path = None

        # --- log
        lf = ttk.LabelFrame(right, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(lf, height=6, wrap="none", font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True, padx=4, pady=4)

        root.bind("<space>", self.on_space)
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
            self.logbox.insert("end", self.msgs.get() + "\n")
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

        for key, var in (("outdir", self.outdir), ("prefix", self.prefix),
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

        self.saved_cfg = self.current_cfg()
        self.log(f"Restored last session from {CONFIG_PATH}")
        if not os.path.isdir(self.outdir.get()):
            self.log(f"  (that folder does not exist yet: {self.outdir.get()})")

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

    def on_space(self, event):
        try:
            cls = event.widget.winfo_class()
        except AttributeError:
            cls = ""
        if cls in SPACE_OWNERS:
            return
        self.do_grab()

    def safe_prefix(self):
        p = "".join("_" if c in BAD_NAME_CHARS else c
                    for c in self.prefix.get()).strip()
        return p or "scope"

    def show_preview(self, path):
        """Put a PNG in the preview box. Main thread only (Tk images are not
        thread safe)."""
        try:
            if Image is not None:
                im = Image.open(path)
                im.load()
                k = min(PREVIEW_W / im.width, PREVIEW_H / im.height, 1.0)
                if k < 1.0:
                    im = im.resize((max(1, round(im.width * k)),
                                    max(1, round(im.height * k))),
                                   Image.LANCZOS)
                img = ImageTk.PhotoImage(im)
            else:
                img = tk.PhotoImage(file=path)     # Tk 8.6 reads PNG natively
                k = 1
                while img.width() // k > PREVIEW_W or img.height() // k > PREVIEW_H:
                    k += 1
                if k > 1:
                    img = img.subsample(k)         # integer factors only
        except Exception as exc:
            self.log(f"  (preview failed: {exc})")
            return
        self.preview_img = img            # keep a reference or Tk drops it
        self.preview_path = path
        self.preview.configure(image=img, text="")
        self.shot_frame.configure(
            text=f"Last screenshot - {os.path.basename(path)}  "
                 f"(double-click to open full size)")

    def load_latest_preview(self):
        outdir = self.outdir.get()
        try:
            shots = [os.path.join(outdir, n) for n in os.listdir(outdir)
                     if n.lower().endswith(".png")]
        except OSError:
            return
        if shots:
            self.show_preview(max(shots, key=os.path.getmtime))

    def open_preview(self, _event=None):
        if self.preview_path:
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
        for btn in (self.read_btn, self.apply_btn):
            btn.configure(state=state)
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

    def cancel_grab(self):
        self.stop_flag.set()
        self.log("Cancel requested - takes effect while waiting for a trigger; "
                 "a transfer already under way will finish.")

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

            wait_s = self.trigger_wait_s()
            self.set_phase("waiting for trigger" + ("" if wait_s else " (no limit)"))
            armed_at = time.time()
            triggered = self.scope.single(wait_s=wait_s,
                                          cancelled=self.stop_flag.is_set)
            t_armed = time.time() - armed_at
            if triggered is None:
                self.log("  cancelled while waiting for a trigger - nothing saved")
                self.scope.run()
                return
            if triggered is False:
                self.log(f"  no trigger within {wait_s:g} s - nothing saved. Raise "
                         f"'Wait for trigger', or set it to 0 to wait indefinitely.")
                self.scope.run()
                return
            # Everything that needs the instrument happens first, so the scope
            # goes live again before the slow business of writing files. It is
            # not armed during either phase - see the missed-trigger note below.
            self.set_phase("reading from scope")
            read_at = time.time()
            points = self.transfer_points()
            names = {ch: self.ch_names[ch].get().strip() for ch in chans}
            cols = {}
            for ch in chans:
                t, v = self.scope.waveform(ch, points=points)
                if "time_s" not in cols:
                    cols["time_s"] = t
                cols[self.column_name(ch)] = v

            # One settings read per grab: the panel and the metadata file are
            # built from the same snapshot.
            settings = self.read_all_settings()
            # The screenshot has to be taken before :RUN, while the captured
            # trace is still the one on screen.
            img = self.scope.screenshot() if self.save_png.get() else None
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
                fh.write(self.scope.metadata(chans, settings, names, label))
            self.grab_wrote = True

            if img is not None:
                png_path = base + ".png"
                with open(png_path, "wb") as fh:
                    fh.write(img)
                self.log(f"{os.path.basename(png_path)}  ({len(img)} bytes)")
                self.root.after(0, lambda p=png_path: self.show_preview(p))
            t_write = time.time() - write_at

            self.root.after(0, lambda v=settings: self.show_settings(v))
            self.log(f"  {'run ' + label if label else 'grab'}: "
                     f"{t_armed:.1f} s waiting for the trigger, "
                     f"{t_read:.1f} s reading, {t_write:.1f} s writing "
                     f"({t_armed + t_read + t_write:.1f} s total)")
            if t_armed < 0.5:
                # The scope cannot be armed while it is being read out, so a
                # trigger that is already pending the moment it re-arms means
                # earlier ones came and went unrecorded.
                self.log("  ! a trigger was already waiting when the scope armed: "
                         "triggers are arriving faster than a run takes, so some "
                         "are being missed. Lower 'Transfer points', or slow the "
                         "source to more than "
                         f"{t_read + t_write:.0f} s between triggers.")
            self.root.after(0, self.save_config)
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            self.root.after(0, self.grab_done)

    # -- settings panel ---------------------------------------------------

    def build_settings(self, parent, pad):
        self.set_vars = {}        # scpi root -> StringVar shown in the panel
        self.set_marks = {}       # scpi root -> "edited" marker label
        self.set_kinds = {}       # scpi root -> num/choice/bool
        self.set_scope = {}       # scpi root -> value the scope last reported
        self.read_stamp = ""      # when the panel last matched the instrument

        sf = ttk.LabelFrame(parent, text="Scope settings")
        sf.pack(fill="x", **pad)

        g = ttk.Frame(sf)
        g.pack(fill="x", padx=6, pady=(6, 2))
        for i, (label, scpi, kind, choices) in enumerate(GLOBAL_SETTINGS):
            row, col = divmod(i, 2)
            ttk.Label(g, text=label + ":").grid(row=row, column=col * 2,
                                                sticky="e", padx=(0, 4), pady=2)
            self.setting_widget(g, scpi, kind, choices, row, col * 2 + 1, 11)

        for i, (label, scpi) in enumerate(INFO_SETTINGS):
            row = len(GLOBAL_SETTINGS) // 2 + i // 2
            col = (i % 2) * 2
            ttk.Label(g, text=label + ":").grid(row=row, column=col, sticky="e",
                                                padx=(0, 4), pady=2)
            self.setting_widget(g, scpi, "info", None, row, col + 1, 11)

        c = ttk.Frame(sf)
        c.pack(fill="x", padx=6, pady=(6, 2))
        for j, (label, _, _, _) in enumerate(CHANNEL_SETTINGS):
            ttk.Label(c, text=label).grid(row=0, column=j + 1, pady=(0, 2))
        for i, ch in enumerate((1, 2, 3, 4)):
            ttk.Label(c, text=f"CH{ch}").grid(row=i + 1, column=0, sticky="e", padx=(0, 4))
            for j, (_, tmpl, kind, choices) in enumerate(CHANNEL_SETTINGS):
                self.setting_widget(c, tmpl.format(ch=ch), kind, choices, i + 1, j + 1, 8)

        bar = ttk.Frame(sf)
        bar.pack(fill="x", padx=6, pady=(2, 6))
        self.read_btn = ttk.Button(bar, text="Read from scope",
                                   command=self.do_read_settings, state="disabled")
        self.read_btn.pack(side="left")
        self.apply_btn = ttk.Button(bar, text="Apply changes",
                                    command=self.do_apply_settings, state="disabled")
        self.apply_btn.pack(side="left", padx=6)
        self.set_status = ttk.Label(bar, text="not read yet", foreground="#666")
        self.set_status.pack(side="left", padx=6)

    def setting_widget(self, parent, scpi, kind, choices, row, col, width):
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=col, sticky="w", padx=2, pady=1)
        var = tk.StringVar()
        if kind == "info":
            # read-only: no entry to edit, no edited-marker, never written back
            ttk.Label(cell, textvariable=var, width=width).pack(side="left")
        elif kind == "num":
            ttk.Entry(cell, textvariable=var, width=width).pack(side="left")
        else:
            ttk.Combobox(cell, textvariable=var, values=list(choices),
                         width=max(4, width - 3), state="readonly").pack(side="left")
        if kind != "info":
            mark = ttk.Label(cell, text=" ", width=1, foreground="#c60")
            mark.pack(side="left")
            self.set_marks[scpi] = mark
            var.trace_add("write", lambda *_: self.refresh_marks())
        self.set_vars[scpi] = var
        self.set_kinds[scpi] = kind
        self.set_scope[scpi] = ""

    def edited(self, scpi):
        """True if the panel value differs from what the scope last reported."""
        return self.set_vars[scpi].get().strip() != self.set_scope[scpi]

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
        self.refresh_marks()

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
        if self.busy or not self.scope.inst:
            return
        changes = {scpi: var.get().strip() for scpi, var in self.set_vars.items()
                   if self.edited(scpi)}
        if not changes:
            self.log("No setting changes to apply.")
            return
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(changes,), daemon=True).start()

    def _settings_worker(self, changes):
        try:
            if changes:
                for scpi, value in changes.items():
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
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            # Settings I/O must not advance a sequence - only a grab does that.
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
        self.seq_next.set(f"next file: {self.safe_prefix()}_{start:0{width}d}.csv")

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
        if self.auto.get():          # only one repeating mechanism at a time
            self.auto.set(False)
            self.toggle_auto()
            self.log("Sequence: switched auto-grab off.")

        self.seq_width = max(3, len(str(start + count - 1)))
        first = self.first_free(start)
        if first != start:
            self.log(f"Sequence: {self.safe_prefix()}_{start:0{self.seq_width}d}.csv "
                     f"already exists, starting at {first:0{self.seq_width}d} instead")
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

    def first_free(self, start):
        """First label whose CSV does not exist yet, so a repeated sequence adds
        to the series instead of overwriting it."""
        outdir, prefix = self.outdir.get(), self.safe_prefix()
        i = start
        while os.path.exists(os.path.join(outdir, f"{prefix}_{i:0{self.seq_width}d}.csv")):
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
            elif label is not None:
                # It was called off before saving, so that label is still free.
                self.seq_start.set(str(int(label)))
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
        used = (not aborted) or self.busy   # a run mid-flight still writes its label
        self.seq_start.set(str(self.seq_index + (1 if used else 0)))

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
