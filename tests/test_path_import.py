"""Check scope_grab.py still imports when loaded by file path.

EOM-ILC does not import this program as a package. ilc_bench.load_module does a
spec_from_file_location + exec_module on the path to scope_grab.py, and that
does NOT put the containing directory on sys.path. So the moment this program
grew a second file, `import scope_profiles` started raising ModuleNotFoundError
inside the ILC bench panel - a break that nothing else in this repo would have
caught, because every other test imports it with the repo root already on the
path.

The other half of the same contract: importing it must not need a display, must
not open a VISA session, and must not read or write the user's config. Anything
that happens at import time happens to the ILC panel too.

    python tests/test_path_import.py
"""
import importlib.util
import os
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TARGET = os.path.join(REPO, "scope_grab.py")

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILS.append(label)


# The import has to happen in a child process with a cwd and a sys.path that
# have nothing to do with this repo - running it in-process would let the
# entries these tests already added do the work, and prove nothing.
PROBE = textwrap.dedent("""
    import importlib.util, os, sys
    path = sys.argv[1]
    # Exactly ilc_bench.load_module, and deliberately no sys.path help.
    before = list(sys.path)
    spec = importlib.util.spec_from_file_location("scope_grab", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scope_grab"] = mod
    spec.loader.exec_module(mod)
    print("LOADED")
    print("PROFILES", len(mod.scope_profiles.PROFILES))
    print("HAS_APP", hasattr(mod, "App") and hasattr(mod, "Scope"))
    # Importing must not have built a window or opened an instrument.
    print("NO_TK_ROOT", "_default_root" not in dir(mod.tk) or
          getattr(mod.tk, "_default_root", None) is None)
""")


def main():
    print("loaded by path, the way EOM-ILC does it")
    # A cwd outside the repo, so a stray relative import cannot rescue it.
    run = subprocess.run([sys.executable, "-c", PROBE, TARGET],
                         capture_output=True, text=True,
                         cwd=os.path.expanduser("~"))
    out = run.stdout
    if run.returncode != 0:
        check("imports by path", False,
              (run.stderr.strip().split("\n") or ["?"])[-1])
        print("\n" + run.stderr)
        return 1
    check("imports by path", "LOADED" in out)
    check("scope_profiles came with it", "PROFILES 2" in out,
          [l for l in out.split("\n") if l.startswith("PROFILES")][0])
    check("Scope and App are both there", "HAS_APP True" in out)
    check("importing built no Tk window", "NO_TK_ROOT True" in out)

    # The same file, imported the ordinary way, must still work - that is how
    # the GUI itself and every other test loads it.
    print("\nand still imports normally")
    sys.path.insert(0, REPO)
    spec = importlib.util.spec_from_file_location("scope_grab_normal", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scope_grab_normal"] = mod
    try:
        spec.loader.exec_module(mod)
        check("imports as a module", True)
    except Exception as exc:
        check("imports as a module", False, f"{type(exc).__name__}: {exc}")

    # It must not have put anything on sys.path that was not already implied.
    check("only its own directory was added to sys.path",
          sys.path.count(REPO) <= 2, f"{sys.path.count(REPO)} entries")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("Path import works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
