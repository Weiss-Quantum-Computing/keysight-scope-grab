#!/usr/bin/env python3
"""
Run a profile against the instrument it claims to describe.

probe_scope.py discovers what a scope does. This checks that a profile written
from those findings actually works: every panel field answers, a capture
completes, the metadata file comes out, and - the part that matters most - the
scope is in the same state afterwards as before.

It exercises the profile through Scope, the same path the GUI uses, so a field
that only works because the panel happens not to read it will still fail here.

    python tools/check_profile.py ds1054z
    python tools/check_profile.py msox2014a --depth 8

WHAT IT CHANGES: it arms one acquisition and builds one shallow average, so the
acquisition type, the average count and the run state move. Every setting it
touches is read first and put back at the end, and the last thing it does is
compare the whole panel against the snapshot it started from and report any
field that did not come home.
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import scope_grab      # noqa: E402
import scope_profiles  # noqa: E402

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILS.append(label)


def panel_roots(prof):
    """Every field the settings panel would build, as the panel builds them."""
    roots = [s for _, s, _, _ in prof.timebase]
    roots += [s for _, s, _, _ in prof.trigger]
    roots += [s for _, s in prof.info]
    for ch in prof.channels:
        roots += [t.format(ch=ch) for _, t, _, _ in prof.channel]
    return roots


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model", choices=sorted(scope_profiles.PROFILES))
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--depth", type=int, default=8,
                    help="averaging depth to build (default 8, kept small "
                         "because a software average is one round trip a sweep)")
    args = ap.parse_args()

    prof = scope_profiles.PROFILES[args.model]
    scope = scope_grab.Scope(prof)

    print(f"connecting as {prof.name}")
    idn = scope.connect()
    check("connect", True, idn)
    check("IDN matches the profile", prof.matches(idn))

    # Everything the panel would read, so a field that has been mistyped in the
    # profile shows up as a refusal rather than as a blank box months later.
    print("\nevery panel field answers")
    roots = panel_roots(prof)
    before = {}
    dead = []
    for scpi in roots:
        value = scope.try_get(scpi, timeout_ms=2500)
        if value is None:
            dead.append(scpi)
        else:
            before[scpi] = value
    check(f"all {len(roots)} fields readable", not dead,
          "" if not dead else "no answer: " + ", ".join(dead))

    print("\nthe roots the capture path asks for by name")
    check("acquisition type reads", scope.get(prof.acq_type) is not None,
          before.get(prof.acq_type, ""))
    check("averaging() agrees with it",
          scope.averaging("AVER") and not scope.averaging("NORM"))
    disp = scope.is_displayed(args.channel)
    check(f"CH{args.channel} display flag reads", disp is not None, str(disp))

    saved = {k: before[k] for k in (prof.acq_type, prof.acq_count,
                                    ":TRIGger:SWEep") if k in before}
    was_running = scope.is_running()
    print(f"\n  saved: {saved}, running={was_running}")

    try:
        print("\nplain capture")
        scope.put(prof.acq_type, "NORMal")
        triggered = scope.single(wait_s=8.0)
        check("single() completed", triggered is True, f"returned {triggered}")
        mode, points = scope.transfer_plan(False, None)
        t, v = scope.waveform(args.channel, points_mode=mode, points=points)
        check("waveform() returned samples", len(v) > 0, f"{len(v)} points")
        check("time axis is the same length", len(t) == len(v))
        check("time axis increases", len(t) > 1 and t[1] > t[0])
        check("volts look like volts", -100 < float(v.min()) and
              float(v.max()) < 100, f"{v.min():.3f} .. {v.max():.3f} V")

        # The single is the one that rewrites the sweep on a Rigol; this is the
        # check that before_single/after_single actually put it back.
        if ":TRIGger:SWEep" in before:
            now = scope.get(":TRIGger:SWEep")
            check("single() left the trigger sweep alone",
                  now == before[":TRIGger:SWEep"],
                  f"was {before[':TRIGger:SWEep']}, now {now}")

        print(f"\naveraged capture, {args.depth} deep")
        scope.put(prof.acq_type, "AVERages" if args.model == "ds1054z" else "AVER")
        scope.put(prof.acq_count, str(args.depth))
        t0 = time.time()
        hits = scope.accumulate(args.depth, wait_s=15.0,
                                channels=[args.channel])
        took = time.time() - t0
        check("accumulate() returned a depth", isinstance(hits, int) and hits > 0,
              f"{hits} of {args.depth} in {took:.1f} s")
        ta, va = scope.waveform(args.channel, *scope.transfer_plan(True, None))
        check("averaged waveform came back", len(va) > 0, f"{len(va)} points")
        if len(va) and len(v):
            import numpy as np
            raw_codes = len(np.unique(v))
            avg_codes = len(np.unique(va))
            check("averaging did something to the trace", avg_codes != raw_codes,
                  f"{raw_codes} distinct levels raw, {avg_codes} averaged")
        count = scope.hit_count()
        check("hit_count() answers", count is not None, str(count))
        if hits and count is not None:
            check("hit_count() reports what was actually built",
                  str(count) == str(hits),
                  f"accumulate counted {hits}, hit_count says {count}")

        print("\nthe rest of what a grab needs")
        img = scope.screenshot()
        kind = "PNG" if bytes(img[:4]) == b"\x89PNG" else f"{bytes(img[:4])!r}"
        check("screenshot is a PNG", kind == "PNG", f"{len(img)} bytes")
        settings = {s: scope.try_get(s, 2500) or "?" for s in roots}
        settings[prof.wave_count] = str(count)
        meta = scope.metadata([args.channel], settings,
                              names={args.channel: "check"}, label=None)
        check("metadata built", meta.count("\n") > 15,
              f"{meta.count(chr(10))} lines")
        check("metadata names the instrument", idn.split(",")[1] in meta)
        check("metadata carries no unknowns", ": ?" not in meta,
              "" if ": ?" not in meta else
              "; ".join(l for l in meta.split("\n") if ": ?" in l))
    finally:
        print("\nrestoring")
        for scpi, value in saved.items():
            scope.put(scpi, value)
            print(f"  {scpi} <- {value}")
        scope.run() if was_running else scope.command(prof.cmd_stop)
        errs = scope.errors()
        check("error queue empty at the end", not errs, "; ".join(errs))
        time.sleep(0.5)
        moved = []
        for scpi, want in before.items():
            got = scope.try_get(scpi, 2500)
            # Info rows are results, not settings - a sample rate that moved is
            # the scope working, not a setting left behind.
            if scpi in [s for _, s in prof.info]:
                continue
            if got is not None and got != want:
                moved.append(f"{scpi}: {want} -> {got}")
        check("every setting came home", not moved, "; ".join(moved))
        scope.close()

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print(f"{prof.name} profile works against the real instrument.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
