"""Driving a Scope without the panel: the API another program uses.

The ramp polarimeter loads scope_grab.py by path and drives a Scope directly -
settings snapshot, offset dither, capture files - so those pieces live outside
App. This checks that each one does what the panel's own path does: the same
settings roots, the same dither offsets, the same files.

Builds one real App (sandboxed config, no connect) to compare against, so it
needs a desktop session for Tk. No instrument.

    python tests/test_headless.py
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


class FakeInst:
    """A VISA session that answers from a dict and records every write."""

    def __init__(self, replies, refuse=()):
        self.replies, self.refuse, self.writes = dict(replies), set(refuse), []

    def query(self, text):
        root = text[:-1]
        if root in self.refuse:
            raise RuntimeError("timeout")
        return self.replies.get(root, "0") + "\n"

    def write(self, text):
        root = text.split(" ", 1)[0]
        if root in self.refuse:
            raise RuntimeError("refused")
        self.writes.append(text)


def scope_with(prof, replies, refuse=()):
    s = sg.Scope(prof)
    s.inst = FakeInst(replies, refuse)
    return s


def settings_checks():
    print("\nsettings snapshot without the panel")
    root = tk.Tk()
    root.withdraw()
    app = sg.App(root)
    roots = sg.setting_roots(app.prof)
    check("setting_roots matches what the panel reads",
          set(roots) == set(app.set_kinds),
          f"{len(roots)} roots vs {len(app.set_kinds)}")
    root.destroy()
    for key, prof in scope_profiles.PROFILES.items():
        roots = set(sg.setting_roots(prof))
        meta = ([s for _, s in prof.meta_head] + [s for _, s in prof.meta_tail]
                + [t.format(ch=c) for c in prof.channels
                   for _, t in prof.meta_channel]
                + [prof.acq_type, prof.acq_count])
        missing = [m for m in meta if m not in roots]
        check(f"{key}: every root the metadata prints is read", not missing,
              ", ".join(missing))
    prof = scope_profiles.PROFILES["msox2014a"]
    bad = prof.ch_scale.format(ch=2)
    logged = []
    s = scope_with(prof, {}, refuse={bad})
    vals = s.read_settings(log=logged.append)
    check("a root that will not answer is left out and logged",
          bad not in vals and len(logged) == 1 and bad in logged[0], logged)
    lines = s.metadata([2], vals).splitlines()
    check("...and the metadata prints '?' for it",
          any(line.startswith("CH2") and line.endswith(": ?") for line in lines))


def dither_checks():
    print("\noffset dither without the panel")
    prof = scope_profiles.PROFILES["msox2014a"]
    sc1, of1 = prof.ch_scale.format(ch=1), prof.ch_offset.format(ch=1)
    sc3, of3 = prof.ch_scale.format(ch=3), prof.ch_offset.format(ch=3)
    s = scope_with(prof, {sc1: "1.0E+00", of1: "2.5E+00",
                          sc3: "5.0E-01", of3: "-1.0E+00"})
    plan = s.dither_plan([1, 3], 3)
    check("plan keeps each channel's offset and spans N codes of its V/div",
          np.isclose(plan[1][0], 2.5) and np.isclose(plan[1][1], 3 * 0.04025)
          and np.isclose(plan[3][0], -1.0) and np.isclose(plan[3][1], 3 * 0.04025 * 0.5),
          plan)
    count = 4
    offs = [sg.dither_offset(2.5, plan[1][1], k, count) for k in range(count)]
    check("steps are evenly spaced and centred on the original offset",
          np.isclose(np.mean(offs), 2.5)
          and np.allclose(np.diff(offs), plan[1][1] / count), offs)
    s.inst.writes.clear()
    failed = s.dither_step(plan, 0, count)
    check("a step writes every planned channel", not failed
          and s.inst.writes == [f"{of1} {offs[0]:.6g}",
                                f"{of3} {sg.dither_offset(-1.0, plan[3][1], 0, count):.6g}"],
          s.inst.writes)
    s.inst.writes.clear()
    failed = s.restore_offsets(plan)
    check("restore puts the original offsets back", not failed
          and s.inst.writes == [f"{of1} 2.5", f"{of3} -1"], s.inst.writes)
    s.inst.refuse = {of3}
    failed = s.dither_step(plan, 1, count)
    check("a refused channel is reported, the others are still set",
          list(failed) == [3] and s.inst.writes[-1].startswith(of1))
    s2 = scope_with(prof, {sc1: "1.0"}, refuse={of3})
    try:
        s2.dither_plan([1, 3], 3)
        check("an unreadable channel raises DitherError naming it", False)
    except sg.DitherError as err:
        check("an unreadable channel raises DitherError naming it", err.ch == 3)


def write_checks(tmp):
    print("\ncapture files without the panel")
    rng = np.random.default_rng(3)
    n = 5000
    codes = (30000 + rng.normal(0, 400, n)).astype(np.uint16)
    rec1 = Record((2e-6, -1e-3, 0.0), codes=codes, y=(1.5723e-4, 32768.0, 0.0))
    rec2 = Record((2e-6, -1e-3, 0.0), codes=codes[::-1].copy(),
                  y=(3.1e-5, 32768.0, 1.0))
    recs = {"CH1_PD_V": rec1, "CH3_Mon_V": rec2}
    for ext in (".npz", ".csv"):
        base = os.path.join(tmp, "cap" + ext[1:])
        path = sg.write_capture(base, ext, recs, "captured : x\nCH1 name : PD\n")
        cols, data = sg.load_capture(path)
        tol = 0 if ext == ".npz" else 2e-6
        check(f"{ext}: columns and volts come back",
              cols == ["time_s", "CH1_PD_V", "CH3_Mon_V"]
              and np.allclose(data[:, 1], rec1.v(), rtol=tol, atol=1e-12)
              and np.allclose(data[:, 2], rec2.v(), rtol=tol, atol=1e-12)
              and np.allclose(data[:, 0], rec1.t(), rtol=1e-8, atol=1e-15))
        with open(base + ".txt", encoding="utf-8") as fh:
            check(f"{ext}: sidecar written as given", fh.read().startswith("captured : x"))
    _, data = sg.load_capture(os.path.join(tmp, "capnpz.npz"))
    check("NPZ volts are bit-exact", np.array_equal(data[:, 1], rec1.v()))


def main():
    tmp = tempfile.mkdtemp(prefix="scopegrab-headless-")
    settings_checks()
    dither_checks()
    write_checks(tmp)
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("Headless API OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
