"""The names other programs reach into scope_grab.py for, loaded by path.

EOM-ILC (ilc_bench.find_scope_grab -> load_module) and the ramp polarimeter
(rampol/hw.load_scope_grab, scope_grab_path in rampol/config.py) both load
THIS file by path and drive a Scope without the panel. Neither repo's tests
run when this one changes, so this is where a rename would be caught.

The list was made on 15 Oct 2026 by listing scope_grab.py's names and
grepping EOM-ILC and ramp-polarimeter for every one they touch. Add to it
when a consumer starts using something new; do not take anything off it
without changing the consumer first.

Also pinned, because both consumers do it: they replace scope._make_rm with
their own shared ResourceManager and wrap close() to set scope.rm = None
before the real close, so that one instrument's close cannot tear down the
other's session (EOM-ILC ilc_bench.make_scope, rampol hw.share_rm). And the
polarimeter's simulator subclasses Scope with super().__init__(prof).

No instrument, no Tk, no config file.

    python tests/test_consumer_api.py
"""
import importlib.util
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + str(detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


# Loaded exactly the way the consumers load it.
spec = importlib.util.spec_from_file_location(
    "scope_grab", os.path.join(REPO, "scope_grab.py"))
sg = importlib.util.module_from_spec(spec)
sys.modules["scope_grab"] = sg
spec.loader.exec_module(sg)

MODULE_NAMES = (
    # EOM-ILC: eomilc/optical.py, eomilc/scope.py, tests/test_scope_files.py,
    # ilc_bench.make_scope; rampol: hw.py, scan.py, sim.py, gui.py, tests.
    "Scope", "load_capture", "read_npz", "write_npz", "write_capture",
    "setting_roots", "safe_column", "split_capture_name", "scope_profiles",
    "pyvisa",
)
SCOPE_ATTRS = (
    "_make_rm", "close", "connect", "get", "put", "try_get", "errors",
    "is_running", "run", "single", "accumulate", "waveform", "record",
    "transfer_plan", "metadata", "read_settings", "dither_plan",
    "dither_step", "restore_offsets",
)
SCOPE_STATE = ("rm", "inst", "idn", "addr", "prof")
PROFILE_ATTRS = ("key", "channels", "acq_type", "acq_count", "wave_count",
                 "ch_scale", "ch_offset", "avg_name")


class FakeDev:
    def __init__(self, idn):
        self.idn, self.closed = idn, False
        self.timeout = self.chunk_size = None
        self.read_termination = self.write_termination = None

    def query(self, q):
        return self.idn + "\n"

    def write(self, q):
        pass

    def close(self):
        self.closed = True


class FakeRM:
    def __init__(self, devices):
        self.devices, self.closed, self.opened = devices, False, []

    def list_resources(self):
        return tuple(self.devices)

    def open_resource(self, res):
        dev = FakeDev(self.devices[res])
        self.opened.append(dev)
        return dev

    def close(self):
        self.closed = True


def main():
    print("module names the consumers use")
    for name in MODULE_NAMES:
        check(f"scope_grab.{name}", hasattr(sg, name))
    sp = sg.scope_profiles
    for name in ("get_profile", "PROFILES", "DEFAULT_PROFILE", "Record"):
        check(f"scope_profiles.{name}", hasattr(sp, name))
    check("pyvisa.ResourceManager", hasattr(sg.pyvisa, "ResourceManager"))
    check("get_profile falls back to the default for an unknown key",
          sp.get_profile("no-such-scope").key == sp.DEFAULT_PROFILE)

    print("\nScope as the consumers drive it")
    prof = sp.get_profile(sp.DEFAULT_PROFILE)
    for name in PROFILE_ATTRS:
        check(f"profile.{name}", hasattr(prof, name))
    sc = sg.Scope(prof)                     # positional, as both build it
    for name in SCOPE_ATTRS:
        check(f"Scope.{name} is callable", callable(getattr(sc, name, None)))
    for name in SCOPE_STATE:
        check(f"Scope.{name} exists before connect", hasattr(sc, name))
    check("a fresh Scope holds no session", sc.rm is None and sc.inst is None)
    check("connect takes an address as its first argument",
          list(inspect.signature(sg.Scope.connect).parameters)[1] == "addr")

    class SimScope(sg.Scope):               # rampol/sim.py's shape
        def __init__(self, prof, bench):
            super().__init__(prof)
            self.bench = bench
    check("subclassable with super().__init__(prof)",
          SimScope(prof, None).prof is prof)

    print("\nthe shared-RM monkeypatch (ilc_bench.make_scope, rampol hw.share_rm)")
    good = "KEYSIGHT TECHNOLOGIES,MSO-X 2014A,MY63080029,02.65"
    shared = FakeRM({"USB0::0x0957::0x1798::MY63080029::INSTR": good})
    sc = sg.Scope(prof)
    sc._make_rm = lambda: shared
    orig_close = sc.close

    def close_keeping_rm():
        sc.rm = None
        orig_close()
    sc.close = close_keeping_rm
    check("connect goes through the replaced _make_rm", sc.connect() == good
          and sc.rm is shared and sc.addr.endswith("MY63080029::INSTR"))
    sc.close()
    check("close() with rm set to None leaves the shared RM open",
          not shared.closed and sc.inst is None and sc.idn == "")
    check("and closed the instrument session", shared.opened[-1].closed)

    print("\na failed connect never closes an RM the Scope did not make")
    # The polarimeter's case (found by the 15 Oct review, G6): one RM shared
    # by the scope, the 4063B and the DS345; a scope Connect that finds no
    # scope used to call rm.close() directly and kill the AWG's session.
    for how in ("host-replaced _make_rm", "Scope(prof, rm=shared)"):
        shared = FakeRM({"USB0::0xF4EC::0xEE38::574D22116::INSTR":
                         "B&K Precision,4063B,574D22116,1.0"})
        awg = shared.open_resource("USB0::0xF4EC::0xEE38::574D22116::INSTR")
        if how.startswith("host"):
            sc = sg.Scope(prof)
            sc._make_rm = lambda: shared
        else:
            sc = sg.Scope(prof, rm=shared)
        try:
            sc.connect()
            refused = False
        except RuntimeError:
            refused = True
        check(f"{how}: no scope found is an error", refused)
        check(f"{how}: the shared RM and the AWG session survive it",
              not shared.closed and not awg.closed)
        sc.close()
        check(f"{how}: and close() leaves them too",
              not shared.closed and not awg.closed)

    print("\nan RM the Scope made itself is still its own to close")
    real = sg.pyvisa.ResourceManager
    try:
        own = FakeRM({"USB0::x::INSTR": good})
        sg.pyvisa.ResourceManager = lambda *a: own
        sc = sg.Scope(prof)
        sc.connect()
        sc.close()
        check("closed on close()", own.closed)
        own = FakeRM({})
        sc = sg.Scope(prof)
        try:
            sc.connect()
        except RuntimeError:
            pass
        check("and on a connect that found nothing", own.closed)
    finally:
        sg.pyvisa.ResourceManager = real

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("Consumer API OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
