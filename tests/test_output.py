"""Check the files a capture writes against golden copies.

The goldens in tests/golden/ were generated from commit 45b2bcd - the last one
before instrument profiles existed - so this is not merely a test that the
program agrees with itself. It says the metadata .txt and the setup .txt still
come out byte for byte as they did when the program only knew about an MSO-X.

That matters more than it sounds. These two files are the lab record of what
the scope was set to when a trace was taken, and every capture in the archive
was written by that version. A layout change would quietly split the archive in
two.

Needs no scope: both files are built from a snapshot dict, and the cases live
in cases.py.

    python tests/test_output.py                 check
    python tests/test_output.py --regenerate    rewrite goldens from the working
                                                tree, for a deliberate change
    python tests/test_output.py --from 45b2bcd  rewrite them from a git commit,
                                                which is how they were made
"""
import argparse
import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GOLDEN = os.path.join(HERE, "golden")

sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import cases  # noqa: E402


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def module_from_commit(ref):
    """scope_grab.py as it was at `ref`, importable.

    Only used to regenerate the goldens. The old file is self-contained - it
    predates scope_profiles - so it loads on its own."""
    src = subprocess.run(["git", "-C", REPO, "show", f"{ref}:scope_grab.py"],
                         capture_output=True, check=True).stdout.decode("utf-8")
    tmp = os.path.join(tempfile.mkdtemp(), "scope_grab_old.py")
    io.open(tmp, "w", encoding="utf-8", newline="").write(src)
    return load_module(tmp, "scope_grab_at_" + ref)


class FrozenClock(datetime.datetime):
    """metadata() stamps the file with now(). Freeze it or every run differs."""
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(*cases.FROZEN_TIME)


def make_scope(mod):
    """A Scope from either module generation - the old one took no profile."""
    try:
        import scope_profiles
        scope = mod.Scope(scope_profiles.PROFILES["msox2014a"])
    except (TypeError, ImportError):
        scope = mod.Scope()
    scope.idn, scope.addr = cases.IDN, cases.ADDR
    return scope


def describe(mod, cfg):
    """describe_setup() gained a profile argument with the refactor."""
    try:
        import scope_profiles
        return mod.describe_setup(cfg, scope_profiles.PROFILES["msox2014a"])
    except TypeError:
        return mod.describe_setup(cfg)


def outputs(mod):
    """{golden stem: text} for everything this checks."""
    got = {}
    real, mod.datetime.datetime = mod.datetime.datetime, FrozenClock
    try:
        for stem, kw in cases.METADATA_CASES:
            got["metadata_" + stem] = make_scope(mod).metadata(**kw)
    finally:
        mod.datetime.datetime = real
    setup_dir = os.path.join(REPO, "scope-setups")
    for name in sorted(os.listdir(setup_dir)):
        if name.endswith(".json"):
            with io.open(os.path.join(setup_dir, name), encoding="utf-8") as fh:
                cfg = json.load(fh)
            got["setup_" + name[:-5]] = describe(mod, cfg)
    return got


def write_goldens(mod, note):
    os.makedirs(GOLDEN, exist_ok=True)
    got = outputs(mod)
    for stem, text in got.items():
        path = os.path.join(GOLDEN, stem + ".txt")
        io.open(path, "w", encoding="utf-8", newline="").write(text)
    print(f"wrote {len(got)} golden file(s) from {note}")


def check():
    mod = load_module(os.path.join(REPO, "scope_grab.py"), "scope_grab")
    got = outputs(mod)
    failures = []
    for stem in sorted(got):
        path = os.path.join(GOLDEN, stem + ".txt")
        if not os.path.exists(path):
            failures.append(stem)
            print(f"  MISSING golden for {stem}")
            continue
        want = io.open(path, encoding="utf-8", newline="").read()
        if got[stem] == want:
            print(f"  OK   {stem}  ({len(want)} chars)")
            continue
        failures.append(stem)
        print(f"  DIFF {stem}")
        a, b = want.split("\n"), got[stem].split("\n")
        for i in range(max(len(a), len(b))):
            x = a[i] if i < len(a) else "<missing>"
            y = b[i] if i < len(b) else "<missing>"
            if x != y:
                print(f"       line {i + 1}\n         want: {x!r}\n         got : {y!r}")
    stale = [n[:-4] for n in os.listdir(GOLDEN)
             if n.endswith(".txt") and n[:-4] not in got]
    for stem in stale:
        failures.append(stem)
        print(f"  STALE golden with no case: {stem}")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--regenerate", action="store_true",
                    help="rewrite the goldens from the working tree")
    ap.add_argument("--from", dest="ref", metavar="COMMIT",
                    help="rewrite the goldens from a git commit")
    args = ap.parse_args()

    if args.ref:
        write_goldens(module_from_commit(args.ref), f"commit {args.ref}")
        return 0
    if args.regenerate:
        write_goldens(load_module(os.path.join(REPO, "scope_grab.py"),
                                  "scope_grab"), "the working tree")
        return 0

    print("output files against the goldens from 45b2bcd")
    failures = check()
    print()
    if failures:
        print(f"FAILED: {len(failures)} file(s) differ: {', '.join(failures)}")
        return 1
    print("All output matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
