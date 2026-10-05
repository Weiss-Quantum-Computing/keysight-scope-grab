# Tests

```
python tests/run_tests.py
```

No scope, no pytest, no `pip install` beyond what the program already needs.
`test_panel.py` does need a desktop session, because it builds the real Tk
window - it just never enters `mainloop()`, so the auto-connect never fires and
nothing opens a VISA session.

| | |
|---|---|
| `test_output.py` | The metadata `.txt` and the setup `.txt` against golden copies |
| `test_panel.py` | The window, the profile wiring, and the connect logic against a fake VISA layer |
| `test_capture_files.py` | NPZ and CSV written and read back (NPZ bit-exact against `Scope.waveform()`), folders holding both, and the grab worker writing each format against a fake scope |
| `cases.py` | The settings snapshot and the metadata cases both of the above are built from |
| `test_probe.py` | `tools/probe_scope.py` driven against mock instruments |
| `test_path_import.py` | That EOM-ILC can still load `scope_grab.py` by file path |
| `test_headless.py` | The API another program drives a `Scope` through: settings snapshot, offset dither, capture files, each against what the panel does |
| `golden/` | Expected output, byte for byte |

## What the goldens are

They were generated from **commit 45b2bcd** - the last one before instrument
profiles existed:

```
python tests/test_output.py --from 45b2bcd
```

So this is not the program agreeing with itself. It says the two files that go
in the lab notebook still come out exactly as they did when the program only
knew about an MSO-X. That matters because every capture in the archive was
written by that version, and a quiet layout change would split the archive in
two.

Regenerate them **only** when the format is being changed on purpose, and say so
in the commit:

```
python tests/test_output.py --regenerate
```

The goldens are stored LF while the rest of the repo is CRLF, because they hold
what `metadata()` returns rather than what lands on disk - `open(..., "w")`
does the CRLF translation afterwards. `.gitattributes` marks them `-text` so
`core.autocrlf` leaves them alone; without it they would come back CRLF on a
fresh clone and every one of these would fail.

## What is not covered

Everything that needs the instrument. `accumulate`, `read_waveform`,
`screenshot` and `running` are only exercised through the profile interface, not
against hardware - a fake VISA session can only prove the plumbing, and the
things this program has historically got wrong were all behavioural: whether the
averager resets, whether the hit count reports the setting or the depth, which
points modes serve which record. Those are checked at the bench.

`test_panel.py` runs its structural checks against every profile in `PROFILES`
automatically. The panel and connect checks stay pinned to the MSO-X, since they
name its resource prefixes and its session settings.
