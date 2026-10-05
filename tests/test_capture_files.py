"""The capture files: NPZ and CSV, written and read back, and the folder logic
that has to see both.

What matters most is the first group. An NPZ is only worth having if the volts
it rebuilds are the volts the panel computed at grab time - bit for bit, not
'close' - because EOM-ILC's bench loop takes its arrays straight from
Scope.waveform() and an offline analysis of the saved file must not see a
different record from the one the loop saw.

Builds one real App (sandboxed config, no connect) to drive the grab worker
against a fake scope, so it needs a desktop session for Tk. No instrument.

    python tests/test_capture_files.py
"""
import importlib.util
import os
import sys
import tempfile
import tkinter as tk

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import scope_profiles  # noqa: E402
from scope_profiles import Record  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "scope_grab", os.path.join(REPO, "scope_grab.py"))
sg = importlib.util.module_from_spec(spec)
sys.modules["scope_grab"] = sg
spec.loader.exec_module(sg)

# Both BEFORE any App exists - see tests/test_panel.py for what each prevents.
sg.CONFIG_PATH = os.path.join(tempfile.mkdtemp(prefix="scopegrab-test-"),
                              "config.json")
sg.App.do_connect = lambda self: None

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


rng = np.random.default_rng(7)


def msox_record(n=50000, seed_level=30000):
    """Codes the way the MSO-X sends an HRES record in WORD: 16-bit, a slow
    ramp plus noise, crossing the full range so the differences wrap."""
    ramp = np.linspace(-20000, 60000, n)
    codes = (seed_level + ramp + rng.normal(0, 40, n)).astype(np.int64) % 65536
    return Record((2e-7, -0.03, 0.0), codes=codes.astype(np.uint16),
                  y=(1.5723e-6, 32768.0, 1.5e-4))


def roundtrip_checks(tmp):
    print("\nNPZ round trip is exact")
    recs = [msox_record(), msox_record(seed_level=100),
            Record((2e-7, -0.03, 0.0),
                   codes=rng.integers(0, 256, 50000).astype(np.uint8),
                   y=(0.04, 127.0 + 3.0, 0.0)),               # Rigol-style
            Record((2e-7, -0.03, 0.0), volts=rng.normal(0, 1e-3, 50000))]
    cols = ["time_s", "CH1_a_V", "CH2_b_V", "CH3_V", "CH4_avg_V"]
    path = sg.write_npz(os.path.join(tmp, "x_001.npz"), cols, recs[0].x, recs)
    got_cols, data = sg.load_capture(path)
    check("columns come back", got_cols == cols, str(got_cols))
    check("time is bit-identical to Record.t()",
          np.array_equal(data[:, 0], recs[0].t()))
    for j, r in enumerate(recs, 1):
        check(f"column {j} bit-identical to Record.v()",
              np.array_equal(data[:, j], r.v()))
    check("no .part left behind", not os.path.exists(path + ".part"))
    with np.load(path, allow_pickle=False) as z:
        check("loads without pickle", "format" in z.files)
        check("codes stored in their own dtype",
              z["y1"].dtype == np.uint16 and z["y3"].dtype == np.uint8)

    print("\nthe profile's read_waveform is unchanged by going through Record")
    r = recs[0]
    xinc, xorig, xref = r.x
    yinc, yref, yorig = r.y
    old_t = (np.arange(len(r.codes)) - xref) * xinc + xorig
    old_v = (r.codes.astype(np.float64) - yref) * yinc + yorig
    check("MSO-X t and v as the old code computed them",
          np.array_equal(r.t(), old_t) and np.array_equal(r.v(), old_v))
    raw = rng.integers(0, 256, 1000)
    yinc, yorig, yref = 0.04, 3.0, 127.0
    rig = Record((1e-6, 0.0, 0.0), codes=raw.astype(np.uint8),
                 y=(yinc, yref + yorig, 0.0))
    check("Rigol v as the old code computed it",
          np.array_equal(rig.v(), (raw.astype(np.float64) - yref - yorig) * yinc))

    print("\nsize")
    csvp = sg.write_csv(os.path.join(tmp, "x_002.csv"), cols,
                        np.column_stack([recs[0].t()] + [q.v() for q in recs]))
    a, b = os.path.getsize(csvp), os.path.getsize(path)
    check("NPZ is much smaller than the CSV", b * 4 < a,
          f"{a / 1e6:.2f} MB csv vs {b / 1e6:.2f} MB npz")
    _, cdata = sg.load_capture(csvp)
    check("the CSV agrees to its 7 digits",
          np.allclose(cdata, data, rtol=1e-6, atol=1e-12))

    print("\nnot a capture")
    junk = os.path.join(tmp, "junk.npz")
    np.savez(junk, t=np.zeros(3), y=np.zeros(3))      # EOM-ILC's own npz shape
    try:
        sg.load_capture(junk)
        check("an untagged npz is refused", False)
    except ValueError as e:
        check("an untagged npz is refused", "not a Scope Grab" in str(e))


def folder_checks(tmp):
    print("\nfolders holding both formats")
    d = os.path.join(tmp, "seq")
    os.makedirs(d)
    cols = ["time_s", "CH1_V"]
    t = np.arange(100) * 1e-6
    for i, fmt in [(1, "csv"), (2, "npz"), (3, "csv"), (3, "npz")]:
        v = np.full(100, float(i))
        base = os.path.join(d, f"run_{i:03d}")
        if fmt == "csv":
            sg.write_csv(base + ".csv", cols, np.column_stack([t, v]))
        else:
            sg.write_npz(base + ".npz", cols, t, [v])
        with open(base + ".txt", "w") as fh:
            fh.write("captured : x\n")
    seq = sg.sequence_files(d, "run")
    check("sequence_files sees both formats", list(seq) == ["001", "002", "003"])
    check("a run in both formats is read from its NPZ",
          seq["003"].endswith(".npz"))
    caps = sg.capture_files(d, "run")
    check("capture_files sees both formats", list(caps) == ["001", "002", "003"],
          str(list(caps)))
    check("split_capture_name strips .npz",
          sg.split_capture_name(seq["002"]) == ("run", "002"))
    check("free_base sees an NPZ", sg.free_base(os.path.join(d, "run_002"))
          == os.path.join(d, "run_002_2"))
    for fmt in ("NPZ", "CSV"):
        path, used = sg.average_sequence(d, "run", fmt=fmt)
        _, avg = sg.load_capture(path)
        check(f"average of a mixed sequence, written as {fmt}",
              path.endswith("." + fmt.lower()) and np.allclose(avg[:, 1], 2.0),
              os.path.basename(path))
    cap = sg.Capture(seq["002"], "run", "002")
    check("Capture reads an NPZ and its sidecar",
          cap.chan == {1: 1} and cap.meta.get("captured") == "x")


class FakeScope:
    """Just what _grab_worker asks of a Scope, with a real Record behind
    record() so the file it writes can be checked against the arrays."""

    def __init__(self, recs):
        self.recs, self.prof = recs, None

    def is_displayed(self, ch):
        return True

    def averaging_depth(self):
        return None

    def freeze(self):
        return False

    def run(self):
        pass

    def single(self, wait_s, cancelled):
        return True

    def transfer_plan(self, averaged, points):
        return "RAW", points

    def record(self, ch, points_mode="RAW", points=None):
        return self.recs[ch]

    def waveform(self, ch, points_mode="RAW", points=None):
        return self.recs[ch].t(), self.recs[ch].v()

    def hit_count(self):
        return None

    def screenshot(self):
        return None

    def metadata(self, chans, settings, names, label, existing):
        return "captured : fake\n"


def grab_checks(tmp):
    print("\nthe grab worker, both formats")
    root = tk.Tk()
    root.withdraw()
    app = sg.App(root)
    recs = {1: msox_record(20000), 2: msox_record(20000, seed_level=500)}
    app.scope = FakeScope(recs)
    real_prof = app.prof
    app.prof = type("P", (), {"meas_results": None, "wave_count": "count"})()
    app.read_all_settings = lambda: {}
    app.report_averaging = lambda *a, **k: None
    app.save_png.set(False)
    app.rec_meas.set(False)
    check("the Data box defaults to NPZ", app.data_fmt.get() == "NPZ")
    out = os.path.join(tmp, "grab")
    app.outdir.set(out)
    app.prefix.set("g")
    for ch, var in app.ch_vars.items():
        var.set(ch in recs)
    for fmt, label in (("NPZ", "001"), ("CSV", "002")):
        app.data_fmt.set(fmt)
        app._grab_worker([1, 2], label=label)
        root.update()
        path = os.path.join(out, f"g_{label}.{fmt.lower()}")
        check(f"{fmt}: file written", os.path.exists(path))
        if not os.path.exists(path):
            continue
        cols, data = sg.load_capture(path)
        want = np.column_stack([recs[1].t(), recs[1].v(), recs[2].v()])
        same = (np.array_equal(data, want) if fmt == "NPZ"
                else np.allclose(data, want, rtol=1e-6, atol=1e-15))
        check(f"{fmt}: what Scope.waveform() would have given",
              same and cols[0] == "time_s" and len(cols) == 3)
        check(f"{fmt}: sidecar written",
              os.path.exists(os.path.join(out, f"g_{label}.txt")))
    app.data_fmt.set("NPZ")
    app._grab_worker([1, 2], label="001")
    check("a taken label is not reused across formats",
          os.path.exists(os.path.join(out, "g_001_2.npz")))
    app.seq_start.set("1")
    app.seq_count.set("2")
    check("the sequence counts an NPZ as taken", app.first_free(1, 3, 2) == 3)
    app.prof = real_prof
    app.data_fmt.set("CSV")
    app.save_config()
    app.data_fmt.set("NPZ")
    app.load_config()
    check("the Data box survives a restart", app.data_fmt.get() == "CSV")
    root.destroy()


def converter_checks(tmp):
    """tools/csv_to_npz.py on the cases that broke its first versions: a 0-5 V
    trigger at 1 V/div (2000 codes, values printed from 1e-3 to 5 V), a fine
    channel, and a time column crossing zero."""
    print("\ncsv_to_npz")
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import csv_to_npz as conv
    d = os.path.join(tmp, "conv")
    os.makedirs(d)
    n = 100000
    step = 1 / 398                                     # the MSO-X HRES step at 1 V/div
    trig = np.where(np.arange(n) % 20000 < 2000, 2002, 1) + rng.integers(0, 3, n)
    fine = 900 + np.cumsum(rng.integers(-2, 3, n)) % 600
    recs = [Record((2e-8, -9.38055687e-05, 0.0), codes=trig.astype(np.uint16),
                   y=(step, 0.0, 0.00342029)),
            Record((2e-8, -9.38055687e-05, 0.0), codes=fine.astype(np.uint16),
                   y=(step / 100, 0.0, -0.0314327))]
    cols = ["time_s", "CH1_V", "CH2_trigger_V"]
    data = np.column_stack([recs[0].t(), recs[1].v(), recs[0].v()])
    csvp = sg.write_csv(os.path.join(d, "c_001.csv"), cols, data)
    sg.write_csv(os.path.join(d, "c_avg_001-002.csv"), cols,
                 np.column_stack([data[:, 0], data[:, 1:]       # off the lattice
                                  + rng.normal(0, 1e-4, (n, 2))]))
    with open(os.path.join(d, "table.csv"), "w") as fh:
        fh.write("angle,ratio\n1,2\n")
    r = conv.convert_one(csvp, write=True, delete=True)
    check("a capture converts and its CSV goes", r["status"] == "converted, CSV removed"
          and not os.path.exists(csvp), r["status"])
    check("time and both channels land on a grid", r.get("how") == "t:grid u16 u16",
          r.get("how"))
    _, back = sg.load_capture(csvp[:-4] + ".npz")
    check("the codes found are the codes written",
          np.allclose(back, data, rtol=2e-7, atol=1e-12))
    r = conv.convert_one(os.path.join(d, "c_avg_001-002.csv"), write=True, delete=False)
    check("an off-lattice average is kept exact as float64",
          r["status"] == "converted" and "f64" in r.get("how", ""), r.get("how"))
    r = conv.convert_one(os.path.join(d, "table.csv"), write=True, delete=True)
    check("a non-capture CSV is left alone", r["status"] == "not a capture"
          and os.path.exists(os.path.join(d, "table.csv")))


def main():
    tmp = tempfile.mkdtemp(prefix="scopegrab-files-")
    roundtrip_checks(tmp)
    folder_checks(tmp)
    converter_checks(tmp)
    grab_checks(tmp)
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("Capture files OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
