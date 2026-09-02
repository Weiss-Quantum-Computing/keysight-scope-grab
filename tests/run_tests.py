"""Run every test. No scope, no pytest, no pip install.

    python tests/run_tests.py

Exits non-zero if anything failed, so it can go in front of a commit.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_output.py", "test_panel.py", "test_probe.py"]


def main():
    failed = []
    for name in SUITES:
        # flush: our own prints are buffered while the child writes straight to
        # the terminal, so without this the headers land after the output they
        # are meant to introduce.
        print(f"{'=' * 70}\n{name}\n{'=' * 70}", flush=True)
        rc = subprocess.run([sys.executable, os.path.join(HERE, name)]).returncode
        if rc:
            failed.append(name)
        print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"All {len(SUITES)} suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
