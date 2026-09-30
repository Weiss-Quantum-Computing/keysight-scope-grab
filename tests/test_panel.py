"""Build the real window with no scope attached and exercise the paths that
run through the profile.

mainloop() is never entered, so the after() that fires do_connect never runs and
nothing here opens a VISA session. The connect tests use a fake resource manager
instead, which is the only way to check the matching logic without owning one of
every scope.

Needs a desktop session for Tk, but no instrument.

    python tests/test_panel.py
"""
import importlib.util
import inspect
import os
import sys
import tempfile
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import scope_profiles  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "scope_grab", os.path.join(REPO, "scope_grab.py"))
scope_grab = importlib.util.module_from_spec(spec)
sys.modules["scope_grab"] = scope_grab
spec.loader.exec_module(scope_grab)

# Point the config somewhere harmless BEFORE any App exists.
#
# App reads the scope model out of the session config to decide how to lay the
# panel out, and writes that file back when things change. Left alone, this
# test would build its panel for whichever scope the user last selected rather
# than the one it is checking - which is exactly how it started failing every
# MSO-X assertion against a Rigol panel - and a run could overwrite the folder,
# prefix and channel names of a live session.
_SANDBOX = os.path.join(tempfile.mkdtemp(prefix="scopegrab-test-"),
                        "config.json")
scope_grab.CONFIG_PATH = _SANDBOX

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILS.append(label)


def panel_checks(prof):
    print("window builds")
    root = tk.Tk()
    root.withdraw()
    app = scope_grab.App(root)
    try:
        check("title names the profile",
              root.title() == f"Scope Grab - {prof.name}", root.title())
        check("app and scope hold the same profile",
              app.prof is prof and app.scope.prof is prof)
        check("preview box comes from the profile",
              (app.preview_w, app.preview_h) == prof.preview_size,
              str((app.preview_w, app.preview_h)))

        print("\nsettings panel is laid out from the profile")
        expected = {s for _, s, _, _ in prof.timebase}
        expected |= {s for _, s, _, _ in prof.trigger}
        expected |= {s for _, s in prof.info}
        for ch in prof.channels:
            expected |= {t.format(ch=ch) for _, t, _, _ in prof.channel}
        check("every profile field has a widget", set(app.set_vars) == expected,
              f"{len(app.set_vars)} fields")
        check("info rows are read-only",
              all(app.set_kinds[s] == "info" for _, s in prof.info))
        check("info rows carry no edit marker",
              all(s not in app.set_marks for _, s in prof.info))
        check("channel boxes match the profile",
              list(app.ch_vars) == list(prof.channels), str(list(app.ch_vars)))
        check("first channel ticked by default",
              app.ch_vars[prof.channels[0]].get())
        check("one action button per profile action",
              len(app.action_btns) == len(prof.actions))

        print("\ndependent fields grey out from the panel's own modes")
        # Every rule in the profile, not a hand-picked two: each dependent field
        # is driven dead and live through the field that decides it.
        for scpi, (owner, live_for) in prof.depends_on.items():
            app.set_vars[scpi].set("1")
            app.set_vars[owner].set("NOT_A_MODE")
            dead = str(app.set_widgets[scpi].cget("state")) == "disabled"
            asserted = scpi not in app.panel_settings()
            app.set_vars[owner].set(live_for[0])
            live = str(app.set_widgets[scpi].cget("state")) != "disabled"
            sent = app.panel_settings().get(scpi) == "1"
            check(f"{scpi} follows {owner}",
                  dead and asserted and live and sent,
                  f"dead={dead} withheld={asserted} live={live} sent={sent}")

        print("\nmode writes still land before the fields they govern")
        first = prof.write_first
        changes = {s: "1" for s in first}
        changes.update({s: "1" for s in prof.depends_on})
        order = [k for k, _ in sorted(
            changes.items(),
            key=lambda kv: first.index(kv[0]) if kv[0] in first else len(first))]
        for scpi, (owner, _) in prof.depends_on.items():
            check(f"{owner} written before {scpi}",
                  order.index(owner) < order.index(scpi))

        print("\nconfig carries the model")
        check("model in current_cfg", app.current_cfg().get("model") == prof.key)
        check("read_config survives a missing file",
              isinstance(scope_grab.read_config(), dict))
        check("the test is not reading the real config",
              scope_grab.CONFIG_PATH == _SANDBOX and
              "AppData\\Roaming" not in scope_grab.CONFIG_PATH,
              scope_grab.CONFIG_PATH)

        print("\nthe scope selector")
        check("every profile is offered",
              set(app.model_box.cget("values")) ==
              {p.name for p in scope_profiles.PROFILES.values()},
              str(app.model_box.cget("values")))
        check("it shows the profile in force", app.model_var.get() == prof.name)
        check("names map back to keys", app.model_names[prof.name] == prof.key)
        # Re-picking what is already selected must not offer to restart.
        restarted = []
        app.restart = lambda: restarted.append(True)
        app.on_model_picked()
        check("picking the current scope does nothing", not restarted)
        # Nor may it act mid-capture: that would abandon a VISA session
        # and a half-written file. It must refuse and put the box back.
        others = [n for n in app.model_names if n != prof.name]
        if others:
            app.busy = True
            app.model_var.set(others[0])
            app.on_model_picked()
            check("refuses to switch during a capture", not restarted)
            check("and puts the box back", app.model_var.get() == prof.name)
            app.busy = False
        app.set_busy(True)
        check("selector greys out while busy",
              str(app.model_box.cget("state")) == "disabled")
        app.set_busy(False)
        check("and comes back afterwards",
              str(app.model_box.cget("state")) == "readonly")

        print("\nthe plot bar stays put and greys out by tab")
        # The bar used to be packed only for the data tabs, which moved the
        # notebook every time Screenshot was turned to or from.
        tabs = {app.nb.tab(t, "text"): t for t in app.nb.tabs()}

        def turn_to(name):
            app.nb.select(tabs[name])
            root.update()
            packed = app.plot_bar.winfo_manager() == "pack"
            sel = {str(w.cget("state")) for w in app.plot_sel_widgets}
            show = {str(w.cget("state")) for w in app.plot_show_widgets}
            return packed, sel, show, str(app.plot_status.cget("state"))

        check("Screenshot: there, and all of it grey",
              turn_to("Screenshot") == (True, {"disabled"}, {"disabled"},
                                        "disabled"))
        check("Clear is grey with it",
              str(app.cmp_clear_btn.cget("state")) == "disabled")
        for name in ("Waveforms", "Spectrum", "Difference", "Statistics",
                     "Measurements"):
            check(f"{name}: all of it live",
                  turn_to(name) == (True, {"normal"}, {"normal"}, "normal"))
        check("XY: live but for the Show ticks, which it does not read",
              turn_to("XY") == (True, {"normal"}, {"disabled"}, "normal"))
        app.plot_cmp.set("other")
        app.refresh_plots()
        check("Clear is live with something to clear",
              str(app.cmp_clear_btn.cget("state")) == "normal")
        turn_to("Screenshot")
        check("and grey again on Screenshot",
              str(app.cmp_clear_btn.cget("state")) == "disabled")
    finally:
        root.destroy()


def profile_checks(prof):
    print("\nprofile is internally consistent")
    check("do_action takes a rewrites flag",
          "rewrites" in inspect.signature(scope_grab.App.do_action).parameters)
    check("actions are five-field rows", all(len(a) == 5 for a in prof.actions))
    check("at most one action rewrites the panel",
          sum(1 for a in prof.actions if a[4]) <= 1)
    roots = {s for _, s, _, _ in prof.timebase} | {s for _, s, _, _ in prof.trigger}
    check("acquisition type is a panel field", prof.acq_type in roots)
    check("average count is a panel field", prof.acq_count in roots)
    check("dependencies point at real fields",
          all(owner in roots for owner, _ in prof.depends_on.values()))
    check("write_first entries are real fields",
          all(s in roots for s in prof.write_first))
    ch_roots = {t for _, t, _, _ in prof.channel}
    check("channel display flag is a channel field", prof.ch_display in ch_roots)
    meta = {s for _, s in prof.meta_head} | {s for _, s in prof.meta_tail}
    check("metadata rows are all readable fields",
          meta <= (roots | {s for _, s in prof.info}), str(sorted(meta - roots)))
    check("metadata channel rows are channel fields",
          {s for _, s in prof.meta_channel} <= ch_roots)


class FakeDev:
    def __init__(self, idn):
        self._idn = idn
        self.closed = False
        self.timeout = None
        self.chunk_size = None
        self.read_termination = self.write_termination = None

    def query(self, q):
        assert q == "*IDN?", q
        return self._idn + "\n"

    def close(self):
        self.closed = True


class FakeRM:
    def __init__(self, devices):
        self.devices = devices
        self.opened = []

    def list_resources(self):
        return tuple(self.devices)

    def open_resource(self, res):
        dev = FakeDev(self.devices[res])
        self.opened.append(dev)
        return dev

    def close(self):
        pass


def connect_checks(prof):
    print("\nconnect matches on the profile, not on a hardcoded maker")

    def scope_over(devices):
        s = scope_grab.Scope(prof)
        rm = FakeRM(devices)
        s._make_rm = lambda: rm
        return s, rm

    good = "KEYSIGHT TECHNOLOGIES,MSO-X 2014A,MY123,02.65"
    s, _ = scope_over({"USB0::0x2A8D::0x1797::MY123::INSTR": good})
    check("finds the instrument the profile describes", s.connect() == good)
    check("profile configured the session",
          s.inst.timeout == 30000 and s.inst.chunk_size == 1024 * 1024)

    s, rm = scope_over({"USB0::a::INSTR": "SOME OTHER BOX,1234,,1.0"})
    try:
        s.connect()
        check("refuses a device that is not ours", False)
    except RuntimeError as exc:
        check("refuses a device that is not ours", True)
        check("names what did answer instead", "SOME OTHER BOX" in str(exc))
    check("released the session it opened and rejected",
          all(d.closed for d in rm.opened))

    s, _ = scope_over({"ASRL1::INSTR": good})
    try:
        s.connect()
        check("ignores resources the profile does not scan", False)
    except RuntimeError:
        check("ignores resources the profile does not scan", True)

    # Two devices, ours second: the scan must not stop at the first stranger.
    s, _ = scope_over({"USB0::a::INSTR": "SOME OTHER BOX,1,,1",
                       "USB0::b::INSTR": good})
    check("keeps looking past a stranger", s.connect() == good)


def main():
    print("=== msox2014a ===")
    prof = scope_profiles.PROFILES["msox2014a"]
    panel_checks(prof)
    profile_checks(prof)
    connect_checks(prof)

    # Whatever else gets registered gets the structural checks for free. The
    # panel and connect checks are written against the Keysight one because
    # they name its resource prefixes and its session settings.
    for key, other in sorted(scope_profiles.PROFILES.items()):
        if key == "msox2014a":
            continue
        print(f"\n=== {key} ===")
        profile_checks(other)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("All panel checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
