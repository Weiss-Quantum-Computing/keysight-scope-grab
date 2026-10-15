"""Several scopes open at once, without the panel: ScopeRegistry, ScopeSlot,
the config migration, and the Scope changes they rest on.

Two scopes on one fake VISA bus (an MSO-X and a DS1054Z, both on USB, so the
profiles' scans overlap the way they do at the bench). pyvisa's own
ResourceManager is replaced by one that fails the test if anything calls it,
so nothing here can reach a real instrument.

    python tests/test_registry.py
"""
import importlib.util
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location(
    "scope_grab", os.path.join(REPO, "scope_grab.py"))
sg = importlib.util.module_from_spec(spec)
sys.modules["scope_grab"] = sg
spec.loader.exec_module(sg)

REAL_VISA = []


def _no_real_visa(*a, **k):
    REAL_VISA.append(a)
    raise AssertionError("a real VISA ResourceManager was asked for")


sg.pyvisa.ResourceManager = _no_real_visa

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + str(detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


MSOX_RES = "USB0::0x0957::0x1798::MY63080029::INSTR"
RIGOL_RES = "USB0::0x1AB1::0x04CE::DS1ZA184752794::INSTR"
MSOX_IDN = "KEYSIGHT TECHNOLOGIES,MSO-X 2014A,MY63080029,02.65.2021030741"
RIGOL_IDN = "RIGOL TECHNOLOGIES,DS1054Z,DS1ZA184752794,00.04.04.SP1"


class FakeDev:
    def __init__(self, res, idn, log):
        self.res, self.idn, self.log = res, idn, log
        self.closed = False
        self.timeout = self.chunk_size = None
        self.read_termination = self.write_termination = None

    def query(self, q):
        self.log.append((self.res, q))
        if q == "*IDN?":
            return self.idn + "\n"
        return "0\n"

    def write(self, q):
        self.log.append((self.res, q))

    def close(self):
        self.closed = True


class FakeRM:
    def __init__(self, devices):
        self.devices = devices
        self.opened = []
        self.log = []
        self.closed = 0

    def list_resources(self):
        return tuple(self.devices)

    def open_resource(self, res):
        dev = FakeDev(res, self.devices[res], self.log)
        self.opened.append(dev)
        return dev

    def close(self):
        self.closed += 1


def migration_checks():
    print("config migration")
    old = {"model": "ds1054z", "outdir": "x", "prefix": "eom",
           "channel_names": {"1": "PD", "2": "mon"}, "channels": {"1": True, "2": False}}
    slots, active = sg.migrate_config(old)
    check("an old single-scope config becomes one slot",
          len(slots) == 1 and slots[0]["model"] == "ds1054z" and active == "ds1054z")
    check("with its channel names and ticks",
          slots[0]["channel_names"] == {"1": "PD", "2": "mon"}
          and slots[0]["channels"] == {"1": True, "2": False})
    check("and no file tag, so its captures keep their names",
          slots[0]["tag"] == "")
    slots, active = sg.migrate_config({})
    check("no config at all: the default scope",
          [s["model"] for s in slots] == [sg.scope_profiles.DEFAULT_PROFILE])
    slots, _ = sg.migrate_config({"model": "no-such-scope"})
    check("an unknown model falls back to the default",
          slots[0]["model"] == sg.scope_profiles.DEFAULT_PROFILE)
    slots, _ = sg.migrate_config("not a dict")
    check("a config that is not an object still gives a slot", len(slots) == 1)
    new = {"model": "msox2014a", "active": "ds1054z", "scopes": [
        {"id": "msox2014a", "model": "msox2014a", "tag": "",
         "resource": "", "channel_names": {"1": "Trek"}},
        {"id": "ds1054z", "model": "ds1054z", "tag": "rigol scope_1",
         "last_addr": RIGOL_RES},
        {"id": "ds1054z", "model": "ds1054z"},
        {"id": "x", "model": "no-such-scope"},
        "junk"]}
    slots, active = sg.migrate_config(new)
    check("a multi-scope config keeps its scopes, drops what it cannot use",
          [s["model"] for s in slots] == ["msox2014a", "ds1054z", "ds1054z"],
          [s["id"] for s in slots])
    check("ids are made unique", len({s["id"] for s in slots}) == 3)
    check("the active scope is kept", active == "ds1054z")
    check("a tag is made safe for a file name (no spaces, no '_')",
          slots[1]["tag"] == "rigol-scope-1", slots[1]["tag"])
    check("the last address comes back", slots[1]["last_addr"] == RIGOL_RES)
    _, active = sg.migrate_config(dict(new, active="gone"))
    check("an active id that is not there falls back to the first",
          active == "msox2014a")

    print("\nfile prefixes")
    check("no tag: the prefix as typed", sg.file_prefix("eom", "") == "eom")
    check("a tag: <prefix>-<tag>", sg.file_prefix("eom", "ds1054z") == "eom-ds1054z")
    check("safe_tag strips what a file name cannot hold",
          sg.safe_tag(' a/b c_d ') == "a-b-c-d", sg.safe_tag(' a/b c_d '))


def registry_checks():
    print("\nthe registry builds without opening anything")
    rm = FakeRM({MSOX_RES: MSOX_IDN, RIGOL_RES: RIGOL_IDN})
    made = []

    def factory():
        made.append(1)
        return rm
    cfg = {"scopes": [{"id": "msox2014a", "model": "msox2014a"},
                      {"id": "ds1054z", "model": "ds1054z", "tag": "ds1054z"}],
           "active": "ds1054z"}
    reg, active = sg.ScopeRegistry.from_config(cfg, rm_factory=factory)
    check("two slots, in order", [s.id for s in reg] == ["msox2014a", "ds1054z"])
    check("active comes back", active == "ds1054z")
    check("no RM made yet", not made)
    msox, rigol = reg.get("msox2014a"), reg.get("ds1054z")
    check("each slot holds the shared profile of its model",
          msox.prof is sg.scope_profiles.PROFILES["msox2014a"]
          and rigol.prof is sg.scope_profiles.PROFILES["ds1054z"])

    print("\nconnecting the second scope first")
    idn = rigol.submit(reg.connect, rigol).result(timeout=10)
    check("the DS1054Z is found", idn == RIGOL_IDN and rigol.scope.addr == RIGOL_RES)
    check("one RM, made on demand", len(made) == 1 and rigol.scope.rm is rm)
    check("its scan asked the MSO-X who it was and let it go",
          any(d.res == MSOX_RES and d.closed for d in rm.opened))
    check("the last address is remembered", rigol.last_addr == RIGOL_RES)

    print("\nthen the first")
    before = len(rm.log)
    idn = msox.submit(reg.connect, msox).result(timeout=10)
    check("the MSO-X is found", idn == MSOX_IDN and msox.scope.addr == MSOX_RES)
    check("its scan never touched the DS1054Z's open session",
          not any(r == RIGOL_RES for r, _ in rm.log[before:]), rm.log[before:])
    check("both open at once, on the one RM",
          reg.connected() == [msox, rigol] and msox.scope.rm is rigol.scope.rm is rm)
    check("held() names the other's resource",
          reg.held(but=msox) == {RIGOL_RES.upper()})

    print("\nreconnecting does not scan the other scope's session")
    before = len(rm.log)
    rigol.submit(reg.connect, rigol).result(timeout=10)
    check("the DS1054Z reconnects", rigol.connected and rigol.scope.idn == RIGOL_IDN)
    check("without a word to the MSO-X",
          not any(r == MSOX_RES for r, _ in rm.log[before:]), rm.log[before:])
    msox_dev = msox.scope.inst
    check("and the MSO-X session is still the same one, open",
          msox_dev is not None and not msox_dev.closed)

    print("\ndisconnecting one leaves the other alone")
    rigol_dev = rigol.scope.inst
    rigol.submit(reg.disconnect, rigol).result(timeout=10)
    check("the DS1054Z is closed", not rigol.connected and rigol_dev.closed)
    check("the shared RM is not", rm.closed == 0)
    check("the MSO-X session is still open", msox.connected and not msox_dev.closed)

    print("\na failed connect does not close the shared RM")
    extra = reg.add("ds1054z", resource="USB0::0x1AB1::0x04CE::NOTHERE::INSTR")
    try:
        rm.devices["USB0::0x1AB1::0x04CE::NOTHERE::INSTR"] = "SOME OTHER BOX,1,,1"
        extra.submit(reg.connect, extra).result(timeout=10)
        check("a stranger at the typed address is refused", False)
    except RuntimeError as exc:
        check("a stranger at the typed address is refused", "SOME OTHER BOX" in str(exc))
    check("the shared RM survived it", rm.closed == 0 and msox.connected)
    check("a second slot of a model gets its own profile instance",
          extra.prof is not rigol.prof and extra.prof.key == "ds1054z")
    check("and its own id and tag", extra.id == "ds1054z-2"
          and extra.tag not in ("", rigol.tag), (extra.id, extra.tag))
    extra.resource = MSOX_RES
    try:
        extra.submit(reg.connect, extra).result(timeout=10)
        check("a typed address another scope holds is refused", False)
    except RuntimeError as exc:
        check("a typed address another scope holds is refused",
              "already open" in str(exc), str(exc))
    check("and the holder is untouched", msox.connected and not msox_dev.closed)
    reg.remove(extra.id)
    check("a disconnected slot can be removed", reg.get(extra.id) is None)
    try:
        reg.remove("msox2014a")
        check("a connected slot cannot", False)
    except RuntimeError:
        check("a connected slot cannot", reg.get("msox2014a") is msox)

    print("\none worker per scope")
    gate = threading.Event()
    seen = []

    def wait_for_gate():
        seen.append("A start")
        ok = gate.wait(5)
        seen.append("A end")
        return ok

    def open_gate():
        seen.append("B")
        gate.set()
    fa = msox.submit(wait_for_gate)
    fb = rigol.submit(open_gate)
    check("two scopes' jobs run side by side (B unblocks A)",
          fa.result(timeout=10) is True and fb.result(timeout=10) is None, seen)
    order = []
    futs = [msox.submit(lambda i=i: (time.sleep(0.01 * (3 - i)), order.append(i)))
            for i in range(3)]
    for f in futs:
        f.result(timeout=10)
    check("one scope's jobs run in the order they were sent", order == [0, 1, 2], order)
    check("each under the scope's lock",
          msox.submit(lambda: msox.lock._is_owned()).result(timeout=10))
    fut = msox.submit(lambda: 1 / 0)
    try:
        fut.result(timeout=10)
        check("an exception comes back through the future", False)
    except ZeroDivisionError:
        check("an exception comes back through the future", True)
    check("and the worker carries on", msox.submit(lambda: 7).result(timeout=10) == 7)
    check("nothing is left pending", not msox.busy and not rigol.busy)

    print("\nconfig round trip")
    cfg2 = {"scopes": reg.to_config(), "active": "msox2014a"}
    reg2, active2 = sg.ScopeRegistry.from_config(cfg2, rm_factory=factory)
    check("the same scopes come back", [s.id for s in reg2] == [s.id for s in reg]
          and [s.tag for s in reg2] == [s.tag for s in reg])
    check("with their last addresses",
          reg2.get("ds1054z").last_addr == RIGOL_RES)

    print("\nclosing the registry")
    reg.close()
    check("every scope closed", not reg.connected() and msox_dev.closed)
    check("and the shared RM, once", rm.closed == 1)
    check("no real VISA was ever asked for", not REAL_VISA)


def standalone_checks():
    print("\na Scope on its own is unchanged")
    rm = FakeRM({MSOX_RES: MSOX_IDN})
    sg.pyvisa.ResourceManager = lambda *a: rm
    try:
        s = sg.Scope(sg.scope_profiles.PROFILES["msox2014a"])
        s.connect()
        s.close()
    finally:
        sg.pyvisa.ResourceManager = _no_real_visa
    check("closes the RM it made", rm.closed == 1)
    host = FakeRM({})
    s = sg.Scope(sg.scope_profiles.PROFILES["msox2014a"])
    s._make_rm = lambda: host
    try:
        s.connect()
    except RuntimeError:
        pass
    s.close()
    check("but not one a host's _make_rm handed it, even on a failed connect",
          host.closed == 0)
    rm2 = FakeRM({MSOX_RES: MSOX_IDN, RIGOL_RES: RIGOL_IDN})
    s = sg.Scope(sg.scope_profiles.PROFILES["msox2014a"], rm_source=lambda: rm2)
    s.connect(prefer=MSOX_RES)
    check("prefer is tried first", rm2.opened[0].res == MSOX_RES)
    s.close()
    check("a lent RM is never closed by the Scope", rm2.closed == 0)
    s = sg.Scope(sg.scope_profiles.PROFILES["ds1054z"], rm_source=lambda: rm2)
    try:
        s.connect(skip={MSOX_RES, RIGOL_RES})
        check("a scan with everything skipped finds nothing", False)
    except RuntimeError:
        check("a scan with everything skipped finds nothing", True)
    check("...without opening anything", len(rm2.opened) == 1 and rm2.closed == 0)


def main():
    migration_checks()
    registry_checks()
    standalone_checks()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("Registry OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
