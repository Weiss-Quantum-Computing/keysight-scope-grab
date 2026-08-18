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
NOT_MEASURED = 9.9e37

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

    def single(self, wait_s=10.0):
        """Arm a single acquisition and wait for it to complete.

        Uses :SINGle rather than :DIGitize so the captured trace stays on the
        scope display - which matters if you also want the screenshot to match
        the data. Returns True if it triggered, False if it timed out and we
        forced a stop.
        """
        self.inst.write(":SINGle")
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                # Bit 3 of the Operation Status Condition register is the Run bit.
                cond = int(self.inst.query(":OPERegister:CONDition?"))
            except Exception:
                time.sleep(min(wait_s, 1.0))
                return True
            if not (cond & 8):
                return True
            time.sleep(0.05)
        self.inst.write(":STOP")
        return False

    def waveform(self, channel, points_mode="RAW"):
        w = self.inst
        w.write(f":WAVeform:SOURce CHANnel{channel}")
        w.write(f":WAVeform:POINts:MODE {points_mode}")
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

    def metadata(self, channels):
        q = self.inst.query
        lines = [
            f"captured           : {datetime.datetime.now().isoformat()}",
            f"instrument         : {self.idn}",
            f"visa address       : {self.addr}",
            f"sample rate (Sa/s) : {q(':ACQuire:SRATe?').strip()}",
            f"points acquired    : {q(':ACQuire:POINts?').strip()}",
            f"acquisition type   : {q(':ACQuire:TYPE?').strip()}",
            f"timebase s/div     : {q(':TIMebase:SCALe?').strip()}",
            f"timebase position  : {q(':TIMebase:POSition?').strip()}",
            f"trigger source     : {q(':TRIGger:EDGE:SOURce?').strip()}",
            f"trigger level      : {q(':TRIGger:EDGE:LEVel?').strip()}",
            f"trigger slope      : {q(':TRIGger:EDGE:SLOPe?').strip()}",
        ]
        for ch in channels:
            lines += [
                f"CH{ch} V/div         : {q(f':CHANnel{ch}:SCALe?').strip()}",
                f"CH{ch} offset        : {q(f':CHANnel{ch}:OFFSet?').strip()}",
                f"CH{ch} coupling      : {q(f':CHANnel{ch}:COUPling?').strip()}",
                f"CH{ch} probe atten   : {q(f':CHANnel{ch}:PROBe?').strip()}",
                f"CH{ch} bandwidth lim : {q(f':CHANnel{ch}:BWLimit?').strip()}",
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

        root.title("Scope Grab - MSO-X 2014A")
        # Tall enough for the screenshot preview, but never taller than the
        # screen - otherwise the log ends up behind the taskbar.
        win_h = min(870, root.winfo_screenheight() - 120)
        root.geometry(f"620x{win_h}+60+20")

        pad = dict(padx=8, pady=4)

        # --- connection row
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        self.status = ttk.Label(top, text="Not connected", foreground="#a00")
        self.status.pack(side="left")
        ttk.Button(top, text="Connect", command=self.do_connect).pack(side="right")

        # --- channels
        chf = ttk.LabelFrame(root, text="Channels")
        chf.pack(fill="x", **pad)
        self.ch_vars = {}
        for ch in (1, 2, 3, 4):
            v = tk.BooleanVar(value=(ch == 1))
            ttk.Checkbutton(chf, text=f"CH{ch}", variable=v).pack(side="left", padx=10, pady=6)
            self.ch_vars[ch] = v

        # --- output folder + prefix
        of = ttk.LabelFrame(root, text="Save to")
        of.pack(fill="x", **pad)
        default_dir = os.path.join(os.path.expanduser("~"), "Desktop", "scope_data")
        self.outdir = tk.StringVar(value=default_dir)
        ttk.Entry(of, textvariable=self.outdir).pack(side="left", fill="x",
                                                    expand=True, padx=6, pady=6)
        ttk.Button(of, text="...", width=3, command=self.pick_dir).pack(side="left", padx=6)

        pf = ttk.Frame(root)
        pf.pack(fill="x", **pad)
        ttk.Label(pf, text="Filename prefix:").pack(side="left")
        self.prefix = tk.StringVar(value="scope")
        ttk.Entry(pf, textvariable=self.prefix, width=20).pack(side="left", padx=6)
        self.save_png = tk.BooleanVar(value=True)
        ttk.Checkbutton(pf, text="also save screenshot",
                        variable=self.save_png).pack(side="left", padx=12)

        # --- grab
        gf = ttk.Frame(root)
        gf.pack(fill="x", **pad)
        self.grab_btn = ttk.Button(gf, text="GRAB  (or press Space)",
                                   command=self.do_grab, state="disabled")
        self.grab_btn.pack(side="left", fill="x", expand=True, ipady=8)

        af = ttk.Frame(root)
        af.pack(fill="x", **pad)
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(af, text="Auto-grab every", variable=self.auto,
                        command=self.toggle_auto).pack(side="left")
        self.interval = tk.StringVar(value="10")
        ttk.Entry(af, textvariable=self.interval, width=6).pack(side="left", padx=4)
        ttk.Label(af, text="seconds").pack(side="left")

        # --- last screenshot
        self.shot_frame = ttk.LabelFrame(root, text="Last screenshot")
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
        lf = ttk.LabelFrame(root, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(lf, height=6, wrap="none", font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True, padx=4, pady=4)

        root.bind("<space>", self.on_space)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.pump)
        self.root.after(300, self.do_connect)
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

    def channels(self):
        return [ch for ch, v in self.ch_vars.items() if v.get()]

    def set_busy(self, busy):
        self.busy = busy
        self.grab_btn.configure(state="disabled" if busy or not self.scope.inst else "normal")

    # -- actions ----------------------------------------------------------

    def do_connect(self):
        def work():
            try:
                idn = self.scope.connect()
                self.root.after(0, lambda: self.status.configure(
                    text=idn[:70], foreground="#060"))
                self.log(f"Connected: {idn}")
                self.log(f"Address:   {self.scope.addr}")
                self.root.after(0, lambda: self.grab_btn.configure(state="normal"))
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
        self.set_busy(True)
        threading.Thread(target=self._grab_worker, args=(chans,), daemon=True).start()

    def _grab_worker(self, chans):
        try:
            outdir = self.outdir.get()
            os.makedirs(outdir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.join(outdir, f"{self.safe_prefix()}_{stamp}")

            triggered = self.scope.single(wait_s=10.0)
            if not triggered:
                self.log("  (no trigger within 10 s - forced stop, "
                         "reading memory contents)")

            cols = {}
            for ch in chans:
                t, v = self.scope.waveform(ch)
                if "time_s" not in cols:
                    cols["time_s"] = t
                cols[f"CH{ch}_V"] = v

            data = np.column_stack([cols[k] for k in cols])
            csv_path = base + ".csv"
            np.savetxt(csv_path, data, delimiter=",",
                       header=",".join(cols.keys()), comments="")
            self.log(f"{os.path.basename(csv_path)}  "
                     f"({data.shape[0]} pts x {data.shape[1]} cols)")

            with open(base + ".txt", "w") as fh:
                fh.write(self.scope.metadata(chans))

            if self.save_png.get():
                img = self.scope.screenshot()
                png_path = base + ".png"
                with open(png_path, "wb") as fh:
                    fh.write(img)
                self.log(f"{os.path.basename(png_path)}  ({len(img)} bytes)")
                self.root.after(0, lambda p=png_path: self.show_preview(p))

            self.scope.run()
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            self.root.after(0, lambda: self.set_busy(False))

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
        self.auto.set(False)
        self.toggle_auto()
        self.scope.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
