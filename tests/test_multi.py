"""The window with two scopes: switching between them in place, connecting
and disconnecting each on its own, grabbing one or all, sequences across
both, and the config that remembers them.

Built the way the other panel tests build it: CONFIG_PATH sandboxed and
App.do_connect replaced BEFORE any App exists, the window withdrawn and moved
to +6000+200. The connect checks go through the registry onto a fake VISA bus
(scope_grab.RM_FACTORY); pyvisa's own ResourceManager is a tripwire. For the
grab checks each slot's Scope is swapped for a fake that hands back fixed
records, so what is tested is the panel's plumbing - which scope, which
thread, which file - not any instrument's behaviour.

    python tests/test_multi.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import tkinter as tk

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location(
    "scope_grab", os.path.join(REPO, "scope_grab.py"))
sg = importlib.util.module_from_spec(spec)
sys.modules["scope_grab"] = sg
spec.loader.exec_module(sg)

SANDBOX = tempfile.mkdtemp(prefix="scopegrab-multi-")
sg.CONFIG_PATH = os.path.join(SANDBOX, "config.json")      # FIRST
_CONNECTS = []
sg.App.do_connect = lambda self: _CONNECTS.append(self)    # no startup connect
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
IDNS = {MSOX_RES: "KEYSIGHT TECHNOLOGIES,MSO-X 2014A,MY63080029,02.65",
        RIGOL_RES: "RIGOL TECHNOLOGIES,DS1054Z,DS1ZA184752794,00.04.04.SP1"}


class FakeDev:
    def __init__(self, res, log):
        self.res, self.log, self.closed = res, log, False
        self.timeout = self.chunk_size = None
        self.read_termination = self.write_termination = None

    def query(self, q):
        self.log.append((self.res, q))
        return (IDNS[self.res] if q == "*IDN?" else "1") + "\n"

    def write(self, q):
        self.log.append((self.res, q))

    def close(self):
        self.closed = True


class FakeRM:
    def __init__(self):
        self.log, self.opened, self.closed = [], [], 0

    def list_resources(self):
        return tuple(IDNS)

    def open_resource(self, res):
        dev = FakeDev(res, self.log)
        self.opened.append(dev)
        return dev

    def close(self):
        self.closed += 1


RM = FakeRM()
sg.RM_FACTORY = lambda: RM


class FakeScope:
    """What a grab asks of a Scope, with fixed records; the dither methods
    are Scope's own, running on get/put here."""
    dither_plan = sg.Scope.dither_plan
    dither_step = sg.Scope.dither_step
    restore_offsets = sg.Scope.restore_offsets
    averaging = sg.Scope.averaging

    def __init__(self, prof, level, barrier=None):
        self.prof, self.barrier = prof, barrier
        self.inst, self.idn, self.addr = object(), f"FAKE {prof.name}", "FAKE"
        self.recs = {ch: sg.scope_profiles.Record(
            (1e-6, 0.0, 0.0), codes=(np.arange(500) % 50 + level * ch).astype(np.uint16),
            y=(0.01, 0.0, 0.0)) for ch in prof.channels}
        self.threads, self.events, self.values = [], [], {}

    def is_displayed(self, ch):
        return True

    def averaging_depth(self):
        return None

    def freeze(self):
        return False

    def run(self):
        pass

    def single(self, wait_s=10.0, cancelled=None):
        self.threads.append(threading.current_thread().name)
        self.events.append("grab")
        if self.barrier is not None:
            self.barrier.wait()          # both scopes must be in here at once
        return True

    def transfer_plan(self, averaged, points):
        return "RAW", points

    def record(self, ch, points_mode="RAW", points=None):
        return self.recs[ch]

    def hit_count(self):
        return None

    def try_get(self, scpi, timeout_ms=2000):
        return None

    def screenshot(self):
        return None

    def metadata(self, chans, settings, names=None, label=None, existing=False):
        return f"instrument : {self.idn}\nnames : {names}\n"

    def get(self, scpi):
        return self.values.get(scpi, "1")

    def put(self, scpi, value):
        self.events.append(("put", scpi, value))
        self.values[scpi] = value

    def close(self):
        self.inst = None


def settle(root, cond, timeout=15.0):
    """Run the real mainloop until cond() holds (then a little longer, for the
    log pump), or the timeout. It has to be mainloop, not update(): the
    workers hand results back with root.after from their own threads, and
    tkinter only accepts that while the main thread sits in mainloop - which
    is how the window runs."""
    t0 = time.time()
    state = {"ok": False, "met": None}

    def poll():
        now = time.time()
        if state["met"] is None and cond():
            state["met"] = now
        if state["met"] is not None and now - state["met"] > 0.25:
            state["ok"] = cond()
            root.quit()
        elif now - t0 > timeout:
            root.quit()
        else:
            root.after(10, poll)
    root.after(10, poll)
    root.mainloop()
    return state["ok"]


def idle(app):
    return (not app.busy and not app.seq_active and app.grab_pending == 0
            and not any(s.busy for s in app.registry) and not app.connecting)


def build(cfg):
    with open(sg.CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    root = tk.Tk()
    root.withdraw()
    app = sg.App(root)
    root.geometry("+6000+200")
    return root, app


def log_text(app):
    return app.logbox.get("1.0", "end")


def migration_checks():
    print("an old single-scope config")
    root, app = build({"model": "ds1054z", "prefix": "eom",
                       "channel_names": {"1": "PD", "3": "MonX1"},
                       "channels": {"1": True, "3": True}})
    try:
        check("becomes one scope, the one it named",
              [s.id for s in app.registry] == ["ds1054z"]
              and app.prof.key == "ds1054z")
        check("its channel names and ticks are in the boxes",
              app.ch_names[1].get() == "PD" and app.ch_names[3].get() == "MonX1"
              and app.channels() == [1, 3])
        check("no file tag, so its files keep their names",
              app.safe_prefix() == "eom")
        app.saved_cfg = None             # write even if nothing changed
        app.save_config()
        with open(sg.CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        check("saved back with the scopes list",
              [e["model"] for e in saved["scopes"]] == ["ds1054z"]
              and saved["active"] == "ds1054z")
        check("and the old keys still there for the bench copy",
              saved["model"] == "ds1054z"
              and saved["channel_names"].get("1") == "PD")
    finally:
        root.destroy()


def two_scope_checks():
    print("\ntwo scopes in one window")
    cfg = {"prefix": "eom", "outdir": os.path.join(SANDBOX, "data"),
           "active": "msox2014a", "scopes": [
               {"id": "msox2014a", "model": "msox2014a",
                "channel_names": {"1": "Trek X1"}, "channels": {"1": True}},
               {"id": "ds1054z", "model": "ds1054z", "tag": "ds1054z",
                "channel_names": {"1": "PD", "2": "gate"},
                "channels": {"1": True, "2": True}}]}
    root, app = build(cfg)
    try:
        msox, rigol = app.registry.get("msox2014a"), app.registry.get("ds1054z")
        check("both rows are there", set(app.slot_widgets) == {"msox2014a", "ds1054z"})
        check("the MSO-X is on show", app.active is msox
              and root.title() == "Scope Grab - MSO-X 2014A")
        check("startup connect went to the stand-in", _CONNECTS[-1] is app
              if settle(root, lambda: bool(_CONNECTS)) else False)

        print("\nconnecting each on its own worker")
        app.connect_slot(rigol)
        app.connect_slot(msox)
        ok = settle(root, lambda: msox.connected and rigol.connected and idle(app))
        check("both connect", ok, log_text(app)[-300:])
        check("onto their own instruments",
              msox.scope.addr == MSOX_RES and rigol.scope.addr == RIGOL_RES)
        check("sharing one RM", msox.scope.rm is rigol.scope.rm is RM)
        check("each on a thread of its own",
              msox._thread is not rigol._thread
              and {msox._thread.name, rigol._thread.name}
              == {"scope-msox2014a", "scope-ds1054z"})
        check("rows say Disconnect",
              str(app.slot_widgets["ds1054z"]["btn"].cget("text")) == "Disconnect")
        check("the MSO-X reading is in the panel",
              app.read_stamp != "" and app.set_scope[msox.prof.acq_type] != "")
        check("the DS1054Z reading waits on its slot", len(rigol.parked) == 1)
        check("no real VISA", not REAL_VISA)

        print("\nswitching what the window shows - no restart")
        roots_msox = set(app.set_vars)
        some = msox.prof.timebase[0][1]
        app.set_vars[some].set("0.00123")          # an unapplied edit
        check("an edit is marked", app.edited(some))
        app.active_var.set("ds1054z")
        app.on_active_picked()
        check("the DS1054Z is on show", app.active is rigol
              and root.title() == "Scope Grab - Rigol DS1054Z")
        check("its settings rows, from its profile",
              set(app.set_vars) == set(sg.setting_roots(rigol.prof))
              and set(app.set_vars) != roots_msox)
        check("its preview box", (app.preview_w, app.preview_h)
              == rigol.prof.preview_size)
        check("its channel names", app.ch_names[1].get() == "PD"
              and app.ch_names[2].get() == "gate" and app.channels() == [1, 2])
        check("the reading that waited is now in the panel",
              app.read_stamp != "" and not rigol.parked)
        check("one action button per DS1054Z action",
              len(app.action_btns) == len(rigol.prof.actions))
        check("the MSO-X is still connected", msox.connected)
        app.ch_names[2].set("gate 2")
        app.active_var.set("msox2014a")
        app.on_active_picked()
        check("back on the MSO-X its unapplied edit is still there",
              app.set_vars[some].get() == "0.00123" and app.edited(some))
        check("and its channel name", app.ch_names[1].get() == "Trek X1")
        check("the DS1054Z kept the name typed for it",
              rigol.channel_names.get("2") == "gate 2")
        app.busy = True
        app.active_var.set("ds1054z")
        app.on_active_picked()
        check("no switching during a capture", app.active is msox
              and app.active_var.get() == "msox2014a")
        app.busy = False
        app.set_busy(False)

        print("\nGRAB takes the shown scope, or every connected one")
        out = os.path.join(SANDBOX, "data")
        barrier = threading.Barrier(2, timeout=5)
        fm = msox.scope = FakeScope(msox.prof, 10, barrier)
        fr = rigol.scope = FakeScope(rigol.prof, 20, barrier)
        app.save_png.set(False)
        app.data_fmt.set("NPZ")
        app.grab_all.set(True)
        app.grab_target_changed()
        check("the button says GRAB ALL",
              str(app.grab_btn.cget("text")).startswith("GRAB ALL"))
        app.do_grab()
        ok = settle(root, lambda: idle(app))
        files = sorted(os.listdir(out)) if os.path.isdir(out) else []
        msox_npz = [f for f in files if f.startswith("eom_") and f.endswith(".npz")]
        rigol_npz = [f for f in files if f.startswith("eom-ds1054z_")
                     and f.endswith(".npz")]
        check("both scopes wrote, at the same time (barrier passed)",
              ok and len(msox_npz) == 1 and len(rigol_npz) == 1 and app.grab_wrote,
              f"{files} {log_text(app)[-600:]}")
        check("each on its own worker",
              fm.threads == ["scope-msox2014a"] and fr.threads == ["scope-ds1054z"],
              (fm.threads, fr.threads))
        if msox_npz and rigol_npz:
            cols_m, _ = sg.load_capture(os.path.join(out, msox_npz[0]))
            cols_r, _ = sg.load_capture(os.path.join(out, rigol_npz[0]))
            check("each with its own channel names",
                  cols_m == ["time_s", "CH1_Trek_X1_V"]
                  and cols_r == ["time_s", "CH1_PD_V", "CH2_gate_2_V"],
                  (cols_m, cols_r))
        app.grab_all.set(False)
        app.grab_target_changed()
        fm.barrier = fr.barrier = None
        before = set(files)
        app.do_grab()
        settle(root, lambda: idle(app))
        new = set(os.listdir(out)) - before
        check("GRAB on its own takes only the shown scope",
              len(new) == 2 and all(f.startswith("eom_") for f in new), new)

        print("\nfile tags keep the scopes apart")
        app.grab_all.set(True)
        app.slot_widgets["ds1054z"]["tag"].delete(0, "end")
        check("two scopes on one prefix are refused",
              app.grab_targets(quiet=True) == [] and
              str(app.grab_btn.cget("state")) == "disabled")
        app.slot_widgets["ds1054z"]["tag"].insert(0, "rigol 1")
        check("a tag is made safe as it is typed", rigol.tag == "rigol-1")
        check("and GRAB ALL is back", app.grab_targets(quiet=True) == [msox, rigol])

        print("\na sequence across both, with the offset dither")
        open(os.path.join(out, "eom-rigol-1_001.npz"), "wb").close()
        app.seq_count.set("3")
        app.seq_start.set("1")
        app.seq_interval.set("0")
        app.seq_dither.set(True)
        app.seq_dither_codes.set("2")
        fm.events.clear()
        fr.events.clear()
        app.do_sequence()
        ok = settle(root, lambda: idle(app), timeout=30)
        files = set(os.listdir(out))
        want = ({f"eom_00{i}.npz" for i in (2, 3, 4)}
                | {f"eom-rigol-1_00{i}.npz" for i in (2, 3, 4)})
        check("labels free for BOTH scopes: starts after the one taken",
              ok and want <= files and "eom_001.npz" not in files,
              sorted(f for f in files if "_00" in f))
        check("the sequence ran 3 runs", app.seq_done == 3, app.seq_done)
        for name, fake, slot in (("MSO-X", fm, msox), ("DS1054Z", fr, rigol)):
            off = slot.prof.ch_offset.format(ch=1)
            puts = [e for e in fake.events if e != "grab" and e[1] == off]
            grabs = [i for i, e in enumerate(fake.events) if e == "grab"]
            last_put = max(i for i, e in enumerate(fake.events)
                           if e != "grab" and e[1] == off)
            check(f"{name}: an offset per run, then put back",
                  len(puts) == 4 and float(puts[-1][2]) == 1.0
                  and len({p[2] for p in puts[:3]}) == 3, puts)
            check(f"{name}: put back only after its last run's grab",
                  bool(grabs) and last_put > grabs[-1])
        app.seq_dither.set(False)

        print("\nthe config remembers both")
        app.saved_cfg = None             # write even if nothing changed
        app.save_config()
        with open(sg.CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        check("both scopes, their tags and addresses",
              [(e["id"], e["tag"]) for e in saved["scopes"]]
              == [("msox2014a", ""), ("ds1054z", "rigol-1")]
              and saved["scopes"][1]["last_addr"] == RIGOL_RES)
        check("GRAB ALL and the one on show", saved["grab_all"] is True
              and saved["active"] == "msox2014a")
        check("their own channel names",
              saved["scopes"][1]["channel_names"]["2"] == "gate 2"
              and saved["scopes"][0]["channel_names"]["1"] == "Trek X1")

        print("\nadding, disconnecting, removing")
        msox.scope, rigol.scope = sg.Scope(msox.prof, rm_source=app.registry.rm), \
            sg.Scope(rigol.prof, rm_source=app.registry.rm)
        app.connect_slot(msox)
        app.connect_slot(rigol)
        settle(root, lambda: msox.connected and rigol.connected and idle(app))
        app.set_vars[some].set("0.002")
        before = len(RM.log)
        app.do_apply_settings()
        settle(root, lambda: idle(app))
        sent = RM.log[before:]
        check("Apply writes to the shown scope only, on its worker",
              (MSOX_RES, f"{some} 0.002") in sent
              and not any(r == RIGOL_RES and q.split(" ")[0] == some for r, q in sent),
              [x for x in sent if " " in x[1]][:4])
        check("and reads it back into the panel", not app.edited(some)
              and app.read_stamp != "")
        app.model_var.set("Rigol DS1054Z")
        app.do_add_scope()
        extra = app.registry.get("ds1054z-2")
        check("a second DS1054Z is added and shown", extra is not None
              and app.active is extra and extra.prof is not rigol.prof)
        check("with a tag of its own", extra.tag not in ("", "rigol-1"), extra.tag)
        ok = settle(root, lambda: idle(app))
        check("its connect finds nothing free and says so, touching nothing held",
              ok and not extra.connected and RM.closed == 0
              and msox.connected and rigol.connected
              and "No Rigol DS1054Z found" in log_text(app))
        check("Remove is live for a disconnected scope",
              str(app.remove_btn.cget("state")) == "normal")
        app.do_remove_scope()
        check("removed, and another is on show",
              app.registry.get("ds1054z-2") is None and app.active is msox)
        check("Remove is grey on a connected scope",
              str(app.remove_btn.cget("state")) == "disabled")
        rdev = rigol.scope.inst
        app.disconnect_slot(rigol)
        settle(root, lambda: not rigol.connected and idle(app))
        check("disconnecting the DS1054Z closes only it",
              rdev.closed and msox.connected and not msox.scope.inst.closed
              and RM.closed == 0)

        print("\nclosing the window")
        mdev = msox.scope.inst
        app.on_close()
        check("every session closed and the shared RM closed once",
              mdev.closed and RM.closed == 1)
        check("no real VISA anywhere", not REAL_VISA)
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass

    print("\nand opened again from what it saved")
    root, app = build(saved)
    try:
        check("both scopes, the MSO-X on show, GRAB ALL",
              [s.id for s in app.registry] == ["msox2014a", "ds1054z"]
              and app.active.id == "msox2014a" and app.grab_all.get())
        app.active_var.set("ds1054z")
        app.on_active_picked()
        check("the DS1054Z's names come back with it",
              app.ch_names[2].get() == "gate 2" and app.safe_prefix() == "eom-rigol-1")
    finally:
        root.destroy()


def main():
    migration_checks()
    two_scope_checks()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("Multi-scope panel OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
